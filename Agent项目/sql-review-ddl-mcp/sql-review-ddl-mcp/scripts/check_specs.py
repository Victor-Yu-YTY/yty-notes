"""规范预检：只读 standards/ 下的文档，打印规则清单，不连库、不起服务。

检查内容
--------
1. ``standards/ddl.md``  → 35 条规则（级别/适用/检查/参数/情形/占位符/阈值全部校验）
2. ``standards/dml.md``  → 22 条规则 + 2 条允许项（判定表达式、事实名、阈值、允许项冲突）
3. ``standards/mysql.md`` → 总纲条款对照表（披露"规范有、本服务不审"的条款）

用法
----
    PYTHONPATH=src python scripts/check_specs.py
    PYTHONPATH=src python scripts/check_specs.py --json
    PYTHONPATH=src python scripts/check_specs.py --ddl-file /path/to/ddl.md

退出码：0 = 全部通过；2 = 有文档不可用（服务会拒绝启动）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sql_review_ddl import spec                                    # noqa: E402
from sql_review_ddl.ddlchecks import CHECKS, check_specs            # noqa: E402
from sql_review_ddl.dmfacts import FACT_NAMES_REGISTERED            # noqa: E402

CHECKS_FIELDS = check_specs()


def _sample_settings():
    """跑样例用的 Settings：直接构造，不依赖 PyYAML / settings.yaml（无 env，不连库）。"""
    from sql_review_ddl.common import Thresholds
    from sql_review_ddl.config import Settings

    s = Settings()
    s.home = ROOT
    s.config_dir = ROOT / "config"
    s.standards_dir = ROOT / "standards"
    s.log_dir = ROOT / "logs"
    s.data_dir = ROOT / "data"
    s.report_dir = ROOT / "logs"
    s.standard_path = ROOT / "standards" / "ddl.md"
    s.dml_standard_path = ROOT / "standards" / "dml.md"
    s.total_standard_path = ROOT / "standards" / "mysql.md"
    s.override_path = ROOT / "config" / "__no_override__.yaml"   # 样例不受 override 影响
    s.questions_path = ROOT / "config" / "questions.yaml"
    s.standard_version_file = ROOT / "config" / "STANDARD_VERSION"
    s.thresholds = Thresholds()
    s.envs = {}
    s.metadata_backend = "none"
    return s


def run_samples(rs, doc: str) -> tuple[int, int, list[str]]:
    """跑文档里 `**样例**：命中 / 通过` 声明的正反用例。

    命中样例：该规则必须出现在 findings 里（级别不限）；
    通过样例：该规则不得出现在 findings 里。
    """
    from sql_review_ddl.reviewer import review_sql

    settings = _sample_settings()
    passed = failed = 0
    fails: list[str] = []
    for rule in rs.rules:
        for sql in rule.sample_sqls("hit"):
            res = review_sql(sql, settings)
            if not res.get("ok"):
                failed += 1
                fails.append(f"{doc} {rule.id} 命中样例执行失败："
                             f"{(res.get('error') or {}).get('message')} | {sql[:60]}")
                continue
            hits = {f["rule_id"] for f in res["data"]["findings"]}
            if rule.id in hits:
                passed += 1
            else:
                failed += 1
                fails.append(f"{doc} {rule.id} 命中样例未命中"
                             f"（实际命中 {sorted(hits) or '无'}） | {sql[:60]}")
        for sql in rule.sample_sqls("pass"):
            res = review_sql(sql, settings)
            if not res.get("ok"):
                failed += 1
                fails.append(f"{doc} {rule.id} 通过样例执行失败："
                             f"{(res.get('error') or {}).get('message')} | {sql[:60]}")
                continue
            hits = {f["rule_id"] for f in res["data"]["findings"]}
            if rule.id not in hits:
                passed += 1
            else:
                failed += 1
                fails.append(f"{doc} {rule.id} 通过样例误命中该规则 | {sql[:60]}")
    return passed, failed, fails


def check_doc(path: Path, doc: str) -> tuple[bool, dict]:
    try:
        rs = spec.load_doc(path, doc, FACT_NAMES_REGISTERED, CHECKS_FIELDS)
    except spec.SpecError as exc:
        return False, {"ok": False, "path": str(path), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return False, {"ok": False, "path": str(path),
                       "error": f"{type(exc).__name__}: {exc}"}
    levels: dict[str, int] = {}
    for r in rs.rules:
        levels[r.level] = levels.get(r.level, 0) + 1
    return True, {
        "ok": True,
        "path": str(path),
        "rules_version": rs.rules_version,
        "sha256": rs.sha256,
        "rules_count": len(rs.rules),
        "enabled_count": len(rs.enabled_rules()),
        "permits_count": len(rs.permits),
        "levels": levels,
        "thresholds": dict(rs.thresholds),
        "rules": [r.as_dict() for r in rs.rules],
        "permits": [p.as_dict() for p in rs.permits],
    }


def check_total(path: Path) -> tuple[bool, dict]:
    """总纲只做可读性检查：条款对照表能读出「不审核」条目即可。"""
    if not path.exists():
        return False, {"ok": False, "path": str(path), "error": "总纲文件不存在"}
    text = path.read_text(encoding="utf-8")
    uncovered = [(m.group(1).strip(), m.group(2).strip()) for m in
                 (re.match(r"^\|\s*([^|]{4,}?)\s*\|\s*不审核（([^）]+)）\s*\|", line)
                  for line in text.splitlines()) if m]
    covered = len(re.findall(r"`(TB|IX|AL|DX|SC|DM)-\d+`", text))
    return True, {"ok": True, "path": str(path), "covered_refs": covered,
                  "uncovered": [{"clause": c, "reason": r} for c, r in uncovered]}


def _check_or_skip(path: Path, doc: str):
    """拆分后的包只带自己那一份判定细则：文件不存在 → (None, 说明) 表示「跳过」。

    返回 (True|False|None, 结果字典)；None 表示本服务不加载该文档（由另一个服务负责）。
    """
    if not path.exists():
        return None, {"ok": True, "skipped": True, "path": str(path),
                      "reason": f"本包未提供 standards/{doc}.md（由另一个 SQL Review 服务加载）"}
    return check_doc(path, doc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check-specs",
                                 description="校验 standards/ 下的规则文档（DDL + DML + 总纲）")
    ap.add_argument("--ddl-file", default=str(ROOT / "standards" / "ddl.md"))
    ap.add_argument("--dml-file", default=str(ROOT / "standards" / "dml.md"))
    ap.add_argument("--total-file", default=str(ROOT / "standards" / "mysql.md"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-samples", action="store_true",
                    help="只校验文档结构，不跑 `**样例**` 正反用例")
    args = ap.parse_args(argv)

    ddl_ok, ddl = _check_or_skip(Path(args.ddl_file), "ddl")
    dml_ok, dml = _check_or_skip(Path(args.dml_file), "dml")
    tot_ok, tot = check_total(Path(args.total_file))

    # v1.4.0：跑文档里声明的 `**样例**`（命中 / 通过）——"改 md 就能上线"的兜底验证
    sample_pass = sample_fail = 0
    sample_fails: list[str] = []
    if not args.no_samples:
        for ok, info, doc in ((ddl_ok, ddl, "ddl"), (dml_ok, dml, "dml")):
            if not ok:
                continue
            try:
                rs = spec.load_doc(Path(info["path"]), doc, FACT_NAMES_REGISTERED, CHECKS_FIELDS)
            except spec.SpecError:
                continue
            p, f, fails = run_samples(rs, doc)
            sample_pass += p
            sample_fail += f
            sample_fails.extend(fails)

    # 拆分部署（v1.0.0）：本包只带一侧判定细则，另一侧 md 不存在 → 跳过，不算失败
    checked = [ok for ok in (ddl_ok, dml_ok, tot_ok) if ok is not None]
    all_ok = bool(checked) and all(checked) and sample_fail == 0

    if args.json:
        print(json.dumps({"ok": all_ok, "ddl": ddl, "dml": dml, "total": tot,
                          "samples": {"pass": sample_pass, "fail": sample_fail,
                                      "failures": sample_fails}},
                         ensure_ascii=False, indent=2))
        return 0 if all_ok else 2

    for name, ok, info in (("DDL", ddl_ok, ddl), ("DML", dml_ok, dml), ("总纲", tot_ok, tot)):
        print("=" * 78)
        print(f"{name} 规范：{info.get('path', '')}")
        print("=" * 78)
        if ok is None:
            print(f"⏭ 跳过：{info.get('reason', '')}")
            print("")
            continue
        if not ok:
            print(f"❌ {info.get('error')}")
            print("   改文档修正后重跑本脚本；服务启动时也会做同样校验。")
            print("")
            continue
        if name == "总纲":
            print(f"条款引用规则数  : {info['covered_refs']}")
            print(f"披露不审核条款  : {len(info['uncovered'])} 条")
            for u in info["uncovered"]:
                print(f"  ! {u['clause']}（{u['reason']}）")
            print("")
            continue
        print(f"规则版本 : {info['rules_version']}")
        print(f"sha256   : {info['sha256']}")
        print(f"规则数   : {info['rules_count']}（启用 {info['enabled_count']}，"
              f"允许项 {info['permits_count']}）")
        print(f"级别分布 : " + "，".join(f"{k}={v}" for k, v in sorted(info["levels"].items())))
        for r in info["rules"]:
            how = f"检查:{r['check']}" if r["check"] else "判定:表达式"
            flag = "" if r["enabled"] else "（停用）"
            cases = f" 情形{len(r['cases'])}" if r["cases"] else ""
            print(f"  - {r['id']:7s} {r['level']:5s} {how:22s} 第{r['line']:>4}行 "
                  f"{r['title']}{cases}{flag}")
        print("")
        if info["permits"]:
            print("允许项：")
            for p in info["permits"]:
                print(f"  - {p['id']}  {p['title']}")
            print("")

    # 检查名覆盖：文档里用到的检查都在 CHECKS 里（load_doc 已校验，这里给出清单）
    # 检查名覆盖清单只在「本包带了 ddl.md」时才有意义（DML 包不加载 DDL 检查）
    if ddl_ok:
        used = {r["check"] for r in ddl["rules"] if r["check"]}
        unused = sorted(set(CHECKS) - used)
        print("=" * 78)
        if unused:
            print(f"ℹ️ 已注册但文档未使用的检查（可复用）：{', '.join(unused)}")

    # 样例回归（文档里 `**样例**：命中 / 通过`）
    if not args.no_samples:
        print("=" * 78)
        print(f"样例回归：通过 {sample_pass} 条，失败 {sample_fail} 条")
        for item in sample_fails:
            print(f"  ❌ {item}")
        if not sample_pass and not sample_fail:
            print("  ℹ️ 文档里还没写 `**样例**`（建议给每条规则至少写一条命中样例）")
        print("")

    print("✅ 全部文档校验通过，改 md 后下次调用即生效（不用重启）" if all_ok
          else "❌ 有文档校验/样例失败，请按提示修正（服务启动时同样会拒）")
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
