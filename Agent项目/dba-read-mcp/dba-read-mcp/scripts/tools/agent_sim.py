"""模拟「网关侧调用 MCP」：直接用准入令牌打本机回环端口，验证 9 个工具。

v0.2.4 起鉴权只剩一层（准入令牌），所以本脚本不再模拟"研发带 Bearer 凭证"，
而是模拟**网关转发**：只带 X-DB-READ-MCP-TOKEN，并且额外做两项反向检查
（不带令牌 / 错令牌都必须被 HTTP 403 挡在协议层之前）。

用法：
    # 只验鉴权与拦截（不需要数据库）
    PYTHONPATH=src .venv/bin/python scripts/tools/agent_sim.py --auth-only

    # 全量：查库表字段 / 只读查询 / EXPLAIN / 只读防线
    PYTHONPATH=src .venv/bin/python scripts/tools/agent_sim.py --all

    # 单条
    PYTHONPATH=src .venv/bin/python scripts/tools/agent_sim.py \
        --tool describe_table --params '{"env":"test","database":"ai_agent","table":"CHAT_MESSAGE"}'

令牌来源优先级：--token > 环境变量 DBA_MCP_GATEWAY_TOKEN > /etc/dba-mcp/dba-mcp.env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

DEFAULT_URL = os.environ.get("DBA_MCP_URL", "http://127.0.0.1:8000/mcp/")
ENV_FILE = os.environ.get("DBA_MCP_ENV_FILE", "/etc/dba-mcp/dba-mcp.env")
TOKEN_HEADER = "X-DB-READ-MCP-TOKEN"

DB_ERROR_HINTS = ("OperationalError", "Can't connect", "Unknown database", "Access denied", "timed out", "TOOL_ERROR")

# expect: ok=应成功 / reject=应被业务拒绝（SQL 拦截等）；need_db=True 需要真实库
SCENARIOS: list[dict[str, Any]] = [
    {"ask": "有哪些环境可以用", "tool": "list_environments", "args": {}, "expect": "ok", "need_db": False},
    {"ask": "测试环境有哪些数据库", "tool": "list_databases", "args": {"env": "test"}, "expect": "ok", "need_db": True},
    {"ask": "某库有哪些表", "tool": "list_tables", "args": {"env": "test", "database": "ai_agent"},
     "expect": "ok", "need_db": True},
    {"ask": "表结构（字段+索引）", "tool": "describe_table",
     "args": {"env": "test", "database": "ai_agent", "table": "CHAT_MESSAGE"}, "expect": "ok", "need_db": True},
    {"ask": "建表语句", "tool": "get_create_table",
     "args": {"env": "test", "database": "ai_agent", "table": "CHAT_MESSAGE"}, "expect": "ok", "need_db": True},
    {"ask": "按字段名反查", "tool": "search_column", "args": {"column_name": "mobile", "env": "test"},
     "expect": "ok", "need_db": True},
    {"ask": "只读 SELECT", "tool": "run_select_query",
     "args": {"env": "test", "database": "ai_agent", "sql": "SELECT 1 AS ok"},
     "expect": "ok", "need_db": True},
    {"ask": "执行计划", "tool": "explain_query",
     "args": {"env": "test", "sql": "SELECT 1"}, "expect": "ok", "need_db": True},
    {"ask": "刷新元数据缓存", "tool": "refresh_metadata", "args": {"env": "test"}, "expect": "ok", "need_db": True},
    # 只读防线（不需要真实库也能被拦）
    {"ask": "删除数据应被拒", "tool": "run_select_query", "args": {"env": "test", "sql": "DELETE FROM t"},
     "expect": "reject", "expect_code": "SQL_REJECTED", "need_db": False},
    {"ask": "多语句应被拒", "tool": "run_select_query",
     "args": {"env": "test", "sql": "SELECT 1; SELECT 2"}, "expect": "reject",
     "expect_code": "SQL_REJECTED", "need_db": False},
    {"ask": "系统库应被拒", "tool": "run_select_query",
     "args": {"env": "test", "sql": "SELECT * FROM mysql.user"}, "expect": "reject",
     "expect_code": "SQL_REJECTED", "need_db": False},
]


def load_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token
    if os.environ.get("DBA_MCP_GATEWAY_TOKEN"):
        return os.environ["DBA_MCP_GATEWAY_TOKEN"]
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("GATEWAY_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _transport(url: str, token: str):
    from fastmcp.client.transports import StreamableHttpTransport

    headers = {TOKEN_HEADER: token} if token else {}
    return StreamableHttpTransport(url=url, headers=headers)


async def check_gate(url: str, token: str) -> int:
    """反向检查：无令牌 / 错令牌必须被 403 拦在协议层之前。"""
    from fastmcp import Client

    failed = 0
    for label, tok, expect_ok in (("无准入令牌", "", False), ("错准入令牌", "bad-token", False)):
        try:
            async with Client(_transport(url, tok)) as client:
                await client.call_tool("list_environments", {})
            if expect_ok:
                print(f"[OK ] {label} -> 放行（预期）")
            else:
                print(f"[NG ] {label} -> 竟然握手成功，准入门没拦住！")
                failed += 1
        except Exception as exc:  # 403 会在握手阶段抛出
            text = f"{type(exc).__name__}: {exc}"
            if (not expect_ok) and ("403" in text or "unauthorized" in text.lower() or "forbidden" in text.lower()):
                print(f"[OK ] {label} -> 被拦在协议层之前（403）")
            else:
                print(f"[NG ] {label} -> 异常类型不符合预期：{text[:120]}")
                failed += 1
    return failed


async def call_mcp(url: str, token: str, tool: str, args: dict) -> dict:
    from fastmcp import Client

    async with Client(_transport(url, token)) as client:
        result = await client.call_tool(tool, args)
        text = result.content[0].text if result.content else ""
        return json.loads(text)


def _summarize(payload: dict) -> str:
    data = payload.get("data") or {}
    if "environments" in data:
        return f"{data.get('count', 0)} 个环境"
    if "databases" in data:
        return f"{data.get('count', 0)} 个库"
    if "tables" in data:
        return f"{data.get('count', 0)} 张表"
    if "columns" in data:
        names = [c["name"] for c in data.get("columns", [])]
        shown = ", ".join(names[:8]) + ("..." if len(names) > 8 else "")
        return f"{data.get('column_count', 0)} 个字段 [{shown}]"
    if "rows" in data:
        return f"{data.get('row_count', 0)} 行"
    if "plan" in data:
        return f"{len(data.get('plan', []))} 行执行计划"
    if "matches" in data:
        return f"{data.get('count', 0)} 处匹配"
    if "ddl" in data:
        return f"DDL {len(str(data.get('ddl', '')))} 字符"
    return ""


def _classify(payload: dict) -> tuple[str, str]:
    if payload.get("ok"):
        return "PASS", _summarize(payload)
    err = payload.get("error") or {}
    code = err.get("code", "?")
    msg = str(err.get("message", ""))
    if any(h in msg for h in DB_ERROR_HINTS):
        return "NEED_DB", f"{code}: {msg[:70]}"
    return "FAIL", f"{code}: {msg[:70]}"


async def run_scenarios(scenarios, url, token, skip_db) -> int:
    passed = failed = need_db = 0
    for idx, sc in enumerate(scenarios, 1):
        if skip_db and sc.get("need_db"):
            need_db += 1
            print(f"[{idx:>2}] SKIP（需真实库）: {sc['ask']}")
            continue
        try:
            payload = await call_mcp(url, token, sc["tool"], sc["args"])
        except Exception as exc:
            print(f"[{idx:>2}] ❌ {sc['ask']}\n     调用异常: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        state, detail = _classify(payload)
        expect = sc.get("expect", "ok")
        expect_code = sc.get("expect_code")
        err_code = (payload.get("error") or {}).get("code")

        if expect == "ok":
            if state == "PASS":
                print(f"[{idx:>2}] ✅ {sc['ask']} —— {detail}")
                passed += 1
            elif state == "NEED_DB":
                print(f"[{idx:>2}] ⏭  {sc['ask']} —— 环境未就绪: {detail}")
                need_db += 1
            else:
                print(f"[{idx:>2}] ❌ {sc['ask']} —— {detail}")
                failed += 1
        else:
            if state == "FAIL" and (expect_code is None or err_code == expect_code):
                print(f"[{idx:>2}] ✅ {sc['ask']} —— 按预期被拒（{err_code}）")
                passed += 1
            else:
                print(f"[{idx:>2}] ❌ {sc['ask']} —— 未被拒绝: {detail}")
                failed += 1

    print("\n--------------------------------------------------")
    print(f"通过 {passed} / 失败 {failed} / 需真实库 {need_db}")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_sim", description="模拟网关侧调用 db-read-mcp（只带准入令牌）")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"MCP 入口（默认 {DEFAULT_URL}，结尾斜杠不可省）")
    ap.add_argument("--token", default=None, help="准入令牌；默认从 env 文件读")
    ap.add_argument("--all", action="store_true", help="跑全部场景（需要真实库）")
    ap.add_argument("--skip-db", action="store_true", help="跳过需要真实库的场景")
    ap.add_argument("--auth-only", action="store_true", help="只跑鉴权三态检查")
    ap.add_argument("--tool", default=None, help="只调一个工具")
    ap.add_argument("--params", default="{}", help="--tool 的入参 JSON")
    args = ap.parse_args(argv)

    token = load_token(args.token)
    if not token:
        print("找不到准入令牌：用 --token 指定，或设置 DBA_MCP_GATEWAY_TOKEN", file=sys.stderr)
        return 2

    if args.tool:
        payload = asyncio.run(call_mcp(args.url, token, args.tool, json.loads(args.params)))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1

    print(f"入口: {args.url}")
    print("一、准入令牌三态（反向检查：无/错令牌必须 403）")
    gate_failed = asyncio.run(check_gate(args.url, token))
    if args.auth_only:
        return 0 if gate_failed == 0 else 1

    print("\n二、工具场景")
    rc = asyncio.run(run_scenarios(SCENARIOS, args.url, token, skip_db=args.skip_db or not args.all))
    return rc if rc else (0 if gate_failed == 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main())
