"""引擎自检：不需要 MCP 依赖、不需要数据库。

覆盖（v1.4 规则全文档化；v1.0.0 拆分后按本包实际带的规范自动裁剪）
--------------------------------------------------------------
A. 规范解析：ddl.md 35 条 / dml.md 22 条 + 2 允许项，阈值、级别分布
B. 文档写坏必须被拒：未知检查、未知占位符、未知情形、未知事实、阈值拼错、级别写错
C. DDL 引擎：命中规则 / 自动改写（sql_final）/ 破坏性驳回 / 缺事实标 ⏸
D. DML 引擎：缺 WHERE、DELETE 驳回、事务包裹、ORDER BY
E. **文档驱动验证**：只改 md（不改代码）→ 级别/停用/阈值/新增规则立即生效
F. 反问目录：questions.yaml 的话术能带占位符渲染

★ 拆分说明（v1.0.0）：sql-review-ddl-mcp 只带 standards/ddl.md，sql-review-dml-mcp
  只带 standards/dml.md。缺哪一份就跳过对应小节（打印 [SKIP]），不会误判为失败；
  因此两个包直接使用**同一份**自检脚本，只有首部 import 的包名不同。

运行：PYTHONPATH=src python scripts/selftest.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sql_review_ddl import spec                                    # noqa: E402
from sql_review_ddl.config import load_settings                     # noqa: E402
from sql_review_ddl.ddlchecks import check_specs                    # noqa: E402
from sql_review_ddl.dmfacts import FACT_NAMES_REGISTERED            # noqa: E402
from sql_review_ddl.reviewer import get_ruleset, review_sql          # noqa: E402

DDL_MD = ROOT / "standards" / "ddl.md"
DML_MD = ROOT / "standards" / "dml.md"
CHECKS_FIELDS = check_specs()

# 拆分部署：本包带哪一份规范，就只跑哪一侧的自检
HAS_DDL = DDL_MD.exists()
HAS_DML = DML_MD.exists()

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"[OK ] {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"[FAIL] {msg}")


def check(cond: bool, msg: str) -> bool:
    ok(msg) if cond else bad(msg)
    return bool(cond)


# --------------------------------------------------------------------------
# 环境
# --------------------------------------------------------------------------


def base_settings():
    """自检专用 Settings：直接构造，不依赖 PyYAML / settings.yaml（生产走 load_settings）。"""
    from sql_review_ddl.common import Thresholds
    from sql_review_ddl.config import Settings

    s = Settings()
    s.home = ROOT
    s.config_dir = ROOT / "config"
    s.standards_dir = ROOT / "standards"
    s.log_dir = ROOT / "logs"
    s.data_dir = ROOT / "data"
    s.report_dir = ROOT / "logs"
    s.standard_path = DDL_MD
    s.dml_standard_path = DML_MD
    s.total_standard_path = ROOT / "standards" / "mysql.md"
    s.override_path = ROOT / "config" / "rules.override.yaml"
    s.questions_path = ROOT / "config" / "questions.yaml"
    ver = ROOT / "config" / "STANDARD_VERSION"
    s.standard_version = ver.read_text(encoding="utf-8").strip() if ver.exists() else "unknown"
    s.thresholds = Thresholds()
    s.envs = {}
    s.metadata_backend = "none"
    return s


def settings_with(ddl_path: Path | None = None, dml_path: Path | None = None, **kw):
    patch = dict(kw)
    if ddl_path is not None:
        patch["standard_path"] = Path(ddl_path)
    if dml_path is not None:
        patch["dml_standard_path"] = Path(dml_path)
    return replace(base_settings(), **patch)


def review(sql: str, s=None, env: str | None = None, database: str | None = None) -> dict:
    return review_sql(sql, s or base_settings(), env=env, database=database)


def rules_of(data: dict) -> set:
    return {f["rule_id"] for f in (data.get("findings") or [])}


def samples_of(rs) -> int:
    return sum(len(r.sample_sqls("hit")) + len(r.sample_sqls("pass")) for r in rs.rules)


# --------------------------------------------------------------------------
# A. 规范解析
# --------------------------------------------------------------------------


def test_specs() -> None:
    print("=" * 78)
    print("A. 规范解析（文档 → 规则集）")
    print("=" * 78)

    if HAS_DDL:
        ddl = spec.load_doc(DDL_MD, "ddl", FACT_NAMES_REGISTERED, CHECKS_FIELDS)
        check(len(ddl.rules) == 35, f"ddl.md 解析出 35 条规则（实际 {len(ddl.rules)}）")
        lv: dict[str, int] = {}
        for r in ddl.rules:
            lv[r.level] = lv.get(r.level, 0) + 1
        check(lv.get("error") == 26 and lv.get("warn") == 8 and lv.get("info") == 1,
              f"DDL 级别分布：❌{lv.get('error')} ⚠️{lv.get('warn')} ✅{lv.get('info')}")
        check(ddl.thresholds.get("max_columns") == 30
              and ddl.thresholds.get("big_table_rows") == 5000000,
              "ddl.md 阈值表解析正确（max_columns=30，big_table_rows=5000000）")
        check(ddl.get("TB-08") is not None and ddl.get("TB-08").p_list("columns") != [],
              "TB-08 必备字段清单来自文档参数")
        check(ddl.get("TB-01") is not None and len(ddl.get("TB-01").cases) >= 6,
              "TB-01 情形文案来自文档")
        n = samples_of(ddl)
        check(n >= 30, f"ddl.md 内置 `**样例**` 共 {n} 条（由 check_specs.py 逐条执行）")
    else:
        print("[SKIP] 本包不含 standards/ddl.md（DDL 规范由 SQL Review DDL 服务加载）")

    if HAS_DML:
        dml = spec.load_doc(DML_MD, "dml", FACT_NAMES_REGISTERED, CHECKS_FIELDS)
        check(len(dml.rules) == 22, f"dml.md 解析出 22 条规则（实际 {len(dml.rules)}）")
        check(len(dml.permits) == 2, f"dml.md 解析出 2 条允许项（实际 {len(dml.permits)}）")
        dlv: dict[str, int] = {}
        for r in dml.rules:
            dlv[r.level] = dlv.get(r.level, 0) + 1
        check(dlv.get("error") == 18 and dlv.get("warn") == 4,
              f"DML 级别分布：❌{dlv.get('error')} ⚠️{dlv.get('warn')}")
        check(all(r.auto_fix is False for r in dml.rules),
              "DML 规则自动改写恒为 false（只报错不代劳）")
        n = samples_of(dml)
        check(n >= 10, f"dml.md 内置 `**样例**` 共 {n} 条（由 check_specs.py 逐条执行）")
    else:
        print("[SKIP] 本包不含 standards/dml.md（DML 规范由 SQL Review DML 服务加载）")


# --------------------------------------------------------------------------
# B. 写坏必须被拒
# --------------------------------------------------------------------------


def _load_from_text(text: str, doc: str):
    fh = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    tmp = Path(fh.name)
    try:
        return spec.load_doc(tmp, doc, FACT_NAMES_REGISTERED, CHECKS_FIELDS)
    finally:
        tmp.unlink(missing_ok=True)


def test_reject_broken() -> None:
    print("")
    print("=" * 78)
    print("B. 文档写坏必须被拒（绝不静默跳过）")
    print("=" * 78)

    if HAS_DDL:
        base = DDL_MD.read_text(encoding="utf-8")
        cases = [
            ("未知检查名", "- **检查**：forbidden_types", "- **检查**：no_such_check", "未知的检查"),
            ("未知占位符", "- **说明**：禁止使用 {old} 存储浮点数，必须用 DECIMAL",
             "- **说明**：禁止使用 {no_such_field} 存储浮点数", "占位符"),
            ("未知情形名", "- **情形**：uppercase = ❌ | {kind}必须使用大写字母 | 已改为 {fixed}",
             "- **情形**：no_such_case = ❌ | x | y", "情形"),
            ("级别写错", "- **级别**：❌", "- **级别**：必须改", "级别"),
            ("缺适用", "- **适用**：create_table\n- **检查**：comments",
             "- **检查**：comments", "适用"),
            ("检查与判定同时写", "- **适用**：create_table\n- **检查**：comments",
             "- **适用**：create_table\n- **检查**：comments\n- **判定**：has_where = false",
             "有且只有一个"),
        ]
        for name, old, new, keyword in cases:
            if old not in base:
                bad(f"用例「{name}」锚点文本已变，请同步自检：{old[:40]!r}")
                continue
            try:
                _load_from_text(base.replace(old, new, 1), "ddl")
                bad(f"用例「{name}」没有被拒绝（应报「{keyword}」）")
            except spec.SpecError as exc:
                check(keyword in str(exc), f"用例「{name}」被拒：{str(exc)[:70]}")
    else:
        print("[SKIP] 本包不含 standards/ddl.md，跳过 DDL 文档写坏用例")

    if HAS_DML:
        dbase = DML_MD.read_text(encoding="utf-8")
        dml_cases = [
            ("DML 阈值拼错", "insert_rows > $max_insert_rows", "insert_rows > $max_insert_row"),
            ("DML 未注册事实", "has_where = false", "no_such_fact = false"),
            ("DML 允许项冲突", "- **判定**：has_order_by = true", "- **判定**：has_limit = true"),
        ]
        for name, old, new in dml_cases:
            if old not in dbase:
                bad(f"用例「{name}」锚点文本已变：{old[:30]!r}")
                continue
            try:
                _load_from_text(dbase.replace(old, new, 1), "dml")
                bad(f"用例「{name}」没有被拒绝")
            except spec.SpecError as exc:
                check(True, f"用例「{name}」被拒：{str(exc)[:70]}")
    else:
        print("[SKIP] 本包不含 standards/dml.md，跳过 DML 文档写坏用例")


# --------------------------------------------------------------------------
# C. DDL 引擎
# --------------------------------------------------------------------------

BAD_CREATE = """CREATE TABLE t_order (
  id int(11) NOT NULL AUTO_INCREMENT,
  name varchar(50) DEFAULT NULL,
  amount float DEFAULT NULL,
  status enum('a','b') DEFAULT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;"""

GOOD_CREATE = """CREATE TABLE DEMO_ORDER (
  ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
  IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除',
  CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI',
  ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号',
  PRIMARY KEY (ID),
  KEY IDX_CREATE_TIME (CREATE_TIME),
  KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更';"""


def test_ddl() -> None:
    print("")
    print("=" * 78)
    print("C. DDL 引擎（判定 / 自动改写 / 驳回 / 未判定）")
    print("=" * 78)
    if not HAS_DDL:
        print("[SKIP] 本包不含 standards/ddl.md —— DDL 引擎自检由 SQL Review DDL 服务跑")
        return
    s = base_settings()

    data = review(BAD_CREATE, s)["data"]
    r = rules_of(data)
    check({"TB-01", "TB-02", "TB-03", "TB-04", "TB-08", "TB-09", "TB-10"} <= r,
          f"不合规建表命中核心规则（{len(r)} 条：{'、'.join(sorted(r))}）")
    final = (data["sql_final"] or "").upper().replace(" ", "")
    check("DECIMAL(12,2)" in final, "TB-03 已改写为 DECIMAL(12,2)")
    check("TINYINT" in final, "TB-04 已改写为 TINYINT")
    check("ENGINE=INNODB" in final and "UTF8MB4" in final, "TB-10 已补 ENGINE/CHARSET")

    data = review(GOOD_CREATE, s)["data"]
    check("TB-08" not in rules_of(data) and "TB-09" not in rules_of(data),
          f"合规建表不再命中必备字段/索引（verdict={data['verdict']}）")

    data = review("DROP TABLE DEMO_ORDER;", s)["data"]
    check("DX-01" in rules_of(data) and data["verdict"] == "rejected",
          f"DROP TABLE → DX-01 且 rejected（rules={sorted(rules_of(data))}）")

    data = review("TRUNCATE TABLE DEMO_ORDER;", s)["data"]
    check("DX-02" in rules_of(data), "TRUNCATE → DX-02")

    data = review("ALTER TABLE DEMO_ORDER MODIFY COLUMN ORDER_NO VARCHAR(64) NOT NULL "
                  "DEFAULT '' COMMENT '订单号';", s)["data"]
    check("AL-03" in rules_of(data) and data["verdict"] == "need_more_info",
          f"ALTER MODIFY 缺 env → AL-03 标 ⏸（verdict={data['verdict']}）")
    check("Q-CTX-STRUCT" in {q["id"] for q in data["questions"]}, "缺结构时给出 Q-CTX-STRUCT 反问")

    data = review("ALTER TABLE DEMO_ORDER ADD COLUMN REMARK VARCHAR(32) NOT NULL "
                  "DEFAULT '' COMMENT '备注' AFTER ORDER_NO;", s)["data"]
    check("AL-01" in rules_of(data), "ALTER 带 AFTER → AL-01")
    check("AFTER" not in (data["sql_final"] or "").upper(), "AL-01 已移除 AFTER 子句")

    data = review("CREATE INDEX idx_order_no ON DEMO_ORDER (ORDER_NO);", s)["data"]
    check("IX-01" in rules_of(data), f"小写索引名 → IX-01（rules={sorted(rules_of(data))}）")
    check("IDX_ORDER_NO" in (data["sql_final"] or ""), "IX-01 已改写索引名为 IDX_ORDER_NO")


# --------------------------------------------------------------------------
# D. DML 引擎
# --------------------------------------------------------------------------


def test_dml() -> None:
    print("")
    print("=" * 78)
    print("D. DML 引擎（表达式判定 / 事务包裹 / 驳回 / 不改写）")
    print("=" * 78)
    if not HAS_DML:
        print("[SKIP] 本包不含 standards/dml.md —— DML 引擎自检由 SQL Review DML 服务跑")
        return
    s = base_settings()

    data = review("UPDATE DEMO_ORDER SET ORDER_NO = 'A' WHERE ID = 1;", s)["data"]
    r = rules_of(data)
    check("DM-10" in r, f"裸 UPDATE 未包事务 → DM-10（rules={sorted(r)}）")
    check(data["verdict"] != "pass", f"裸 UPDATE 不能放行（verdict={data['verdict']}）")
    check(data["sql_final"] == data["statements"][0]["sql_input"], "DML 不改写（sql_final == sql_input）")

    data = review("START TRANSACTION;\nUPDATE DEMO_ORDER SET ORDER_NO = 'A' WHERE ID = 1;\nCOMMIT;",
                  s)["data"]
    check("DM-10" not in rules_of(data), f"事务包裹后不再命中 DM-10（rules={sorted(rules_of(data))}）")
    check(data["verdict"] in ("pass", "need_more_info"), f"事务包裹后结论 = {data['verdict']}")

    data = review("DELETE FROM DEMO_ORDER WHERE ID = 1;", s)["data"]
    check("DM-12" in rules_of(data) and data["verdict"] == "rejected",
          f"DELETE → DM-12 一律驳回（verdict={data['verdict']}，rules={sorted(rules_of(data))}）")

    data = review("REPLACE INTO DEMO_ORDER (ID) VALUES (1);", s)["data"]
    check("DM-12" in rules_of(data), "REPLACE INTO → DM-12")

    data = review("UPDATE DEMO_ORDER SET A = 1 WHERE ID = 1 ORDER BY ID;", s)["data"]
    check("DM-07" in rules_of(data), "UPDATE 带 ORDER BY → DM-07")

    data = review("INSERT INTO DEMO_ORDER VALUES (1, 'x');", s)["data"]
    check("DM-11" in rules_of(data), "INSERT 不写列名 → DM-11")


# --------------------------------------------------------------------------
# E. 文档驱动验证（核心诉求）：只改 md，不改代码
# --------------------------------------------------------------------------


def _tmp_md(src: Path, transform) -> Path:
    text = transform(src.read_text(encoding="utf-8"))
    fh = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    return Path(fh.name)


def test_doc_driven() -> None:
    print("")
    print("=" * 78)
    print("E. 文档驱动验证（只改 md、不改代码）")
    print("=" * 78)
    if HAS_DDL:
        _doc_driven_ddl()
    else:
        print("[SKIP] 本包不含 standards/ddl.md，跳过 DDL 文档驱动用例")
    if HAS_DML:
        _doc_driven_dml()
    else:
        print("[SKIP] 本包不含 standards/dml.md，跳过 DML 文档驱动用例")
    _doc_driven_version()


def _doc_driven_ddl() -> None:
    def lower_tb03(md: str) -> str:
        i = md.find("### TB-03")
        j = md.find("- **级别**：❌", i)
        return md[:j] + "- **级别**：⚠️" + md[j + len("- **级别**：❌"):]

    tmp = _tmp_md(DDL_MD, lower_tb03)
    try:
        d = review(BAD_CREATE, settings_with(ddl_path=tmp))["data"]
        lv = {f["level"] for f in d["findings"] if f["rule_id"] == "TB-03"}
        check(lv == {"warn"}, f"只改 ddl.md 级别 → TB-03 变 ⚠️（实际 {lv or '未命中'}）")
    finally:
        tmp.unlink(missing_ok=True)

    def disable_tb02(md: str) -> str:
        i = md.find("### TB-02")
        j = md.find("- **条款**", i)
        return md[:j] + "- **停用**：true\n" + md[j:]

    tmp = _tmp_md(DDL_MD, disable_tb02)
    try:
        d = review(BAD_CREATE, settings_with(ddl_path=tmp))["data"]
        check("TB-02" not in rules_of(d), "只改 ddl.md 写「停用：true」→ TB-02 不再命中")
    finally:
        tmp.unlink(missing_ok=True)

    tmp = _tmp_md(DDL_MD, lambda md: md.replace("| max_columns | 30 |",
                                                "| max_columns | 2 |", 1))
    try:
        d = review(GOOD_CREATE, settings_with(ddl_path=tmp))["data"]
        check("TB-12" in rules_of(d), "只改 ddl.md 阈值 max_columns=2 → TB-12 命中")
    finally:
        tmp.unlink(missing_ok=True)

    def add_rule(md: str) -> str:
        block = """### TB-17 禁用 GEOMETRY 类型

- **级别**：❌
- **适用**：create_table
- **检查**：forbidden_types
- **参数**：types = GEOMETRY, POINT; fix_mode = none
- **条款**：空间类型需单独评估
- **说明**：禁止使用 {type} 类型
- **整改**：请与 DBA 确认空间字段方案

## 不在本服务审核范围（仅披露）"""
        return md.replace("## 不在本服务审核范围（仅披露）", block, 1)

    tmp = _tmp_md(DDL_MD, add_rule)
    try:
        d = review("CREATE TABLE DEMO_T (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT 'x', "
                   "G GEOMETRY NOT NULL COMMENT 'y');", settings_with(ddl_path=tmp))["data"]
        check("TB-17" in rules_of(d),
              f"只改 ddl.md 新增 TB-17 → 立即生效（rules={sorted(rules_of(d))}）")
    finally:
        tmp.unlink(missing_ok=True)

    tmp = _tmp_md(DDL_MD, lambda md: md.replace("- **检查**：naming",
                                                "- **检查**：no_such_check", 1))
    try:
        rs, warn = get_ruleset(settings_with(ddl_path=tmp), "ddl", force=True)
        check(rs is None and bool(warn), f"文档写坏 → 加载失败并给出原因：{warn[:60]}")
    finally:
        tmp.unlink(missing_ok=True)

    # E7：热加载成功后会触发 on_reload（server 用它同步 healthz / 审计里的规则版本）
    from sql_review_ddl import reviewer as RV

    seen: list[tuple] = []
    RV.on_reload(lambda doc, rs, warn: seen.append((doc, getattr(rs, "sha256", "")[:8])))
    tmp = _tmp_md(DDL_MD, lower_tb03)
    try:
        rs, _warn = get_ruleset(settings_with(ddl_path=tmp), "ddl")
        check(any(d == "ddl" for d, _ in seen),
              f"热加载触发 on_reload 回调（{len(seen)} 次），healthz/审计才能同步到新规则")
        check(bool(rs) and rs.get("TB-03") is not None and rs.get("TB-03").level == "warn",
              "热加载后的规则集内容已是新版本（TB-03 = warn）")
    finally:
        tmp.unlink(missing_ok=True)

    # E9：单条检查抛异常时不再静默漏审，而是落一条 ⏸ 结论（ENGINE-03）
    import sql_review_ddl.ddlchecks as DC

    orig = DC.CHECKS["comments"]["func"]

    def _boom(res, rule):
        raise RuntimeError("selftest 注入异常")

    DC.CHECKS["comments"]["func"] = _boom
    try:
        d = review("CREATE TABLE DEMO_T (ID BIGINT NOT NULL AUTO_INCREMENT);",
                   base_settings())["data"]
        rules = rules_of(d)
        check("ENGINE-03" in rules, f"检查异常 → 落 ENGINE-03 未判定（rules={sorted(rules)}）")
        check(d["verdict"] == "need_more_info", f"异常不能当通过（verdict={d['verdict']}）")
    finally:
        DC.CHECKS["comments"]["func"] = orig


def _doc_driven_dml() -> None:
    def lower_dm10(md: str) -> str:
        i = md.find("### DM-10")
        j = md.find("- **级别**：❌", i)
        return md[:j] + "- **级别**：⚠️" + md[j + len("- **级别**：❌"):]

    tmp = _tmp_md(DML_MD, lower_dm10)
    try:
        d = review("UPDATE DEMO_ORDER SET ORDER_NO = 'A' WHERE ID = 1;",
                   settings_with(dml_path=tmp))["data"]
        check(d["verdict"] != "rejected",
              f"只改 dml.md 把 DM-10 降为 ⚠️ → 裸 UPDATE 不再 rejected（{d['verdict']}）")
    finally:
        tmp.unlink(missing_ok=True)

    tmp = _tmp_md(DML_MD, lambda md: md.replace("- **判定**：has_where = false",
                                                "- **判定**：no_such_fact = false", 1))
    try:
        rs, warn = get_ruleset(settings_with(dml_path=tmp), "dml", force=True)
        check(rs is None and bool(warn), f"dml.md 写坏 → 加载失败并给出原因：{warn[:60]}")
    finally:
        tmp.unlink(missing_ok=True)


def _doc_driven_version() -> None:
    # E8：config/STANDARD_VERSION 按 mtime 惰性重读（不用重启）
    from sql_review_ddl.reviewer import standard_version

    ver_file = Path(tempfile.mkdtemp(prefix="sr-ver-")) / "STANDARD_VERSION"
    ver_file.write_text("2026-11-01", encoding="utf-8")
    s_ver = settings_with(standard_version_file=ver_file)
    check(standard_version(s_ver) == "2026-11-01", "规范版本号首读正确")
    ver_file.write_text("2026-11-09", encoding="utf-8")
    import os as _os
    _os.utime(ver_file, (ver_file.stat().st_atime, ver_file.stat().st_mtime + 2))
    check(standard_version(s_ver) == "2026-11-09", "改 STANDARD_VERSION 后不重启即读到新版本")


# --------------------------------------------------------------------------
# F. 追问目录
# --------------------------------------------------------------------------


def test_questions() -> None:
    print("")
    print("=" * 78)
    print("F. 反问目录（config/questions.yaml 的话术，支持占位符）")
    print("=" * 78)
    if HAS_DDL:
        d = review("ALTER TABLE DEMO_ORDER MODIFY COLUMN ORDER_NO VARCHAR(64) NOT NULL "
                   "DEFAULT '' COMMENT '订单号';", base_settings())["data"]
        qs = {q["id"]: q for q in d["questions"]}
        q = qs.get("Q-CTX-STRUCT") or {}
        check(bool(q), "缺结构时给出 Q-CTX-STRUCT 反问")
        check("{" not in str(q.get("ask", "")),
              f"反问话术占位符已渲染（ask={str(q.get('ask'))[:40]}）")
    else:
        print("[SKIP] 本包不含 standards/ddl.md，跳过 Q-CTX-STRUCT（DDL 侧）话术校验")

    if HAS_DML:
        d = review("UPDATE DEMO_ORDER SET ORDER_NO = 'A' WHERE ID = 1;",
                   base_settings())["data"]
        qs = {q["id"]: q for q in d["questions"]}
        q = qs.get("Q-TRANSACTION") or {}
        check(bool(q), "裸 DML 命中 DM-10 时给出 Q-TRANSACTION 反问")
        check("{" not in str(q.get("ask", "")),
              f"DML 反问话术占位符已渲染（ask={str(q.get('ask'))[:40]}）")
    else:
        print("[SKIP] 本包不含 standards/dml.md，跳过 Q-TRANSACTION（DML 侧）话术校验")

    # F2：目录话术渲染闸门（不依赖 PyYAML —— 直接注入 qcatalog，覆盖 218 上才走到的分支）
    from sql_review_ddl.common import ReviewResult

    r1 = ReviewResult(qcatalog={"Q-X": {"ask": "字段 {column} 的原类型无法比对"}})
    r1.ask("Q-X", "AL-03", {"column": "ORDER_NO"}, fallback="兜底话术")
    a1 = r1.questions[0].ask
    check("ORDER_NO" in a1 and "{" not in a1, f"目录话术按字段渲染（ask={a1[:40]}）")

    r2 = ReviewResult(qcatalog={"Q-X": {"ask": "字段 {column} 的原类型无法比对"}})
    r2.ask("Q-X", "AL-03", fallback="兜底话术：请传 env + database")   # 故意不传 fields
    a2 = r2.questions[0].ask
    check("{" not in a2 and "兜底话术" in a2,
          f"话术残留占位符时自动回落 fallback（ask={a2[:40]}）")

    # F3：questions.yaml 里的话术不得包含"目录未声明、代码也不会传"的占位符
    try:
        from sql_review_ddl.config import load_questions
        cat = {q["id"]: q for q in ((load_questions(base_settings()) or {}).get("questions") or [])}
        if cat:
            bads = [f"{qid}:{ph}" for qid, item in cat.items()
                    for ph in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", str(item.get("ask", "")))
                    if ph not in ("name", "column", "table", "columns", "length", "old",
                                  "new", "values", "items", "index")]
            check(not bads, f"questions.yaml 占位符均在白名单内（异常 {bads}）")
        else:
            print("[SKIP] PyYAML 不可用，反问目录未加载（服务器上会执行本项）")
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] 反问目录校验跳过：{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """跑全部自检；deploy.sh 会传 --report <path>，输出同时写一份报告。"""
    import io
    import sys as _sys

    argv = list(_sys.argv[1:] if argv is None else argv)
    report_path = ""
    if "--report" in argv:
        i = argv.index("--report")
        if i + 1 < len(argv):
            report_path = argv[i + 1]
    buf = io.StringIO()
    real_print = print

    def _tee(*args, **kwargs):
        kwargs.pop("file", None)          # 调用方若指定 file 也统一写到报告里
        real_print(*args, **kwargs)
        buf.write(" ".join(str(a) for a in args) + "\n")   # 不能再用 print（已被替换，会递归）

    globals()["print"] = _tee
    try:
        rc = _run_all()
    finally:
        globals()["print"] = real_print
    if report_path:
        from pathlib import Path as _Path
        try:
            _Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            _Path(report_path).write_text(buf.getvalue(), encoding="utf-8")
            real_print(f"（自检报告已写入 {report_path}）")
        except OSError as exc:
            real_print(f"（自检报告写入失败：{exc}）")
    return rc


def _run_all() -> int:
    print(f"本包规范：ddl.md={'有' if HAS_DDL else '无'}　dml.md={'有' if HAS_DML else '无'}"
          "（缺的一侧由另一个 SQL Review 服务负责，对应小节打印 [SKIP]）")
    test_specs()
    test_reject_broken()
    test_ddl()
    test_dml()
    test_doc_driven()
    test_questions()
    print("")
    print("=" * 78)
    total = PASS + FAIL
    if FAIL:
        print(f"❌ 自检失败：{PASS}/{total} 通过，{FAIL} 项失败")
        return 1
    print(f"✅ 自检全部通过：{PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
