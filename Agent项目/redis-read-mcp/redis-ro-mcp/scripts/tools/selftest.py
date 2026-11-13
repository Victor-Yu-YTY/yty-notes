#!/usr/bin/env python3
"""工具面自检（离线断言 + 可选真连）：工具注册、环境别名、只读防线、审计字段。

用法（安装目录下，venv 解释器）：
    set -a; . /etc/redis-ro-mcp/redis-ro-mcp.env; set +a
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

# 期望的工具面（11 个，与 README §1 一一对应）
EXPECTED_TOOLS = (
    "list_environments", "scan_keys", "find_keys", "key_detail",
    "read_string", "read_hash", "read_list", "read_set", "read_zset",
    "read_zset_by_score", "bigkey_scan",
)


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
    ap = argparse.ArgumentParser(description="redis-ro-mcp 工具面自检")
    ap.add_argument("--config", default=None, help="environments.yaml 路径")
    ap.add_argument("--offline", action="store_true", help="跳过所有真连检查")
    args = ap.parse_args()

    try:
        from redis_ro_mcp import server as S
        from redis_ro_mcp import redis_guard as G
        from redis_ro_mcp.config import ConfigError
        settings = S.init_state(args.config or str(ROOT / "config" / "environments.yaml"))
    except ConfigError as exc:
        print("❌ 配置加载失败: %s" % exc)
        print("   先注入密钥：set -a; . /etc/redis-ro-mcp/redis-ro-mcp.env; set +a")
        return 2
    except Exception as exc:                        # noqa: BLE001
        print("❌ 导入失败: %s: %s" % (type(exc).__name__, exc))
        return 2

    # ---- 1. 工具面 ----
    print("== 1. 工具面 ==")
    names = sorted(_tool_names(S.mcp))
    check("工具注册 11 个", len(names) == 11, "实际 %d: %s" % (len(names), ", ".join(names)))
    for must in EXPECTED_TOOLS:
        check("含 %s" % must, must in names)

    # ---- 2. 环境与别名 ----
    print("\n== 2. 环境路由 ==")
    check("环境解析", bool(settings.environments), "可选: %s" % settings.names())
    for name in settings.names():
        env = settings.environments[name]
        check("环境 %s 有连接地址" % name, bool(env.host and env.port),
              "%s actual=%s aliases=%s auth=%s"
              % (env.endpoint, env.actual, env.aliases or "无",
                 "yes" if env.has_auth else "no"))
    for name in settings.names():
        env = settings.environments[name]
        if env.protocol != 2:
            check("环境 %s protocol=2（Redis 5 无 HELLO）" % name, False,
                  "当前 protocol=%s" % env.protocol)
        else:
            check("环境 %s protocol=2（Redis 5 无 HELLO）" % name, True)
        if env.lib_name != "":
            check("环境 %s lib_name 为空（Predixy 拒 CLIENT）" % name, False,
                  "当前 lib_name=%r" % env.lib_name)
        else:
            check("环境 %s lib_name 为空（Predixy 拒 CLIENT）" % name, True)
    try:
        stage = settings.env("stage")
        check("stage → mirror 别名", stage.actual == "mirror", "stage 解析到 %s" % stage.actual)
    except Exception as exc:                        # noqa: BLE001
        skip("stage → mirror 别名", "配置里没有 stage 别名（当前：%s）" % exc)
    try:
        settings.env("mirror")
        check("别名 mirror 可直达", True)
    except Exception as exc:                        # noqa: BLE001
        skip("别名 mirror 可直达", str(exc)[:80])
    try:
        settings.env("__not_exist__")
        check("未知环境拒绝", False, "未拦截")
    except Exception as exc:                        # noqa: BLE001
        check("未知环境拒绝", "可选" in str(exc), str(exc)[:80])

    # ---- 3. 只读防线 ----
    print("\n== 3. 只读防线 ==")
    guard = settings.guard
    check("写命令黑名单非空", len(guard.forbidden_commands) > 20,
          "%d 条" % len(guard.forbidden_commands))
    check("只读白名单非空", len(guard.allowed_commands) > 10,
          "%d 条" % len(guard.allowed_commands))
    check("Predixy 拒绝清单非空", len(guard.proxy_rejected_commands) > 0,
          "%s" % sorted(guard.proxy_rejected_commands))

    for bad in ("set", "del", "flushall", "hset", "zadd", "eval"):
        try:
            G.guard_command(bad, guard.allowed_commands, guard.forbidden_commands)
            check("写命令拒绝 %s" % bad, False, "未拦截")
        except G.GuardError:
            check("写命令拒绝 %s" % bad, True)
    for good in ("getrange", "hgetall", "scan"):
        try:
            G.guard_command(good, guard.allowed_commands, guard.forbidden_commands)
            check("读命令放行 %s" % good, True)
        except G.GuardError as exc:
            check("读命令放行 %s" % good, False, str(exc))

    for bad in ("", "x" * (guard.max_key_len + 1), "a\nb"):
        try:
            G.check_key(bad, guard.max_key_len)
            check("非法 key 拒绝 %r" % bad[:8], False, "未拦截")
        except G.GuardError:
            check("非法 key 拒绝 %r" % bad[:8], True)
    try:
        G.check_key("www:SellCarProgress:123", guard.max_key_len)
        check("正常 key 放行", True)
    except G.GuardError as exc:
        check("正常 key 放行", False, str(exc))

    for bad in ("x" * (guard.max_pattern_len + 1), "a\tb"):
        try:
            G.check_pattern(bad, guard.max_pattern_len)
            check("非法 pattern 拒绝 %r" % bad[:8], False, "未拦截")
        except G.GuardError:
            check("非法 pattern 拒绝 %r" % bad[:8], True)
    try:
        check("空 pattern 归一为 *", G.check_pattern("", guard.max_pattern_len) == "*")
    except G.GuardError as exc:
        check("空 pattern 归一为 *", False, str(exc))

    try:
        G.check_range("read_list", 5, 1, guard.max_members)
        check("非法区间拒绝", False, "未拦截")
    except G.GuardError:
        check("非法区间拒绝", True)
    s, e = G.check_range("read_list", 0, 99999, guard.max_members)
    check("区间窗口被夹住", e - s + 1 == guard.max_members, "range=%s..%s" % (s, e))

    for good in ("-inf", "+inf", "0", "1.5", "(5"):
        try:
            G.check_score("min_score", good)
            check("合法分数放行 %s" % good, True)
        except G.GuardError as exc:
            check("合法分数放行 %s" % good, False, str(exc))
    for bad in ("abc", "", "1..2"):
        try:
            G.check_score("min_score", bad)
            check("非法分数拒绝 %r" % bad, False, "未拦截")
        except G.GuardError:
            check("非法分数拒绝 %r" % bad, True)

    check("整数夹取", G.clamp_int("count", 999999, 1, guard.max_count) == guard.max_count)
    try:
        G.clamp_int("count", "abc", 1, 10)
        check("非整数拒绝", False, "未拦截")
    except G.GuardError:
        check("非整数拒绝", True)

    # ---- 4. 输出控制 ----
    print("\n== 4. 输出控制 ==")
    big = {"s": "字" * 5000}
    shrunk = G.dumps(big, guard.max_field_chars)
    check("超长字段截断", "截断" in shrunk, "长度 %d" % len(shrunk))
    clamped = G.clamp_bytes("字" * 100000, 1000)
    check("字节上限截断", len(clamped.encode("utf-8")) <= 1000 + 64,
          "字节 %d" % len(clamped.encode("utf-8")))
    items, note, truncated = G.cap_items(list(range(21)), 20)
    check("成员数上限", truncated and len(items) == 20, note)

    # ---- 5. 审计 ----
    print("\n== 5. 审计 ==")
    from redis_ro_mcp import audit as A
    policy = A.AuditPolicy.from_config(settings.audit)
    sha, length = A.fingerprint("www:SellCarProgress:123")
    check("key 指纹只落 sha256/长度", len(sha) == 64 and length > 0, "len=%d" % length)
    payload = A.record(settings.audit.dir, policy, event="selftest", tool="selftest",
                       outcome="ok", env="test", env_actual="test",
                       client_ip="10.0.0.1", agent_id="agent-x",
                       socket_ip="10.0.0.219", client_ip_source="X-Forwarded-For",
                       agent_id_source="X-Agent-Id", identity_trusted=True,
                       database="db0", collection="www:SellCarProgress:123")
    for field in ("client_ip", "agent_id", "socket_ip", "client_ip_source",
                  "agent_id_source", "identity_trusted", "query_sha256", "reject",
                  "database", "collection"):
        check("审计字段 %s" % field, field in payload)
    check("审计前缀为 audit-redis", policy.filename_prefix == "audit-redis",
          policy.filename_prefix)

    # ---- 6. 真连（可选） ----
    if args.offline:
        print("\n== 6. 真连（--offline 跳过）==")
    else:
        print("\n== 6. 真连 ==")
        for name in settings.names():
            try:
                info = S._conns().probe(name)
                if info.get("reachable"):
                    proxy = info.get("proxy") or {}
                    check("list_environments(%s)" % name, bool(info.get("ping")),
                          "代理 %s %s / 样例键 %s"
                          % (proxy.get("name"), proxy.get("version"),
                             info.get("scan_sample")))
                else:
                    skip("list_environments(%s)" % name, (info.get("error") or "")[:120])
            except Exception as exc:                # noqa: BLE001
                skip("list_environments(%s)" % name,
                     "%s: %s" % (type(exc).__name__, exc))

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
