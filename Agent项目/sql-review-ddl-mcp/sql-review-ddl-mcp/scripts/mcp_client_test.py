"""MCP 端到端验证（用 fastmcp 自带 Client）：工具面 + 拒绝场景 + 审计留痕。

用法：
    PYTHONPATH=src .venv/bin/python scripts/mcp_client_test.py \
        http://127.0.0.1:8010/mcp/ <令牌> [env] [database] [审计日志目录]

★ 拆分说明（v1.0.0）：本脚本两个包共用——
  服务只暴露 DDL 工具 → 断言 8 个工具体 + 跑 DDL 侧用例；
  服务只暴露 DML 工具 → 断言 4 个工具体 + 跑 DML 侧用例。
  「工具面恰好等于本服务应有的那一套」本身就是一条断言（多出/漏掉都会 FAIL）。

第 5 个参数（或环境变量 SR_AUDIT_LOGS）指向 /opt/mcpservers/<MCP_NAME>/logs；
给了就校验「拒绝原因是否留档、是否落了 SQL 明文」。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

from fastmcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SR_URL", "")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
ENV = sys.argv[3] if len(sys.argv) > 3 else ""          # 可选：dev/test/stage（无密码时留空）
DB = sys.argv[4] if len(sys.argv) > 4 else ""           # 可选：库名
LOGS = (sys.argv[5] if len(sys.argv) > 5 else os.environ.get("SR_AUDIT_LOGS", "")) or ""
TOKEN_HEADER = os.environ.get("SR_TOKEN_HEADER", "X-SQL-REVIEW-MCP-TOKEN")

# 拆分后两侧各自的工具面（服务暴露哪一套，就断言哪一套，且必须**恰好**相等）
DDL_TOOLS = {"review_ddl", "review_create_table", "review_alter_table",
             "review_index_change", "review_destructive",
             "get_table_structure", "check_table_conflict", "get_rules"}
DML_TOOLS = {"review_dml", "get_table_structure", "check_table_conflict", "get_rules"}

PASS, FAIL = 0, 0

GOOD_CREATE = """CREATE TABLE `DEMO_ORDER` (
  `ID` bigint NOT NULL AUTO_INCREMENT COMMENT '无意义主键ID',
  `IS_DEL` tinyint NOT NULL DEFAULT 0 COMMENT '是否删除',
  `CREATE_TIME` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `MODIFY_TIME` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `__MODIFY_TIME__` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI',
  `ORDER_NO` varchar(32) NOT NULL DEFAULT '' COMMENT '订单号号',
  PRIMARY KEY (`ID`),
  KEY `IDX_CREATE_TIME` (`CREATE_TIME`),
  KEY `IDX__MODIFY_TIME__` (`__MODIFY_TIME__`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更记录';"""

BAD_CREATE = "CREATE TABLE demo_x (ID bigint(20) NOT NULL AUTO_INCREMENT, FEE double NOT NULL DEFAULT '0') ENGINE=MyISAM;"
BAD_ALTER = "ALTER TABLE `DEMO_ORDER` ADD COLUMN `MEMO` varchar(64) NOT NULL DEFAULT '' COMMENT '备注' AFTER `ID`;"
TWO_ALTER = ("ALTER TABLE `DEMO_ORDER` ADD COLUMN `A` int NOT NULL DEFAULT 0 COMMENT 'a';\n"
             "ALTER TABLE `DEMO_ORDER` ADD COLUMN `B` int NOT NULL DEFAULT 0 COMMENT 'b';")
DROP_TABLE = "DROP TABLE `DEMO_OLD`;"

BAD_UPDATE = f"UPDATE {DB or 'db'}.T_ORDER SET STATUS = 1"
BAD_DELETE = f"DELETE FROM {DB or 'db'}.T_ORDER WHERE ID = 1"


def ok(msg):
    global PASS
    PASS += 1
    print("[OK ]", msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("[FAIL]", msg)


def payload(result) -> dict:
    sc = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc.get("data") or sc
    for item in (getattr(result, "content", None) or []):
        if getattr(item, "type", "") == "text":
            try:
                j = json.loads(item.text)
                return j.get("data") or j
            except Exception:  # noqa: BLE001
                continue
    return {}


def http_status(headers: dict) -> int:
    """裸 HTTP POST（不带 MCP 会话），用于验证鉴权拒绝。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "application/json, text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def audit_records() -> list[dict]:
    if not LOGS:
        return []
    pkg = os.environ.get("SR_PKG", "")
    if pkg:
        mod = __import__(f"{pkg}.audit", fromlist=["iter_records"])
        return list(mod.iter_records(LOGS))
    for cand in ("sql_review_ddl", "sql_review_dml", "sql_review_mcp"):
        try:
            mod = __import__(f"{cand}.audit", fromlist=["iter_records"])
        except ImportError:
            continue
        return list(mod.iter_records(LOGS))
    raise SystemExit("无法导入 audit 模块：请设 PYTHONPATH=src 或 SR_PKG=sql_review_ddl|sql_review_dml")


async def main() -> int:
    if not URL:
        print("用法：scripts/mcp_client_test.py <url> <令牌> [env] [database] [审计日志目录]")
        return 2

    # 只校验"本次运行"新写入的审计记录：本机 logs/ 里可能有历史版本的记录，
    # 全量扫描会把历史口径差异误判成本次缺陷。
    audit_before = len(audit_records())
    headers = {TOKEN_HEADER: TOKEN} if TOKEN else {}
    try:
        from fastmcp.client.transports import StreamableHttpTransport

        client = Client(StreamableHttpTransport(URL, headers=headers))
    except Exception as exc:  # noqa: BLE001
        print(f"[INFO] 使用默认 transport（{exc}）")
        client = Client(URL)

    async with client:
        tools = set(t.name for t in await client.list_tools())
        is_ddl = "review_ddl" in tools
        is_dml = "review_dml" in tools
        if is_ddl and is_dml:
            scope, expect = "ddl+dml（未拆分？）", DDL_TOOLS | DML_TOOLS
        elif is_ddl:
            scope, expect = "ddl", DDL_TOOLS
        elif is_dml:
            scope, expect = "dml", DML_TOOLS
        else:
            scope, expect = "unknown", set()

        if tools == expect:
            ok(f"tools/list 与 {scope.upper()} 服务的工具面完全一致（{len(tools)} 个）：{sorted(tools)}")
        else:
            bad(f"工具面不符（按 {scope} 判）：多出 {sorted(tools - expect)}，"
                f"缺少 {sorted(expect - tools)}")

        # ---------------- DDL 侧 ----------------
        if is_ddl:
            d = payload(await client.call_tool("review_create_table", {"sql": GOOD_CREATE}))
            (ok if d.get("verdict") == "pass" else bad)(f"合规建表 -> {d.get('verdict')}")

            d = payload(await client.call_tool("review_create_table", {"sql": BAD_CREATE}))
            fin = d.get("sql_final") or ""
            checks = {
                "verdict=need_more_info": d.get("verdict") == "need_more_info",
                "补必备字段 IS_DEL": "`IS_DEL`" in fin,
                "double→DECIMAL": "DECIMAL(12,2)" in fin,
                "MyISAM→InnoDB": "ENGINE=InnoDB" in fin,
                "补字符集": "utf8mb4" in fin,
            }
            for k, v in checks.items():
                (ok if v else bad)(f"不合规建表：{k}")

            d = payload(await client.call_tool("review_alter_table", {"sql": BAD_ALTER}))
            fin = d.get("sql_final") or ""
            (ok if "AFTER" not in fin.upper() else bad)(f"ALTER 去 AFTER -> {d.get('verdict')}")

            d = payload(await client.call_tool("review_alter_table", {"sql": TWO_ALTER}))
            stmts = d.get("statements") or []
            merged = any(s.get("merged_into") for s in stmts)
            (ok if merged else bad)(f"同表多 ALTER 已合并 -> {d.get('verdict')}（merged={merged}）")

            d = payload(await client.call_tool("review_destructive", {"sql": DROP_TABLE}))
            (ok if d.get("verdict") == "rejected" else bad)(f"DROP TABLE -> {d.get('verdict')}")

            d = payload(await client.call_tool("get_rules", {}))
            (ok if (d.get("count") or 0) >= 30 else bad)(f"get_rules（DDL）-> {d.get('count')} 条规则")
            d = payload(await client.call_tool("get_rules", {"scope": "ddl"}))
            (ok if (d.get("count") or 0) >= 30 else bad)(f"get_rules(scope=ddl) -> {d.get('count')} 条")
            d = payload(await client.call_tool("get_rules", {"scope": "dml"}))
            (ok if d.get("error") else bad)(
                "get_rules(scope=dml) 在 DDL 服务上被明确拒绝（DML 规则在另一个服务）")

        # ---------------- DML 侧 ----------------
        if is_dml:
            d = payload(await client.call_tool("review_dml", {"sql": BAD_UPDATE}))
            rules = {f.get("rule_id") for f in (d.get("findings") or [])}
            same = d.get("sql_final") == BAD_UPDATE
            (ok if "DM-05" in rules else bad)(f"review_dml UPDATE 无 WHERE -> rules={sorted(rules)}")
            (ok if same else bad)(f"review_dml 不改写（sql_final == sql_input）-> {same}")

            d = payload(await client.call_tool("review_dml", {"sql": BAD_DELETE}))
            rules = {f.get("rule_id") for f in (d.get("findings") or [])}
            (ok if "DM-12" in rules else bad)(f"review_dml DELETE 驳回 -> rules={sorted(rules)}")

            d = payload(await client.call_tool("get_rules", {}))
            (ok if (d.get("count") or 0) >= 15 else bad)(f"get_rules（DML）-> {d.get('count')} 条规则")
            d = payload(await client.call_tool("get_rules", {"scope": "dml"}))
            (ok if (d.get("count") or 0) >= 15 else bad)(f"get_rules(scope=dml) -> {d.get('count')} 条")
            d = payload(await client.call_tool("get_rules", {"scope": "ddl"}))
            (ok if d.get("error") else bad)(
                "get_rules(scope=ddl) 在 DML 服务上被明确拒绝（DDL 规则在另一个服务）")

        # ---------------- 元数据类（两侧都有）----------------
        if ENV and DB:
            d = payload(await client.call_tool("get_table_structure",
                                               {"env": ENV, "database": DB, "table": "DEMO_ORDER"}))
            (ok if d.get("source", "").startswith("mysql:") or d.get("exists") is not None else bad)(
                f"get_table_structure -> source={d.get('source')} exists={d.get('exists')}")
            d = payload(await client.call_tool("check_table_conflict",
                                               {"env": ENV, "database": DB, "table": "DEMO_SMOKE_TEST"}))
            (ok if d.get("source", "").startswith("mysql:") else bad)(
                f"check_table_conflict -> source={d.get('source')} exists={d.get('exists')}")
        else:
            print("[INFO] 未传 env/database，跳过元数据类工具（元数据不可用时它们会返回 source=none）")

        if is_ddl and ENV and DB:
            d = payload(await client.call_tool("review_alter_table",
                                               {"sql": BAD_ALTER, "env": ENV, "database": DB}))
            facts = (d.get("statements") or [{}])[0].get("facts", {})
            (ok if facts.get("source", "").startswith("mysql:") else bad)(
                f"带 env 的 ALTER -> facts={facts.get('source')}")

        # ---------------- 拒绝场景（每个拒绝都要能被日志解释）----------------
        print("\n--- 拒绝场景 ---")
        code = http_status({})
        (ok if code == 403 else bad)(f"无令牌 POST /mcp/ -> HTTP {code}（期望 403）")
        code = http_status({TOKEN_HEADER: "bad-token"})
        (ok if code == 403 else bad)(f"错令牌 POST /mcp/ -> HTTP {code}（期望 403）")

        if is_ddl:
            d = payload(await client.call_tool("review_create_table", {"sql": BAD_ALTER}))
            (ok if not d.get("verdict") else bad)("工具类型不符（ALTER 发给 review_create_table）被拒绝")
            d = payload(await client.call_tool("review_ddl", {"sql": GOOD_CREATE, "env": "prod"}))
            (ok if not d.get("verdict") else bad)("非法 env=prod 被拒绝")
            d = payload(await client.call_tool("review_create_table", {"sql": "  "}))
            (ok if not d.get("verdict") else bad)("空 SQL 被拒绝")

        if is_dml:
            d = payload(await client.call_tool("review_dml", {"sql": GOOD_CREATE}))
            (ok if not d.get("verdict") else bad)("工具类型不符（建表语句发给 review_dml）被拒绝")
            d = payload(await client.call_tool("review_dml", {"sql": BAD_UPDATE, "env": "prod"}))
            (ok if not d.get("verdict") else bad)("非法 env=prod 被拒绝（review_dml）")
            d = payload(await client.call_tool("review_dml", {"sql": "  "}))
            (ok if not d.get("verdict") else bad)("空 SQL 被拒绝（review_dml）")

    # ---------- 审计留痕校验 ----------
    print("\n--- 审计日志校验 ---")
    all_recs = audit_records()
    recs = all_recs[audit_before:]            # 只取本次运行新增的记录
    if not LOGS:
        print("[INFO] 未传审计日志目录，跳过审计校验（部署脚本会自动带上）")
    elif not recs:
        bad(f"审计目录 {LOGS} 里读不到【本次运行】新增的记录")
    else:
        codes = {r["reject"]["code"] for r in recs if r.get("reject")}
        expect_codes = {"TOKEN_MISSING", "TOKEN_INVALID", "ARG_TOOL_TYPE_MISMATCH",
                        "ARG_ENV_UNSUPPORTED", "ARG_SQL_EMPTY"}
        if is_ddl:
            expect_codes.add("RULE_REJECTED_DESTRUCTIVE")
        if is_dml:
            expect_codes.add("RULE_REJECTED_DML_DESTRUCTIVE")
        missing = expect_codes - codes
        (ok if not missing else bad)(
            f"拒绝原因码齐全（{len(codes)} 种）" + (f"，缺 {sorted(missing)}" if missing else ""))
        no_reason = [r for r in recs
                     if r.get("outcome") not in ("pass", "info")
                     and not (r.get("reject") or {}).get("code")]
        (ok if not no_reason else bad)(f"所有未放行记录都带 reject.code（异常 {len(no_reason)} 条）")
        no_text = [r for r in recs
                   if r.get("reject") and not (r["reject"].get("text") or "").strip()]
        (ok if not no_text else bad)(f"所有 reject 都带中文说明（空 {len(no_text)} 条）")
        # 启动审计由服务启动时写入（必然早于本次测试）→ 用全量历史判断
        (ok if any(r.get("event") == "startup" for r in all_recs) else bad)("存在启动审计（event=startup）")
        (ok if any(r.get("event") == "auth_denied" for r in recs) else bad)("存在鉴权拒绝审计")
        std = [r for r in recs if r.get("standard")]
        (ok if std and all(r["standard"].get("version") for r in std) else bad)(
            f"审核类记录带规范版本（{len(std)} 条）")

        plain = []
        for name in os.listdir(LOGS):
            if name.startswith("audit-"):
                with open(os.path.join(LOGS, name), encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                if ("CREATE TABLE `" in text or "DROP TABLE `" in text
                        or "ALTER TABLE `" in text or "DELETE FROM `" in text
                        or "UPDATE `" in text):
                    plain.append(name)
        (ok if not plain else bad)(f"日志不含 SQL 明文（命中文件：{plain}）")

        denied = [r for r in recs if r.get("outcome") == "denied"]
        (ok if denied and all(r.get("client_ip") for r in denied) else bad)(
            f"鉴权拒绝记录带 client_ip（{len(denied)} 条）")

    print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
    print("端到端验证通过 ✅" if FAIL == 0 else "存在失败 ❌")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
