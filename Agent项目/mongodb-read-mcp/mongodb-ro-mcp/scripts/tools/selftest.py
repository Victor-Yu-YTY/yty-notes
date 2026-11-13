#!/usr/bin/env python3
"""工具面自检（离线断言 + 可选真连）：工具注册、环境别名、只读防线、审计字段。

用法（安装目录下，venv 解释器）：
    set -a; . /etc/mongodb-ro-mcp/mongodb-ro-mcp.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/tools/selftest.py            # 含真连（连不上标 SKIP）
    PYTHONPATH=src .venv/bin/python scripts/tools/selftest.py --offline  # 只做离线断言

判定：全部 ✅ / ⚠️(SKIP) 即通过；任何 ❌ 视为失败，退出码 1。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # <包根>/scripts/tools/selftest.py
sys.path.insert(0, str(ROOT / "src"))

FAILED: list[str] = []
SKIPPED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("%s %s%s" % ("✅" if ok else "❌", label, ("  " + detail) if detail else ""))
    if not ok:
        FAILED.append(label)


def skip(label: str, detail: str = "") -> None:
    print("⚠️  %s（SKIP）%s" % (label, ("  " + detail) if detail else ""))
    SKIPPED.append(label)


def _tool_names(server) -> list[str]:
    """取已注册工具名。

    fastmcp 2.11.x 只有 `await server.get_tools()`（返回 dict）；
    早期/其它版本可能只有 `await server.list_tools()`（返回 list[Tool]）。
    两种都兼容，避免自检脚本被框架小版本升级打挂。
    """
    getter = getattr(server, "get_tools", None)
    if getter is not None:
        return [str(k) for k in asyncio.run(getter()).keys()]
    lister = getattr(server, "list_tools", None)
    if lister is not None:
        return [str(t.name) for t in asyncio.run(lister())]
    raise RuntimeError("当前 fastmcp 版本既没有 get_tools() 也没有 list_tools()")


def main() -> int:
    ap = argparse.ArgumentParser(description="mongodb-ro-mcp 工具面自检")
    ap.add_argument("--config", default=None, help="environments.yaml 路径")
    ap.add_argument("--offline", action="store_true", help="跳过所有真连检查")
    args = ap.parse_args()

    try:
        from mongodb_ro_mcp import server as S
        from mongodb_ro_mcp import mongo_guard as G
        from mongodb_ro_mcp.config import ConfigError
        settings = S.init_state(args.config or str(ROOT / "config" / "environments.yaml"))
    except ConfigError as exc:
        print("❌ 配置加载失败: %s" % exc)
        print("   先注入密钥：set -a; . /etc/mongodb-ro-mcp/mongodb-ro-mcp.env; set +a")
        return 2
    except Exception as exc:                        # noqa: BLE001
        print("❌ 导入失败: %s: %s" % (type(exc).__name__, exc))
        return 2

    # ---- 1. 工具面 ----
    print("== 1. 工具面 ==")
    names = sorted(_tool_names(S.mcp))
    check("工具注册 13 个", len(names) == 13, "实际 %d: %s" % (len(names), ", ".join(names)))
    for must in ("list_environments", "find_documents", "aggregate", "explain",
                 "current_operations", "server_status", "replica_status"):
        check("含 %s" % must, must in names)

    # ---- 2. 环境与别名 ----
    print("\n== 2. 环境路由 ==")
    check("环境解析", bool(settings.environments), "可选: %s" % settings.names())
    for name in settings.names():
        env = settings.environments[name]
        check("环境 %s 有连接串" % name, bool(env.uri),
              "actual=%s aliases=%s" % (env.actual, env.aliases or "无"))
    try:
        stage = settings.env("stage")
        check("stage → mirror 别名", stage.actual == "mirror", "stage 解析到 %s" % stage.actual)
    except Exception as exc:                        # noqa: BLE001
        skip("stage → mirror 别名", "配置里没有 stage 别名（当前：%s）" % exc)

    try:
        settings.env("__not_exist__")
        check("未知环境拒绝", False, "未拦截")
    except Exception as exc:                        # noqa: BLE001
        check("未知环境拒绝", "可选" in str(exc), str(exc)[:80])

    # ---- 3. 只读防线 ----
    print("\n== 3. 只读防线 ==")
    forbidden = settings.guard.forbidden_operators
    for label, payload in (
        ("$out 拦截", [{"$out": "x"}]),
        ("$merge 拦截", [{"$merge": {"into": "x"}}]),
        ("$where 拦截", [{"$match": {"$where": "1==1"}}]),
        ("嵌套 $out 拦截（$facet 内）", [{"$facet": {"a": [{"$out": "x"}]}}]),
        ("$lookup 子管道 $merge 拦截", [{"$lookup": {"from": "y", "pipeline": [{"$merge": {"into": "z"}}], "as": "r"}}]),
    ):
        try:
            G.guard_pipeline(payload, forbidden)
            check(label, False, "未拦截")
        except G.GuardError:
            check(label, True)

    for label, flt in (
        ("filter $where 拦截", {"$where": "1==1"}),
        ("filter $function 拦截", {"$expr": {"$function": {}}}),
    ):
        try:
            G.guard_filter(flt, forbidden)
            check(label, False, "未拦截")
        except G.GuardError:
            check(label, True)

    for good in ([{"$match": {"a": 1}}, {"$group": {"_id": "$a"}}],):
        try:
            G.guard_pipeline(good, forbidden)
            check("正常管道放行", True)
        except G.GuardError as exc:
            check("正常管道放行", False, str(exc))

    try:
        G.check_db_allowed("admin", settings.guard.forbidden_dbs)
        check("admin 库拒绝", False, "未拦截")
    except G.GuardError:
        check("admin 库拒绝", True)

    for bad in ("a b", "a;b", "", "x" * 200):
        try:
            G.check_ident("collection", bad)
            check("非法集合名拒绝 %r" % bad[:8], False, "未拦截")
        except G.GuardError:
            check("非法集合名拒绝 %r" % bad[:8], True)

    # ---- 4. 输出控制 ----
    print("\n== 4. 输出控制 ==")
    big = {"s": "字" * 5000}
    shrunk = G.dumps(big, settings.guard.max_field_chars)
    check("超长字段截断", "截断" in shrunk, "长度 %d" % len(shrunk))
    clamped = G.clamp_bytes("字" * 100000, 1000)
    check("字节上限截断", len(clamped.encode("utf-8")) <= 1000 + 64, "字节 %d" % len(clamped.encode("utf-8")))
    docs, note, truncated = G.summarize_docs(list(range(21)), 20)
    check("limit+1 溢出提示", truncated and len(docs) == 20, note)

    # ---- 5. 审计 ----
    print("\n== 5. 审计 ==")
    from mongodb_ro_mcp import audit as A
    policy = A.AuditPolicy.from_config(settings.audit)
    sha, length = A.fingerprint({"a": 1})
    check("查询指纹只落 sha256/长度", len(sha) == 64 and length > 0, "len=%d" % length)
    payload = A.record(settings.audit.dir, policy, event="selftest", tool="selftest",
                       outcome="ok", env="test", env_actual="test",
                       client_ip="10.0.0.1", agent_id="agent-x",
                       socket_ip="10.0.0.219", client_ip_source="X-Forwarded-For",
                       agent_id_source="X-Agent-Id", identity_trusted=True)
    for field in ("client_ip", "agent_id", "socket_ip", "client_ip_source",
                  "agent_id_source", "identity_trusted", "query_sha256", "reject"):
        check("审计字段 %s" % field, field in payload)

    # ---- 6. 真连（可选） ----
    if args.offline:
        print("\n== 6. 真连（--offline 跳过）==")
    else:
        print("\n== 6. 真连 ==")
        for name in settings.names():
            try:
                info = S._conns().probe(name)
                if info.get("reachable"):
                    check("list_environments(%s)" % name,
                          bool(info.get("server_version")),
                          "版本 %s / 副本集 %s / wire %s"
                          % (info.get("server_version"), info.get("setName"),
                             info.get("maxWireVersion")))
                else:
                    skip("list_environments(%s)" % name, info.get("error", "")[:120])
            except Exception as exc:                # noqa: BLE001
                skip("list_environments(%s)" % name, "%s: %s" % (type(exc).__name__, exc))

    print()
    if FAILED:
        print("失败项: %s" % FAILED)
        if SKIPPED:
            print("跳过的真连项: %s" % SKIPPED)
        return 1
    print("全部通过%s" % ("（含 %d 项 SKIP）" % len(SKIPPED) if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
