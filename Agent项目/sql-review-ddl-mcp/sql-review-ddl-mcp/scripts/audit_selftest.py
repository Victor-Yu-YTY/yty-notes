"""审计行为自测（不需要 MCP 依赖、不需要数据库）。

用法：
    PYTHONPATH=src python3 scripts/audit_selftest.py

验证内容：
  1. 9 个工具的每个出口都写一行审计（含所有拒绝路径）；
  2. 未放行（outcome != pass/info）必须有 reject{code,stage,text}，且原因码对得上具体场景；
  3. 鉴权拒绝（401/403 前置）与协议层拒绝（POST + HTTP>=400）也留痕；
  4. 留存：30 天外删除、7 天外压缩、近期不动；
  5. 不落 SQL 明文（只落 sha256 与长度）。

做法：用内置的 fastmcp 桩替换真实框架，直接调用 server.py 里的工具函数。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


# --------------------------------------------------------------------------
# fastmcp 桩（必须在 import server 之前装好）
# --------------------------------------------------------------------------


class _FastMCP:
    def __init__(self, name="sql-review-ddl-mcp"):
        self.name = name

    def tool(self, *a, **kw):
        def deco(fn):
            return fn
        return deco

    def run(self, *a, **kw):
        return None

    def http_app(self, *a, **kw):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        return app


def _install_fastmcp_stub():
    m = types.ModuleType("fastmcp")
    m.FastMCP = _FastMCP
    srv = types.ModuleType("fastmcp.server")
    deps = types.ModuleType("fastmcp.server.dependencies")
    deps.HEADERS = {}

    def get_http_headers():
        return dict(deps.HEADERS)

    deps.get_http_headers = get_http_headers
    srv.dependencies = deps
    m.server = srv
    sys.modules["fastmcp"] = m
    sys.modules["fastmcp.server"] = srv
    sys.modules["fastmcp.server.dependencies"] = deps
    return deps


DEPS = _install_fastmcp_stub()

from sql_review_ddl import server                                    # noqa: E402
from sql_review_ddl.audit import AuditPolicy, iter_records, purge   # noqa: E402
from sql_review_ddl.common import Thresholds                         # noqa: E402
from sql_review_ddl.config import EnvConfig, Settings                # noqa: E402
from sql_review_ddl.reviewer import get_ruleset                     # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[OK ] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  {extra}" if extra else ""))


REQ = [
    "`ID` bigint NOT NULL AUTO_INCREMENT COMMENT '无意义主键ID'",
    "`IS_DEL` tinyint NOT NULL DEFAULT 0 COMMENT '是否删除'",
    "`CREATE_TIME` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'",
    "`MODIFY_TIME` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间'",
    "`__MODIFY_TIME__` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI'",
]
REQIDX = ["PRIMARY KEY (`ID`)", "KEY `IDX_CREATE_TIME` (`CREATE_TIME`)",
          "KEY `IDX__MODIFY_TIME__` (`__MODIFY_TIME__`)"]
GOOD = ("CREATE TABLE `DEMO_ORDER` (\n"
        + ",\n".join("  " + x for x in REQ + ["`ORDER_NO` varchar(32) NOT NULL DEFAULT '' COMMENT '订单号号'"])
        + ",\n" + ",\n".join("  " + x for x in REQIDX)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更记录';")
BAD_ALTER = "ALTER TABLE `T` ADD COLUMN `C` int NOT NULL DEFAULT 0 COMMENT 'c';"


class _AppReturning:
    def __init__(self, status):
        self.status = status

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b'{"error":{"code":-32602}}'})


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sql-review-audit-")
    logs = os.path.join(tmp, "logs")

    s = Settings()
    s.standard_version = "selftest"
    s.thresholds = Thresholds()
    s.metadata_backend = "none"
    s.token = "t" * 64
    s.token_header = "X-SQL-REVIEW-DDL-MCP-TOKEN"
    s.log_dir = type(s.log_dir)(logs)
    s.standard_path = type(s.standard_path)(os.path.join(ROOT, "standards", "ddl.md"))
    s.dml_standard_path = type(s.dml_standard_path)(
        os.path.join(ROOT, "standards", "dml.md"))
    s.override_path = type(s.override_path)(os.path.join(tmp, "__none__.yaml"))
    s.questions_path = type(s.questions_path)(os.path.join(tmp, "__none_q__.yaml"))
    s.envs = {"dev": EnvConfig(host="127.0.0.1"), "test": EnvConfig(host="127.0.0.1"),
              "stage": EnvConfig(host="127.0.0.1")}

    server._STATE.clear()
    server._STATE["settings"] = s
    server._STATE["ddl_ruleset"], _ = get_ruleset(s, "ddl", force=True)
    server._STATE["dml_ruleset"], _ = get_ruleset(s, "dml", force=True)
    server._STATE["questions"] = {"questions": []}
    DEPS.HEADERS = {"X-SQL-REVIEW-DDL-MCP-TOKEN": s.token}

    def records():
        return list(iter_records(logs))

    print("=" * 78)
    print("审计自测：每个出口都要留痕，拒绝必须有原因")
    print("=" * 78)

    # v1.0.0（拆分）：两个包共用本脚本，按 server 实际暴露的工具装用例
    cases = []

    if hasattr(server, "review_create_table"):        # DDL 侧工具（SQL Review DDL）
        cases += [
            ("合规建表", lambda: server.review_create_table(sql=GOOD), "pass"),
            ("空 SQL", lambda: server.review_create_table(sql="   "), "invalid_request"),
            ("env 不支持", lambda: server.review_ddl(sql=GOOD, env="prod"), "invalid_request"),
            ("工具类型不符", lambda: server.review_create_table(sql=BAD_ALTER), "invalid_request"),
            ("DROP TABLE", lambda: server.review_destructive(sql="DROP TABLE `DEMO_OLD`;"), "rejected"),
            ("TRUNCATE", lambda: server.review_destructive(sql="TRUNCATE TABLE `DEMO_OLD`;"), "rejected"),
            ("CTAS", lambda: server.review_create_table(
                sql="CREATE TABLE `DEMO_X` SELECT * FROM `DEMO_Y`;"), "rejected"),
            ("语法坏掉", lambda: server.review_ddl(sql="CREATE TABLE ;"), "rejected"),
            ("缺必备字段", lambda: server.review_create_table(
                sql="CREATE TABLE `DEMO_X` (`ID` bigint NOT NULL AUTO_INCREMENT COMMENT 'id') ENGINE=InnoDB;"),
             "fix_required"),
            ("全部不可审（只 SELECT）", lambda: server.review_ddl(sql="SELECT 1;"), "rejected"),
            ("规则清单只取 DDL", lambda: server.get_rules(scope="ddl"), "info"),
            ("get_rules 拒绝 scope=dml", lambda: server.get_rules(scope="dml"), "invalid_request"),
        ]
    else:
        print("[SKIP] 本服务不提供 DDL 审核工具（DDL 由 SQL Review DDL 服务负责）")

    if hasattr(server, "review_dml"):                 # DML 侧工具（SQL Review DML）
        cases += [
            ("空 SQL（DML）", lambda: server.review_dml(sql="   "), "invalid_request"),
            ("env 不支持（DML）", lambda: server.review_dml(
                sql="UPDATE `DEMO_T` SET A = 1 WHERE ID = 1;", env="prod"), "invalid_request"),
            ("工具类型不符（DML）", lambda: server.review_dml(
                sql="CREATE TABLE `DEMO_X` (`ID` bigint NOT NULL COMMENT 'id');"), "invalid_request"),
            ("DELETE 驳回（DM-12 fatal）",
             lambda: server.review_dml(sql="DELETE FROM `DEMO_T` WHERE ID = 1;"), "rejected"),
            ("全部不可审（只 SELECT）", lambda: server.review_dml(sql="SELECT 1;"), "rejected"),
            ("规则清单只取 DML", lambda: server.get_rules(scope="dml"), "info"),
            ("get_rules 拒绝 scope=ddl", lambda: server.get_rules(scope="ddl"), "invalid_request"),
        ]
    else:
        print("[SKIP] 本服务不提供 DML 审核工具（DML 由 SQL Review DML 服务负责）")

    cases += [
        ("查结构（非法 env）", lambda: server.get_table_structure(env="badenv", database="d", table="t"),
         "invalid_request"),
        ("查重（无元数据源）", lambda: server.check_table_conflict(env="dev", database="d", table="t"),
         "info"),
        ("规则清单", lambda: server.get_rules(), "info"),
        ("get_rules 非法 scope", lambda: server.get_rules(scope="bad"), "invalid_request"),
    ]
    for name, fn, want in cases:
        before = len(records())
        fn()
        recs = records()[before:]
        check(f"{name}：写了 1 行", len(recs) == 1, f"实际 {len(recs)}")
        if not recs:
            continue
        r = recs[0]
        check(f"{name}：outcome={want}", r.get("outcome") == want, f"实际 {r.get('outcome')}")
        rj = r.get("reject") or {}
        if want in ("pass", "info"):
            check(f"{name}：不误报拒绝", r.get("reject") is None)
        else:
            check(f"{name}：reject 有码有说明", bool(rj.get("code")) and bool(rj.get("text")),
                  json.dumps(rj, ensure_ascii=False)[:120])

    print()
    print("--- 原因码 ---")
    codes = {r["reject"]["code"] for r in records() if r.get("reject")}
    want_codes = ["ARG_SQL_EMPTY", "ARG_ENV_UNSUPPORTED", "ARG_TOOL_TYPE_MISMATCH",
                  "ARG_SCOPE_UNSUPPORTED", "SCOPE_UNCALLABLE"]
    if hasattr(server, "review_create_table"):        # DDL 侧专属原因码
        want_codes += ["ARG_SQL_UNPARSABLE", "RULE_REJECTED_DESTRUCTIVE",
                       "RULE_REJECTED_CTAS", "RULE_FIX_REQUIRED"]
    if hasattr(server, "review_dml"):                  # DML 侧专属原因码
        want_codes += ["RULE_REJECTED_DML_DESTRUCTIVE"]
    for want in want_codes:
        check(f"出现原因码 {want}", want in codes, str(sorted(codes)))

    # v1.4.0 修订：新码必须落在正确的 stage（否则 audit_report 按 stage 统计会错位）
    stages = {r["reject"]["code"]: r["reject"]["stage"]
              for r in records() if r.get("reject")}
    for code, want_stage in (("SCOPE_UNCALLABLE", "scope"),
                             ("RULE_REJECTED_DML_DESTRUCTIVE", "rule"),
                             ("ARG_SCOPE_UNSUPPORTED", "args"),
                             ("RULE_REJECTED_DESTRUCTIVE", "rule"),
                             ("ARG_TOOL_TYPE_MISMATCH", "args")):
        if code in stages:
            check(f"{code} 的 stage={want_stage}", stages[code] == want_stage,
                  f"实际 {stages[code]}")

    drop = [r for r in records() if (r.get("reject") or {}).get("code") == "RULE_REJECTED_DESTRUCTIVE"]
    if drop:
        rj = drop[0]["reject"]
        blob = rj["text"] + json.dumps(rj.get("evidence", {}), ensure_ascii=False)
        check("DROP 的原因含规则号 DX-01", "DX-01" in blob, rj["text"])
        check("DROP 的原因含规范行号",
              bool((rj.get("evidence") or {}).get("standard_lines")),
              json.dumps(rj.get("evidence", {}), ensure_ascii=False)[:160])

    print()
    print("--- 鉴权 / 协议层拒绝 ---")

    async def run_mw(headers: dict, method="POST", app_status=200):
        mw = server.TokenMiddleware(_AppReturning(app_status), s)
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        DEPS.HEADERS = headers
        scope = {"type": "http", "path": "/mcp/", "method": method,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                 "client": ("10.0.0.9", 1234)}
        await mw(scope, receive, send)
        return sent

    import asyncio

    before = len(records())
    sent = asyncio.run(run_mw({}))
    recs = records()[before:]
    check("无令牌 → HTTP 403", sent[0]["status"] == 403, str(sent[0].get("status")))
    check("无令牌 → TOKEN_MISSING + 来源 IP",
          len(recs) == 1 and recs[0]["event"] == "auth_denied"
          and recs[0]["reject"]["code"] == "TOKEN_MISSING"
          and recs[0]["client_ip"] == "10.0.0.9",
          json.dumps(recs, ensure_ascii=False)[:200])

    before = len(records())
    asyncio.run(run_mw({"X-SQL-REVIEW-DDL-MCP-TOKEN": "bad"}))
    recs = records()[before:]
    check("错令牌 → TOKEN_INVALID",
          len(recs) == 1 and recs[0]["reject"]["code"] == "TOKEN_INVALID",
          json.dumps(recs, ensure_ascii=False)[:200])

    before = len(records())
    asyncio.run(run_mw({"X-SQL-REVIEW-DDL-MCP-TOKEN": s.token}, app_status=400))
    recs = records()[before:]
    check("POST 400 → http_error/PROTO_REJECTED",
          len(recs) == 1 and recs[0]["event"] == "http_error"
          and recs[0]["reject"]["code"] == "PROTO_REJECTED",
          json.dumps(recs, ensure_ascii=False)[:200])

    before = len(records())
    asyncio.run(run_mw({"X-SQL-REVIEW-DDL-MCP-TOKEN": s.token}, method="GET", app_status=406))
    check("GET 非 200 不记（避免部署自检噪音）", len(records()) == before)

    print()
    print("--- 脱敏 ---")
    blob = ""
    for name in os.listdir(logs):
        with open(os.path.join(logs, name), encoding="utf-8") as f:
            blob += f.read()
    check("日志不含 SQL 明文",
          "CREATE TABLE `" not in blob and "DROP TABLE `" not in blob
          and "ALTER TABLE `" not in blob)
    check("日志含规范指纹", '"sha256"' in blob)
    check("日志不含服务令牌", s.token not in blob)

    print()
    print("--- 留存（30 天删除 / 7 天压缩） ---")
    pol = AuditPolicy(retention_days=30, compress_after_days=7)
    today = dt.date.today()
    for age in (40, 10, 3):
        p = os.path.join(logs, f"audit-{(today - dt.timedelta(days=age)).isoformat()}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"ts":"x","outcome":"pass"}\n')
    res = purge(logs, pol, force=True)
    files = sorted(os.listdir(logs))
    check("40 天前被删除",
          f"audit-{(today - dt.timedelta(days=40)).isoformat()}.jsonl" not in files)
    check("10 天前被压缩",
          f"audit-{(today - dt.timedelta(days=10)).isoformat()}.jsonl.gz" in files)
    check("3 天前保持原样",
          f"audit-{(today - dt.timedelta(days=3)).isoformat()}.jsonl" in files)
    check("purge 统计正确", res == {"deleted": 1, "compressed": 1, "errors": 0}, str(res))
    check("压缩文件可读", any(r.get("ts") == "x" for r in iter_records(logs, days=30)))

    print()
    print("--- 调用方身份（网关透传 IP / AgentID，v1.0.1）---")
    scope_gw = {"client": ("10.0.0.219", 5000)}
    hdrs = {"x-forwarded-for": "10.20.30.40, 10.0.0.219",
            "x-real-ip": "10.20.30.40",
            "x-agent-id": "agent-abc123"}
    ident = server._resolve_identity(scope_gw, hdrs, s)
    check("可信代理：解析出 agent_id", ident.get("agent_id") == "agent-abc123", str(ident))
    check("可信代理：client_ip 取 XFF 最左一跳、socket_ip 为网关",
          ident.get("client_ip") == "10.20.30.40"
          and ident.get("socket_ip") == "10.0.0.219"
          and ident.get("client_ip_source") == "X-Forwarded-For",
          str(ident))

    before = len(records())
    tok = server._CTX_IDENT.set(ident)
    server._CTX_IP.set(ident.get("client_ip", ""))
    try:
        server.get_rules()
    finally:
        server._CTX_IDENT.reset(tok)
        server._CTX_IP.set("")
    r0 = (records()[before:] or [{}])[0]
    check("身份随工具调用落审计（agent_id/client_ip/socket_ip）",
          r0.get("agent_id") == "agent-abc123" and r0.get("client_ip") == "10.20.30.40"
          and r0.get("socket_ip") == "10.0.0.219" and r0.get("identity_trusted") is True,
          json.dumps({k: r0.get(k) for k in ("agent_id", "client_ip", "socket_ip",
                                             "client_ip_source", "agent_id_source",
                                             "identity_trusted")}, ensure_ascii=False))

    ident_bad = server._resolve_identity({"client": ("10.9.9.9", 1)}, hdrs, s)
    check("非可信直连方：身份留空、只记直连 IP、标 untrusted-peer",
          ident_bad.get("agent_id") == "" and ident_bad.get("identity_trusted") is False
          and ident_bad.get("client_ip") == "10.9.9.9"
          and ident_bad.get("agent_id_source") == "untrusted-peer",
          str(ident_bad))

    ident_dirty = server._resolve_identity(scope_gw, {"x-agent-id": "bad\nagent" + "x" * 200}, s)
    aid = ident_dirty.get("agent_id", "")
    check("身份字段清洗（去控制字符 + 截断 128）",
          "\n" not in aid and len(aid) <= 128 and aid.startswith("badagent"), f"len={len(aid)}")

    s.record_identity = False
    try:
        before = len(records())
        tok = server._CTX_IDENT.set(ident)
        server._CTX_IP.set(ident.get("client_ip", ""))
        try:
            server.get_rules()
        finally:
            server._CTX_IDENT.reset(tok)
            server._CTX_IP.set("")
        r0 = (records()[before:] or [{}])[0]
        check("identity.record=false：审计只留 client_ip、不写身份值",
              r0.get("agent_id") == "" and r0.get("socket_ip") == ""
              and r0.get("client_ip") == "10.20.30.40",
              json.dumps({k: r0.get(k) for k in ("agent_id", "client_ip", "socket_ip")},
                         ensure_ascii=False))
    finally:
        s.record_identity = True

    async def run_mw_gw(hdrs: dict, app_status: int = 400):
        """以「网关 IP 作为直连方」跑一次中间件：验证透传头 → 上下文 → 审计这条真实链路。"""
        mw = server.TokenMiddleware(_AppReturning(app_status), s)
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        DEPS.HEADERS = hdrs
        scope = {"type": "http", "path": "/mcp/", "method": "POST",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
                 "client": ("10.0.0.219", 50123)}
        await mw(scope, receive, send)
        return sent

    before = len(records())
    asyncio.run(run_mw_gw({s.token_header: s.token,
                           "X-Forwarded-For": "10.5.6.7, 10.0.0.219",
                           "X-Agent-Id": "agent-mw-1"}))
    r0 = (records()[before:] or [{}])[0]
    check("中间件链路：透传身份直接进审计（http_error 记录也带 agent_id）",
          r0.get("agent_id") == "agent-mw-1" and r0.get("client_ip") == "10.5.6.7"
          and r0.get("socket_ip") == "10.0.0.219" and r0.get("identity_trusted") is True,
          json.dumps({k: r0.get(k) for k in ("agent_id", "client_ip", "socket_ip",
                                             "identity_trusted")}, ensure_ascii=False))

    print()
    print("=" * 78)
    print(f"审计自测：通过 {PASS} 项，失败 {FAIL} 项 —— "
          f"{'全部通过 ✅' if FAIL == 0 else '存在失败 ❌'}")
    print("=" * 78)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
