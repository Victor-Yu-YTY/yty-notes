"""目标环境预检：只读，不写任何数据。

用途：库白名单已从代码层删除，库级隔离完全由 MySQL 账号的 GRANT 承担。
上线或本机联调前，先用本脚本实测每个环境的：

  ① 能否连通         ② 账号是谁       ③ 授权到哪些库（有没有 *.* 全局授权）
  ④ 必需权限齐不齐   ⑤ 有没有危险权限  ⑥ 实例是否只读
  ⑦ information_schema 能不能读        ⑧ 会话参数能不能设

判定标准：
  ✅ ok     ⚠️ 需要人工确认但可继续     ❌ 必须先解决，否则不要开 execute

用法：
    python scripts/probe_targets.py                 # 全部环境
    python scripts/probe_targets.py --env dev       # 单个环境
    python scripts/probe_targets.py --json          # 机器可读
    python scripts/probe_targets.py --show-dbs      # 额外列出账号可见的库

密码从环境变量注入（${EXEC_PASSWORD}，见 config/targets.yaml），脚本不会打印密码。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import pymysql  # type: ignore
except Exception:  # noqa: BLE001
    print("未安装 pymysql，无法预检：pip install pymysql==1.2.3", file=sys.stderr)
    raise SystemExit(2)

from sql_executor_mcp.config import Settings  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# executor 真正需要的权限（设计 §8.4）
REQUIRED = {"SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"}
# 绝对不能有的：回滚是 DBA 人工执行，执行账号持有这些等于把兜底自己拆了
FATAL = {"DELETE", "DROP", "ALL PRIVILEGES", "GRANT OPTION"}
# 有也不致命、但要人工确认的
WARN = {"SUPER", "FILE", "PROCESS", "RELOAD", "SHUTDOWN", "CREATE USER",
        "CREATE TEMPORARY TABLES", "TRIGGER", "EVENT", "REFERENCES"}

OK, WARN_MARK, BAD = "ok", "warn", "bad"


def _var(cur, name: str):
    """单独取一个系统变量，不存在就返回 None（别让一个变量拖垮整段检查）。"""
    try:
        cur.execute(f"SELECT @@{name} AS v")
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def parse_grants(lines: list[str]) -> tuple[set[str], set[str], list[str]]:
    """拆 SHOW GRANTS：返回 (权限集合, 授权到的库, 全局授权告警)。"""
    privs: set[str] = set()
    dbs: set[str] = set()
    notes: list[str] = []
    for line in lines:
        m = re.match(r"^GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+", line, re.I)
        if not m:
            if line.upper().startswith("GRANT PROXY"):
                notes.append("存在 GRANT PROXY 授权")
            continue
        priv_part, scope = m.group(1), m.group(2).strip()
        items = {p.strip().upper() for p in priv_part.split(",") if p.strip()}
        privs |= items
        if scope == "*.*":
            if items - {"USAGE"}:
                notes.append("授权范围是 *.* （全局）：库级隔离失效")
        else:
            dm = re.match(r"`?([^`.]+)`?\.", scope)
            if dm:
                dbs.add(dm.group(1))
    return privs, dbs, notes


def probe_env(env: str, cfg: dict, show_dbs: bool = False) -> dict:
    out: dict = {"env": env, "checks": [], "grants": [], "databases": []}

    def chk(name: str, state: str, detail: str = ""):
        out["checks"].append({"name": name, "state": state, "detail": detail})

    if not cfg:
        chk("配置存在", BAD, f"targets.yaml 里没有 {env} 段")
        return out
    missing = [k for k in ("host", "user", "password") if not cfg.get(k)]
    if "password" in missing:
        chk("密码已注入", BAD,
            f"环境变量 {ENV_HINT.get(env, 'EXEC_PASSWORD')} 为空"
            f"（targets.yaml 里 {env} 的密码占位符取不到值；"
            f"先在当前 shell 执行：set -a; . /etc/sql-executor-mcp/sql-executor-mcp.env; set +a）")
        return out
    if missing:
        chk("配置完整", BAD, f"缺少 {missing}")
        return out

    out["target"] = f"{cfg['user']}@{cfg['host']}:{cfg.get('port', 3306)}"

    try:
        conn = pymysql.connect(
            host=cfg["host"], port=int(cfg.get("port", 3306)),
            user=cfg["user"], password=cfg["password"],
            charset=cfg.get("charset", "utf8mb4"),
            connect_timeout=5, read_timeout=10, write_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        chk("连通性", BAD, f"{type(exc).__name__}: {exc}")
        return out
    chk("连通性", OK)

    try:
        with conn.cursor() as cur:
            out["version"] = _var(cur, "version")
            out["hostname"] = _var(cur, "hostname")
            out["read_only"] = _var(cur, "read_only")
            out["super_read_only"] = _var(cur, "super_read_only")
            out["lower_case_table_names"] = _var(cur, "lower_case_table_names")
            out["autocommit"] = _var(cur, "autocommit")

            cur.execute("SELECT CURRENT_USER() AS u")
            row = cur.fetchone()
            out["current_user"] = row[0] if row else None
            chk("账号身份", OK, str(out["current_user"]))

            try:
                cur.execute("SHOW GRANTS")
                grants = [r[0] for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                grants = []
                chk("SHOW GRANTS", BAD, f"{type(exc).__name__}: {exc}")
            out["grants"] = grants

            privs, dbs, notes = parse_grants(grants)
            out["privileges"] = sorted(privs)
            out["granted_databases"] = sorted(dbs)

            lack = sorted(REQUIRED - privs)
            chk("必需权限", BAD if lack else OK,
                f"缺少 {lack}" if lack else "、".join(sorted(REQUIRED)))

            fatal = sorted(privs & FATAL)
            chk("危险权限", BAD if fatal else OK,
                f"不该有：{fatal}" if fatal else "无 DELETE / DROP / ALL")

            warns = sorted(privs & WARN)
            chk("待确认权限", WARN_MARK if warns else OK,
                f"存在：{warns}，确认是否必要" if warns else "无")

            if dbs and not any("*" in n for n in notes):
                chk("库级隔离", OK, f"授权到 {len(dbs)} 个库：{sorted(dbs)}")
            elif any("*.*" in n or "全局" in n for n in notes):
                chk("库级隔离", WARN_MARK,
                    "授权为 *.* 全局 —— 代码层已无白名单，越库写只能靠账号，建议改成按库授权")
            else:
                chk("库级隔离", WARN_MARK, "未从 GRANTS 解析出具体库，请人工核对")

            for n in notes:
                chk("GRANTS 备注", WARN_MARK, n)

            if out["read_only"] or out["super_read_only"]:
                chk("实例可写", BAD, f"read_only={out['read_only']} "
                                   f"super_read_only={out['super_read_only']}")
            else:
                chk("实例可写", OK)

            try:
                cur.execute("SELECT 1 FROM information_schema.SCHEMATA LIMIT 1")
                cur.fetchone()
                chk("information_schema", OK)
            except Exception as exc:  # noqa: BLE001
                chk("information_schema", BAD, f"{type(exc).__name__}: {exc}")

            try:
                cur.execute("SET SESSION lock_wait_timeout=10")
                chk("会话参数", OK, "lock_wait_timeout 可设")
            except Exception as exc:  # noqa: BLE001
                chk("会话参数", WARN_MARK, f"设不上：{type(exc).__name__}: {exc}")

            if show_dbs:
                try:
                    cur.execute("SHOW DATABASES")
                    out["databases"] = [r[0] for r in cur.fetchall()]
                except Exception:  # noqa: BLE001
                    pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    return out


# targets.yaml 里 dev/test/stage 共用同一个密码占位符 ${EXEC_PASSWORD}；
# 若某环境改用独立变量（如 ${EXEC_TEST_PASSWORD}），把这里改成对应名字。
ENV_HINT = {
    "dev": "EXEC_PASSWORD",
    "test": "EXEC_PASSWORD",
    "stage": "EXEC_PASSWORD",
}

_ICON = {OK: f"{GREEN}✅{RESET}", WARN_MARK: f"{YELLOW}⚠️ {RESET}", BAD: f"{RED}❌{RESET}"}


def render(res: dict, verbose: bool = True) -> str:
    lines = [f"\n{'=' * 74}", f"环境 {res['env']}　{res.get('target') or '-'}", f"{'=' * 74}"]
    if res.get("version"):
        lines.append(f"{DIM}MySQL {res['version']}｜hostname {res.get('hostname')}"
                     f"｜lower_case_table_names={res.get('lower_case_table_names')}{RESET}")
    for c in res["checks"]:
        icon = _ICON[c["state"]]
        detail = f"  {DIM}{c['detail']}{RESET}" if c["detail"] else ""
        lines.append(f"{icon} {c['name']}{detail}")
    if verbose and res.get("grants"):
        lines.append(f"{DIM}GRANTS：{RESET}")
        for g in res["grants"]:
            lines.append(f"{DIM}  {g}{RESET}")
    if res.get("databases"):
        lines.append(f"{DIM}可见库（SHOW DATABASES）：{', '.join(res['databases'])}{RESET}")
    bad = sum(1 for c in res["checks"] if c["state"] == BAD)
    warn = sum(1 for c in res["checks"] if c["state"] == WARN_MARK)
    tail = f"{GREEN}可以开始测试{RESET}" if not bad else f"{RED}先解决 ❌ 项{RESET}"
    lines.append(f"小结：❌ {bad}　⚠️ {warn}　→ {tail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sql-executor-mcp 目标环境预检（只读）")
    ap.add_argument("--env", default="", help="只查某个环境，留空查全部")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-dbs", action="store_true", help="额外列出账号可见的库")
    ap.add_argument("--quiet", action="store_true", help="不打印 GRANTS 原文")
    args = ap.parse_args(argv)

    s = Settings.load()
    targets = (s.load_targets().get("environments") or {})
    envs = [args.env] if args.env else list(targets.keys())
    if not envs:
        print("targets.yaml 里没有任何环境", file=sys.stderr)
        return 2

    results = []
    for env in envs:
        results.append(probe_env(env, targets.get(env) or {}, args.show_dbs))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(render(r, verbose=not args.quiet))
        bad = [r["env"] for r in results
               if any(c["state"] == BAD for c in r["checks"])]
        print(f"\n{'=' * 74}")
        if bad:
            print(f"{RED}有问题的环境：{', '.join(bad)}{RESET}")
        else:
            print(f"{GREEN}全部环境通过预检{RESET}")

    bad = [r for r in results if any(c["state"] == BAD for c in r["checks"])]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
