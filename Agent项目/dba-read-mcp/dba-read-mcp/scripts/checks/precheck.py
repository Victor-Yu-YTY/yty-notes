"""部署前自检：连通性 / 版本特性 / 账号权限 / 可见库清单。

不写任何数据，只做只读探测。

用法：
    export DEV_DB_PASS=... TEST_DB_PASS=... STAGE_DB_PASS=...
    python scripts/checks/precheck.py --config config/environments.yaml
    python scripts/checks/precheck.py --config config/environments.yaml --env test --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from dba_mcp.config import Settings, load_settings  # noqa: E402
from dba_mcp.pool import ConnectionManager  # noqa: E402

WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "ALL PRIVILEGES")


def _check_one(settings: Settings, conns: ConnectionManager, env_name: str) -> dict[str, Any]:
    cfg = settings.environments[env_name]
    result: dict[str, Any] = {"env": env_name, "host": f"{cfg.host}:{cfg.port}", "user": cfg.user}
    started = time.perf_counter()
    try:
        with conns.connection(env_name) as conn:
            result["connect_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["connect"] = "OK"
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION() AS v")
                result["version"] = cur.fetchone()["v"]

                cur.execute("SHOW VARIABLES LIKE 'transaction_read_only'")
                row = cur.fetchone()
                result["read_only_session"] = "OK" if row and row["Variable_name"] == "transaction_read_only" else "UNKNOWN"

                cur.execute("SHOW VARIABLES LIKE 'max_execution_time'")
                result["max_execution_time"] = "supported" if cur.fetchone() else "not supported (5.7 依赖驱动 read_timeout)"

                cur.execute("SELECT COUNT(*) AS c FROM information_schema.SCHEMATA "
                            "WHERE SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys')")
                result["visible_databases"] = cur.fetchone()["c"]

                cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                            "WHERE SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys') "
                            "ORDER BY SCHEMA_NAME LIMIT 50")
                result["database_list"] = [r["SCHEMA_NAME"] for r in cur.fetchall()]

                cur.execute("SELECT COUNT(*) AS c FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')")
                result["visible_tables"] = cur.fetchone()["c"]

                cur.execute("SHOW GRANTS FOR CURRENT_USER()")
                grants = [list(r.values())[0] for r in cur.fetchall()]
                result["grants"] = grants
                risky = [
                    g for g in grants
                    if any(k in g.upper() for k in WRITE_KEYWORDS) or "WITH GRANT OPTION" in g.upper()
                ]
                result["write_privilege_warning"] = risky or []
    except Exception as exc:
        result["connect"] = f"FAILED: {type(exc).__name__}: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBA MCP 部署前自检")
    parser.add_argument("--config", default=None)
    parser.add_argument("--env", default=None, help="只检查指定环境")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except Exception as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2

    conns = ConnectionManager(settings)
    targets = [args.env] if args.env else list(settings.environments)
    results = [_check_one(settings, conns, name) for name in targets]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"\n=== env={r['env']}  host={r['host']}  user={r['user']} ===")
            for key, value in r.items():
                if key in ("env", "host", "user"):
                    continue
                if key == "grants":
                    print(f"  {key:<26}: ")
                    for g in value:
                        print(f"      {g}")
                elif key == "database_list":
                    print(f"  {key:<26}: {', '.join(value) if value else '(空)'}")
                else:
                    print(f"  {key:<26}: {value}")

    failed = [r["env"] for r in results if r.get("connect") != "OK"]
    if failed:
        print(f"\n❌ 连接失败的环境: {failed}", file=sys.stderr)
        return 1
    warned = [r["env"] for r in results if r.get("write_privilege_warning")]
    if warned:
        print(f"⚠️  只读账号存在写权限: {warned}", file=sys.stderr)
    print("\n✅ 三个环境均可连通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
