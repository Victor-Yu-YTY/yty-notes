"""sql-review-ddl-mcp MCP 服务入口：8 个工具 + 准入令牌闸门 + /healthz。

本服务由 sql-review-mcp v1.4.0 拆分而来，**只审 DDL**；DML（INSERT / UPDATE /
DELETE / REPLACE）由独立的 SQL Review DML 服务（sql-review-dml-mcp，8030）负责。

工具分层（8 个）：
  审核类（核心） review_ddl / review_create_table / review_alter_table /
                 review_index_change / review_destructive
  事实类        get_table_structure / check_table_conflict
  元信息        get_rules（只返回 DDL 规则）

DDL 规则来自 standards/ddl.md，运行时抽取；standards/mysql.md 是供人读的总纲，不参与抽取。
（本服务不加载 standards/dml.md —— 那份文档属 SQL Review DML 服务。）

审计：每个工具出口、鉴权拒绝、协议层拒绝、启动各写一行 JSONL，
未直接放行的结论都带 reject{code,stage,text,evidence}。详见 audit.py。
v1.0.1 起额外记录**调用方身份**：网关透传的「机器 IP + AgentID」落进每行审计
（字段 agent_id / client_ip / socket_ip / *_source / identity_trusted），
用于追溯"哪台机器、哪个 Agent 调了哪个工具"；只记日志，不参与任何审核判定。
"""

from __future__ import annotations

import argparse
import contextvars
import hmac
import json
import logging
import os
import re
import time
from typing import Any

from fastmcp import FastMCP

from . import metadata as M
from .audit import (
    NOT_PASS_OUTCOMES, AuditPolicy, make_reject, purge, record,
)
from .config import ConfigError, Settings, load_questions, load_settings
from .reviewer import (
    DDL_TYPES, TYPE_ALTER_TABLE, TYPE_CREATE_INDEX, TYPE_CREATE_TABLE,
    TYPE_DROP_DATABASE, TYPE_DROP_INDEX, TYPE_DROP_TABLE, TYPE_TRUNCATE,
    get_ruleset, on_reload, review_sql, standard_version,
)
from .spec import summary as spec_summary

log = logging.getLogger("sql_review_ddl.server")

_STATE: dict[str, Any] = {}
mcp = FastMCP("sql-review-ddl-mcp")

SERVICE_DISPLAY_NAME = "SQL Review DDL"   # 兜底；实际取 config 的 server.display_name

# 单次 HTTP 请求的上下文（审计用）：来源 IP / 调用方身份 / 本次是否已写过审计
_CTX_IP: contextvars.ContextVar[str] = contextvars.ContextVar("sr_client_ip", default="")
_CTX_AUDITED: contextvars.ContextVar[bool] = contextvars.ContextVar("sr_audited", default=False)
# v1.0.1：调用方身份（网关透传的 AgentID 与机器 IP）。stdio 模式为空，不参与判定。
_CTX_IDENT: contextvars.ContextVar[dict] = contextvars.ContextVar("sr_identity", default={})


def init_state(config_path: str | None = None) -> Settings:
    if "settings" in _STATE:
        return _STATE["settings"]
    settings = load_settings(config_path or os.environ.get("DDM_CONFIG"))
    _STATE["settings"] = settings
    _STATE["questions"] = load_questions(settings)
    # 热加载成功后把最新规则集回写 _STATE（healthz / 启动日志 / 兜底读都看这一份）
    def _sync(doc: str, rs, warn: str) -> None:
        _STATE[f"{doc}_ruleset"] = rs
        _STATE[f"{doc}_warn"] = warn
        if rs is not None:
            log.info("%s 规则已%s：version=%s 规则=%d（停用 %d）允许项=%d",
                     doc.upper(), "加载" if not warn else "回退",
                     rs.rules_version, len(rs.rules),
                     len(rs.rules) - len(rs.enabled_rules()), len(rs.permits))
        if warn:
            log.warning("%s 规则警告：%s", doc.upper(), warn)

    on_reload(_sync)
    # 本服务只装 DDL 规范（standards/ddl.md）。写坏不阻塞启动，
    # 但 review_* 会明确报 ENGINE-02 / DEP_STANDARD_INCOMPLETE，不会静默放行。
    ddl_rs, ddl_warn = get_ruleset(settings, "ddl", force=True)
    _STATE["ddl_ruleset"] = ddl_rs
    _STATE["ddl_warn"] = ddl_warn
    if ddl_rs is None:
        log.error("DDL 规则不可用：%s", ddl_warn)
    else:
        log.info("DDL 规则已加载：version=%s 规则=%d（停用 %d）允许项=%d（%s）",
                 ddl_rs.rules_version, len(ddl_rs.rules),
                 len(ddl_rs.rules) - len(ddl_rs.enabled_rules()), len(ddl_rs.permits),
                 settings.standard_path)
    return settings


def _settings() -> Settings:
    return _STATE.get("settings") or init_state()


def _err(code: str, message: str, reason: str = "") -> dict:
    err = {"code": code, "message": message}
    if reason:
        err["reason"] = reason
    return {"ok": False, "error": err}


def _uncovered(ddl_rs) -> list[dict]:
    """总纲里「规范有、本服务不审」的条款（从 standards/mysql.md 的对照表读，如实披露）。"""
    path = getattr(_settings(), "total_standard_path", None)
    if not path:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"^\|\s*([^|]{4,}?)\s*\|\s*不审核（([^）]+)）\s*\|", line)
        if m:
            out.append({"clause": m.group(1).strip(), "reason": m.group(2).strip()})
    return out


# ==========================================================================
# 审计辅助
# ==========================================================================


def _display_name() -> str:
    """对外显示名：取配置，缺则用默认值。"""
    try:
        return _settings().display_name or SERVICE_DISPLAY_NAME
    except Exception:  # noqa: BLE001
        return SERVICE_DISPLAY_NAME


def _policy() -> AuditPolicy:
    s = _settings()
    return AuditPolicy(enabled=s.audit_enabled,
                       retention_days=s.audit_retention_days,
                       compress_after_days=s.audit_compress_after_days,
                       log_http_errors=s.audit_log_http_errors,
                       filename_prefix=s.audit_filename_prefix,
                       ext=s.audit_ext)


def _live_ruleset():
    """取**当前生效**的 DDL 规则集（按 mtime 热加载）。

    healthz 与审计读的是实时规则集而不是启动时缓存的 _STATE：
    改完 ddl.md（规则已生效）后不能还报旧版本，否则无法自证"按哪版规则判的"。
    """
    try:
        rs, _ = get_ruleset(_settings(), "ddl")
    except Exception:            # noqa: BLE001  healthz 不能因为规则异常而 500
        return _STATE.get("ddl_ruleset")
    return rs


def _standard_brief() -> dict | None:
    """本次判定依据的规范（版本 + 指纹 + 规则数）。"""
    rs = _live_ruleset()
    if rs is None:
        return None
    return {"version": standard_version(_settings()),
            "rules_version": rs.rules_version,
            "sha256": (rs.sha256 or "")[:16],
            "rules_count": len(rs.rules),
            "ddl_rules_count": len(rs.rules),
            "dml_rules_count": 0}


def _rule_lines(ids) -> dict:
    """规则 ID → 规范行号（回答"依据规范第几行"）。"""
    by_id: dict[str, int] = {}
    rs = _live_ruleset()
    for r in (rs.rules if rs is not None else []):
        by_id.setdefault(r.id, r.line)
    return {i: by_id[i] for i in ids if by_id.get(i)}


def _audit(**kw) -> dict:
    """统一写审计：自动补来源身份（IP / AgentID）与规范信息；未放行必须有原因（兜底自动补）。"""
    kw.setdefault("client_ip", _CTX_IP.get() or "")
    for key, value in _ident_kw(_CTX_IDENT.get() or {}).items():
        kw.setdefault(key, value)
    outcome = kw.get("outcome", "info")
    if outcome in NOT_PASS_OUTCOMES and not kw.get("reject"):
        log.warning("审计缺陷：outcome=%s 缺少 reject 原因，已兜底补记", outcome)
        kw["reject"] = make_reject(
            "INTERNAL_EXCEPTION",
            f"内部缺陷：结论 {outcome} 未附原因，已兜底留痕，请报 DBA 修复",
            {"outcome": outcome})
    return record(_settings().log_dir, _policy(), **kw)


def _audit_reject(tool: str, code: str, *, text: str | None = None,
                  evidence: dict | None = None, outcome: str = "invalid_request",
                  sql: str = "", detail: dict | None = None,
                  event: str = "tool_call") -> dict:
    payload = _audit(event=event, tool=tool, outcome=outcome, sql=sql,
                     standard=_standard_brief(),
                     reject=make_reject(code, text, evidence), detail=detail or {})
    _CTX_AUDITED.set(True)
    return payload


def _rules_hit(data: dict) -> list[str]:
    """命中的规则 ID（去重有序，最多 20 个）——让日志自己就能看出"违反了哪几条"。"""
    ids = []
    for f in data.get("findings") or []:
        rid = f.get("rule_id")
        if rid and rid not in ids:
            ids.append(rid)
    return ids[:20]


def _rule_reject(data: dict) -> dict | None:
    """把 verdict 翻译成 reject 块：说明为什么没有直接放行。"""
    verdict = data.get("verdict")
    if verdict in ("pass", "info", None):
        return None
    findings = data.get("findings") or []
    err_rules = sorted({f["rule_id"] for f in findings
                        if f.get("level") == "error" and not f.get("auto_fixed")})
    rules = _rules_hit(data)
    blocking = [q["id"] for q in (data.get("questions") or []) if q.get("blocking")]
    fatal = "；".join(s.get("fatal") for s in (data.get("statements") or []) if s.get("fatal"))
    lines = _rule_lines(err_rules or rules)
    evidence = {"rules": err_rules or rules, "standard_lines": lines,
                "verdict": verdict, "fatal": fatal, "blocking_questions": blocking}
    if verdict == "rejected":
        if data.get("in_scope_count", data.get("in_scope_ddl_count")) == 0:
            return make_reject(
                "SCOPE_UNCALLABLE",
                "本次没有可审核的语句（本服务只收 DDL）：SELECT 类查询请走 dba-mcp，"
                "DML（INSERT / UPDATE / DELETE / REPLACE）请走 SQL Review DML 服务",
                evidence)
        if "PARSE-01" in rules or "解析失败" in fatal:
            return make_reject("ARG_SQL_UNPARSABLE",
                               f"SQL 解析失败，无法审核；{fatal}".strip("；"), evidence)
        if "ENGINE-01" in rules or "审核异常" in fatal:
            return make_reject("INTERNAL_EXCEPTION",
                               f"服务内部异常导致无法审核；{fatal}".strip("；"), evidence)
        if any(r.startswith("DX-0") for r in err_rules) or "DX-0" in fatal:
            return make_reject(
                "RULE_REJECTED_DESTRUCTIVE",
                f"物理删除类操作一律驳回（命中 {'、'.join(err_rules) or 'DX'}）；{fatal}".strip("；"),
                evidence)
        if "TB-13" in err_rules or "TB-13" in fatal:
            return make_reject("RULE_REJECTED_CTAS",
                               f"CTAS（建表即灌数据）不可自动改写（命中 TB-13）；{fatal}".strip("；"),
                               evidence)
        if "DM-12" in err_rules:
            return make_reject(
                "RULE_REJECTED_DML_DESTRUCTIVE",
                "DELETE / REPLACE INTO 属不可逆写入，本通道不出审核结论，请改走 SQL Review DML 服务"
                + (f"；{fatal}".strip("；") if fatal else ""),
                evidence)
        return make_reject("RULE_REJECTED",
                           f"存在不可自动修复的违规：{'、'.join(err_rules) or '（见 findings）'}"
                           + (f"（规范第 {'、'.join(str(v) for v in lines.values())} 行）" if lines else ""),
                           evidence)
    if verdict == "need_more_info":
        if err_rules:
            # 已有明确违规，只是同时还有未判定项——不能笼统说成「缺信息」
            return make_reject(
                "RULE_REJECTED",
                f"存在不可自动修复的违规：{'、'.join(err_rules)}"
                + (f"（规范第 {'、'.join(str(v) for v in lines.values())} 行）" if lines else ""),
                evidence)
        why = f"需补 {'、'.join(blocking)}" if blocking else "需补目标表结构/行数等事实信息"
        return make_reject("RULE_NEED_INFO", f"缺少事实信息，无法判定；{why}（补齐后可复审）", evidence)
    if verdict == "fix_required":
        counts = data.get("counts") or {}
        return make_reject(
            "RULE_FIX_REQUIRED",
            f"已自动改写 {counts.get('auto_fixed', 0)} 处，另有 "
            f"{counts.get('error', 0)} 项需人工确认（非拒绝，可直接提交改写后的语句）",
            evidence)
    return None


def _pick_header(headers: dict, names) -> tuple[str, str]:
    """按配置顺序取第一个非空的请求头，返回 (头名, 值)。"""
    for name in names or ():
        value = (headers.get(str(name).lower()) or "").strip()
        if value:
            return str(name), value
    return "", ""


def _peer_trusted(socket_ip: str, trusted) -> bool:
    """直连方（网关）是否可信：只有可信代理透传的身份头才采信，防伪造。

    支持精确 IP 与 CIDR（如 10.0.0.219 / 10.0.0.0/24）。
    """
    if not socket_ip or not trusted:
        return False
    try:
        import ipaddress

        ip = ipaddress.ip_address(socket_ip)
    except ValueError:
        return False
    for item in trusted:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            if "/" in text:
                if ip in ipaddress.ip_network(text, strict=False):
                    return True
            elif ip == ipaddress.ip_address(text):
                return True
        except ValueError:
            continue
    return False


def _clean_ident(value: str, limit: int = 128) -> str:
    """身份字段落日志前清洗：去掉控制字符并截断（防换行注入 / 超长）。"""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    return text.strip()[:limit]


def _resolve_identity(scope, headers: dict, settings: Settings | None = None) -> dict:
    """解析调用方身份：机器 IP + AgentID（都由网关透传），并判定是否可信。

    * 直连方（socket peer）在 identity.trusted_proxies 之内时才采信透传头；
      否则只记直连 IP、身份留空，并标 identity_trusted=false（防伪造）。
    * 机器 IP：identity.client_ip_headers（X-Forwarded-For 取最左一跳 = 最原始客户端）。
    * AgentID：identity.agent_id_headers（按顺序第一个非空）。
    """
    s = settings or _settings()
    client = scope.get("client") or ("", 0)
    socket_ip = str(client[0] or "")
    trusted = _peer_trusted(socket_ip, getattr(s, "trusted_proxies", None))

    ip_name, ip_value = _pick_header(headers, getattr(s, "client_ip_headers", None))
    if trusted and ip_value:
        client_ip = _clean_ident(ip_value.split(",")[0], 64)     # XFF 最左一跳
        ip_source = ip_name
    else:
        client_ip = _clean_ident(socket_ip, 64)
        ip_source = "socket"

    agent_name, agent_value = _pick_header(headers, getattr(s, "agent_id_headers", None))
    if trusted and agent_value:
        agent_id, agent_source = _clean_ident(agent_value), agent_name
    else:
        agent_id, agent_source = "", ("untrusted-peer" if agent_value else "")

    return {"agent_id": agent_id, "agent_id_source": agent_source,
            "client_ip": client_ip, "client_ip_source": ip_source,
            "socket_ip": _clean_ident(socket_ip, 64), "identity_trusted": trusted}


def _ident_kw(ident: dict) -> dict:
    """把身份字段展开成 record() 的关键字参数（identity.record=false 时返回空）。"""
    if not ident or not getattr(_settings(), "record_identity", True):
        return {}
    return {"agent_id": ident.get("agent_id", ""),
            "socket_ip": ident.get("socket_ip", ""),
            "client_ip_source": ident.get("client_ip_source", ""),
            "agent_id_source": ident.get("agent_id_source", ""),
            "identity_trusted": ident.get("identity_trusted")}


def _client_ip(scope, headers: dict) -> str:
    """兼容旧调用：只返回解析后的调用方 IP。"""
    return _resolve_identity(scope, headers).get("client_ip", "")



def _gate(tool: str = "") -> dict | None:
    """工具级兜底鉴权（正常由 TokenMiddleware 拦截；stdio 模式靠这里）。被拒也会留档。"""
    settings = _settings()
    if not settings.token:
        return None
    header = settings.token_header.lower()
    provided = ""
    try:
        from fastmcp.server.dependencies import get_http_headers

        for k, v in (get_http_headers() or {}).items():
            if k.lower() == header:
                provided = (v or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not provided:
        _audit_reject(tool or "tool_gate", "TOKEN_MISSING",
                      text=f"缺少服务令牌（请求头 {settings.token_header}），工具级兜底鉴权拦截",
                      evidence={"via": "tool_gate", "header": settings.token_header},
                      outcome="denied")
        return _err("AUTH_TOKEN_MISSING", f"缺少服务令牌（请求头 {settings.token_header}）",
                    reason="TOKEN_MISSING")
    if not hmac.compare_digest(provided, settings.token):
        _audit_reject(tool or "tool_gate", "TOKEN_INVALID",
                      text="服务令牌无效，工具级兜底鉴权拦截",
                      evidence={"via": "tool_gate", "header": settings.token_header},
                      outcome="denied")
        return _err("FORBIDDEN", "服务令牌无效", reason="TOKEN_INVALID")
    return None


def _run(tool: str, sql: str, env, database, allow_types: tuple[str, ...], **kw) -> dict:
    """公共执行体：鉴权 → 类型预检 → 审核 → 审计（每个出口都留档）。

    v1.4.0 修订：allow_types 传进 review_sql，在**取元数据之前**就判类型不符，
    避免"工具选错"还白跑一次库查询。
    """
    started = time.perf_counter()
    denied = _gate(tool)
    if denied:
        return denied
    std = _standard_brief()
    result = review_sql(sql, _settings(), env=kw.get("env", env), database=database,
                        allow_types=allow_types or ())
    if not result.get("ok"):
        err = result["error"]
        reason = err.get("reason") or "INTERNAL_EXCEPTION"
        _audit(event="tool_call", tool=tool,
               outcome="invalid_request" if reason.startswith("ARG_") else "error",
               sql=sql, standard=std,
               reject=make_reject(reason, err.get("message"), err.get("detail")),
               detail={"error": err.get("code"), "message": (err.get("message") or "")[:200]})
        return result
    data = result["data"]
    if allow_types:
        # 兜底复核（正常已在 review_sql 前置判掉）：解析失败的语句不报「类型不符」，
        # 不在审核范围的语句（事务控制、SELECT 等）不参与类型校验
        bad = [s for s in data["statements"]
               if s["type"] not in allow_types
               and s.get("in_scope", True)
               and not any(f.get("rule_id") == "PARSE-01" for f in (s.get("findings") or []))]
        if bad:
            kinds = "、".join(sorted({s["type_label"] for s in bad}))
            detected = sorted({s["type"] for s in bad})
            _audit(event="tool_call", tool=tool, outcome="invalid_request", sql=sql,
                   standard=std,
                   reject=make_reject(
                       "ARG_TOOL_TYPE_MISMATCH",
                       f"该工具只接受 {', '.join(allow_types)}，实际检测到 {kinds}",
                       {"allowed": list(allow_types), "detected": detected}),
                   detail={"verdict": data.get("verdict"),
                           "statements": data.get("statement_count")})
            return _err("INVALID_ARGUMENTS",
                        f"该工具只接受 {', '.join(allow_types)}；检测到：{kinds}。"
                        "多条 DDL 请用 review_ddl；DML（INSERT / UPDATE / DELETE / REPLACE）"
                        "请走 SQL Review DML 服务的 review_dml。"
                        "DDL 与 DML 不要放在同一批次（DDL 会隐式提交，打断事务）",
                        reason="ARG_TOOL_TYPE_MISMATCH")
    outcome = data["verdict"]
    reject = _rule_reject(data)
    _audit(event="tool_call", tool=tool, outcome=outcome, sql=sql, standard=std,
           reject=reject,
           detail={"verdict": outcome,
                   "statements": data["statement_count"],
                   "in_scope_ddl": data.get("in_scope_ddl_count", 0),
                   "counts": data["counts"],
                   "rules": _rules_hit(data),
                   "rules_version": (std or {}).get("rules_version", ""),
                   "env": data.get("env", ""),
                   "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
    return result


# ==========================================================================
# 审核类工具
# ==========================================================================


@mcp.tool()
def review_ddl(sql: str, env: str | None = None, database: str | None = None) -> dict:
    """【核心】审核一段 DDL（可多语句），自动识别语句类型并逐条给结论。

    只接受 DDL（建表 / 变更 / 索引 / 破坏性）；DML（INSERT / UPDATE / DELETE /
    REPLACE）请走 SQL Review DML 服务的 `review_dml`。

    Args:
        sql: 待审 SQL，可含多条语句（用分号分隔，最多 20 条）。
        env: dev / test / stage；给了会自动读取目标表结构与行数（用于 MODIFY 原类型、大表判定）。
        database: 目标库名（语句里没写库前缀时使用）。

    Returns:
        {ok, data:{verdict, summary, submittable, statements[], findings[], questions[],
                   sql_final, counts{}, big_table_warning, env, database, sql_sha256}}

        verdict：
          pass            ✅ 全部符合规范，可以提交
          fix_required    ⚠️ 已自动改写，可直接提交（见 sql_final）
          need_more_info  ⏸ 需补充信息或必须人工整改（见 questions / findings）
          rejected        ❌ 本单不通过（如 DROP TABLE / CTAS 等不可自动修复项）
    """
    return _run("review_ddl", sql, env, database, allow_types=DDL_TYPES)


@mcp.tool()
def review_create_table(sql: str, env: str | None = None, database: str | None = None) -> dict:
    """专精：审核建表语句（CREATE TABLE），不合规直接给出改写后的语句。

    Args:
        sql: 单条 CREATE TABLE 语句
        env: 可选，dev / test / stage（用于判断表是否已存在）
        database: 可选，目标库名
    """
    return _run("review_create_table", sql, env, database, allow_types=(TYPE_CREATE_TABLE,))


@mcp.tool()
def review_alter_table(sql: str, env: str | None = None, database: str | None = None) -> dict:
    """专精：审核变更表语句（ALTER TABLE）。自动去掉 AFTER/FIRST、合并同表多语句。

    依赖事实的判定（MODIFY 原类型、VARCHAR 跨 255、大表窗口）需要传 env + database，
    缺失时会明确标 ⏸ 未判定，不会假装通过。

    Args:
        sql: 一条或多条 ALTER TABLE 语句
        env: dev / test / stage
        database: 目标库名
    """
    return _run("review_alter_table", sql, env, database, allow_types=(TYPE_ALTER_TABLE,))


@mcp.tool()
def review_index_change(sql: str, env: str | None = None, database: str | None = None) -> dict:
    """专精：审核索引变更（CREATE INDEX / DROP INDEX）。

    Args:
        sql: CREATE [UNIQUE] INDEX ... ON tbl (...) 或 DROP INDEX name ON tbl
        env: dev / test / stage（用于取字段类型、判断索引是否已存在）
        database: 目标库名
    """
    return _run("review_index_change", sql, env, database,
                allow_types=(TYPE_CREATE_INDEX, TYPE_DROP_INDEX))


@mcp.tool()
def review_destructive(sql: str) -> dict:
    """专精：审核破坏性操作（DROP TABLE / TRUNCATE / DROP DATABASE）——一律驳回并说明原因。

    Args:
        sql: DROP TABLE ... / TRUNCATE ... / DROP DATABASE ...
    """
    return _run("review_destructive", sql, None, None,
                allow_types=(TYPE_DROP_TABLE, TYPE_TRUNCATE, TYPE_DROP_DATABASE))


# ==========================================================================
# 事实类工具（只读元数据）
# ==========================================================================


@mcp.tool()
def get_table_structure(env: str, database: str, table: str) -> dict:
    """查看目标表结构（字段类型、索引、行数、容量）——dev/test/stage 只读。

    审核 ALTER 时建议先调这个，把结构信息提供给 agent 作为上下文。

    Args:
        env: dev / test / stage
        database: 库名
        table: 表名
    """
    denied = _gate("get_table_structure")
    if denied:
        return denied
    started = time.perf_counter()
    settings = _settings()
    env = (env or "").strip().lower()
    if env not in settings.envs:
        _audit(event="tool_call", tool="get_table_structure", outcome="invalid_request",
               reject=make_reject("ARG_ENV_UNSUPPORTED",
                                  f"env 只支持 {', '.join(sorted(settings.envs))}，收到 {env or '(空)'}",
                                  {"supported": sorted(settings.envs), "received": env}),
               detail={"env": env, "database": database, "table": table})
        return _err("INVALID_ARGUMENTS", f"env 只支持 {', '.join(sorted(settings.envs))}",
                    reason="ARG_ENV_UNSUPPORTED")
    try:
        fact = M.fetch_fact(settings, env, database, table)
        ddl = M.fetch_create_table_text(settings, env, database, table) if fact.exists else ""
        data = {
            "env": env,
            "database": database,
            "table": table,
            "exists": fact.exists,
            "source": fact.source,
            "engine": fact.engine,
            "table_rows": fact.table_rows,
            "size_gb": fact.size_gb(),
            "columns": [{"name": k, "type": v} for k, v in fact.columns.items()],
            "indexes": [{"name": k, "columns": v} for k, v in fact.indexes.items()],
            "create_table": ddl,
            "note": fact.note,
        }
        _audit(event="tool_call", tool="get_table_structure", outcome="info",
               standard=_standard_brief(),
               detail={"env": env, "database": database, "table": table,
                       "exists": fact.exists, "source": fact.source,
                       "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        return {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001
        _audit(event="tool_call", tool="get_table_structure", outcome="error",
               reject=make_reject("DEP_DB_UNREACHABLE",
                                  f"{env} 只读元数据不可用：{type(exc).__name__}: {exc}",
                                  {"env": env, "database": database, "table": table}),
               detail={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return _err("INTERNAL_ERROR", str(exc), reason="DEP_DB_UNREACHABLE")


@mcp.tool()
def check_table_conflict(env: str, database: str, table: str) -> dict:
    """提交前查重：目标库是否已有同名表、相似表（dev/test/stage 只读）。

    注意：查不到来源时返回 exists=null + source=unknown，绝不返回「不存在」。

    Args:
        env: dev / test / stage
        database: 库名
        table: 表名
    """
    denied = _gate("check_table_conflict")
    if denied:
        return denied
    started = time.perf_counter()
    settings = _settings()
    env = (env or "").strip().lower()
    if env not in settings.envs:
        _audit(event="tool_call", tool="check_table_conflict", outcome="invalid_request",
               reject=make_reject("ARG_ENV_UNSUPPORTED",
                                  f"env 只支持 {', '.join(sorted(settings.envs))}，收到 {env or '(空)'}",
                                  {"supported": sorted(settings.envs), "received": env}),
               detail={"env": env, "database": database, "table": table})
        return _err("INVALID_ARGUMENTS", f"env 只支持 {', '.join(sorted(settings.envs))}",
                    reason="ARG_ENV_UNSUPPORTED")
    try:
        exists = M.table_exists(settings, env, database, table)
        similar, source = M.similar_tables(settings, env, database, table)
        data = {
            "env": env, "database": database, "table": table,
            "exists": exists,
            "same_name": table if exists else None,
            "similar_tables": similar,
            "source": source if exists is not None else "unknown",
            "note": "" if exists is not None else "元数据不可用，请人工确认是否已存在同名表",
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _audit(event="tool_call", tool="check_table_conflict", outcome="info",
               standard=_standard_brief(),
               detail={"env": env, "database": database, "table": table,
                       "exists": exists, "similar": len(similar or []),
                       "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        return {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001
        _audit(event="tool_call", tool="check_table_conflict", outcome="error",
               reject=make_reject("DEP_DB_UNREACHABLE",
                                  f"{env} 只读元数据不可用：{type(exc).__name__}: {exc}",
                                  {"env": env, "database": database, "table": table}),
               detail={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return _err("INTERNAL_ERROR", str(exc), reason="DEP_DB_UNREACHABLE")


@mcp.tool()
def get_rules(rule_id: str | None = None, category: str | None = None,
              scope: str = "all") -> dict:
    """列出当前生效的审核规则（编号/级别/条款），让研发提前知道红线。

    DDL 规则来自 standards/ddl.md、DML 规则来自 standards/dml.md，都是运行时抽取，
    改规范只改这两个 md，不用改代码、不用重启。

    Args:
        rule_id: 如 TB-01、AL-01、IX-01、DX-01；留空返回全部
        category: 建表 / 索引 / 变更 / 破坏性 / 规模
        scope: ddl（默认）/ all —— 本服务只有 DDL 规则，两者等价
    """
    denied = _gate("get_rules")
    if denied:
        return denied
    try:
        scope_norm = (scope or "ddl").strip().lower()
        if scope_norm not in ("all", "ddl"):
            _audit(event="tool_call", tool="get_rules", outcome="invalid_request",
                   reject=make_reject("ARG_SCOPE_UNSUPPORTED",
                                      f"本服务只有 DDL 规则（scope 只支持 ddl / all），"
                                      f"收到 {scope!r}；DML 规则请查 SQL Review DML 服务", {}),
                   detail={"scope": scope})
            return _err("INVALID_ARGUMENTS",
                        "本服务只有 DDL 规则（scope 只支持 ddl / all）；"
                        "DML 规则请查 SQL Review DML 服务的 get_rules",
                        reason="ARG_SCOPE_UNSUPPORTED")

        ddl_rs, ddl_warn = get_ruleset(_settings(), "ddl")

        items = []
        with_samples = bool(rule_id)      # 样例只在按 rule_id 单查时返回（避免响应膨胀）
        if ddl_rs is not None:
            items.extend(dict(r.as_dict(include_samples=with_samples), scope="ddl")
                         for r in ddl_rs.rules)
        if rule_id:
            items = [r for r in items if str(r.get("id", "")).upper() == rule_id.strip().upper()]
        if category:
            items = [r for r in items if category in str(r.get("category", ""))]
        # 反问目录只披露与「本服务已加载的 DDL 规则」相关的话术（DML 的 Q-* 属另一个服务）
        ddl_ids = {r.id for r in ddl_rs.rules} if ddl_rs else set()
        questions = [q for q in ((_STATE.get("questions") or {}).get("questions") or [])
                     if str(q.get("rule_id", "")).upper() in ddl_ids]
        if rule_id:
            questions = [q for q in questions
                         if str(q.get("rule_id", "")).upper() == rule_id.strip().upper()]
        _audit(event="tool_call", tool="get_rules", outcome="info",
               standard=_standard_brief(),
               detail={"rule_id": rule_id or "", "category": category or "",
                       "scope": "ddl", "count": len(items)})
        return {"ok": True, "data": {
            "standard_version": standard_version(_settings()),
            "source": {"ddl": str(ddl_rs.path) if ddl_rs else ""},
            "sha256": ((ddl_rs.sha256 if ddl_rs else "")[:16]),
            "source_url": _settings().source_url,
            "scope_note": _settings().scope_note,
            "scope": "ddl",
            "count": len(items),
            "rules": items,
            "permits": [],
            "ddl_rules": spec_summary(ddl_rs, ddl_warn),
            "thresholds": dict(ddl_rs.thresholds if ddl_rs else {}),
            "questions": questions,
            "uncovered_forced_clauses": _uncovered(ddl_rs),
            "warnings": [ddl_warn] if ddl_warn else [],
        }}
    except Exception as exc:  # noqa: BLE001
        _audit(event="tool_call", tool="get_rules", outcome="error",
               reject=make_reject("INTERNAL_EXCEPTION",
                                  f"读取规则清单失败：{type(exc).__name__}: {exc}", {}),
               detail={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return _err("INTERNAL_ERROR", str(exc), reason="INTERNAL_EXCEPTION")


# ==========================================================================
# HTTP：令牌闸门 + /healthz
# ==========================================================================


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class TokenMiddleware:
    """第 1 层鉴权：X-SQL-REVIEW-DDL-MCP-TOKEN 缺失/错误 → 403，且早于 MCP 协议处理。

    v1.2.1 起兼任审计入口：记录来源 IP；令牌被拒写 auth_denied；
    对 POST 且 HTTP >= 400 的响应做兜底留痕（协议层拒绝，工具根本没被调用）。
    """

    def __init__(self, app, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        self._header = settings.token_header.lower()
        self._token = settings.token or ""
        self._path = settings.path or "/mcp"

    def _observe(self, send, ident: dict, path: str, method: str):
        """观测响应状态码；只读不改写，任何异常都不影响请求。"""

        async def wrapped(message):
            try:
                if message.get("type") == "http.response.start":
                    status = int(message.get("status") or 0)
                    if (method == "POST" and status >= 400
                            and not _CTX_AUDITED.get()
                            and self._settings.audit_log_http_errors):
                        _CTX_AUDITED.set(True)
                        code = "PROTO_REJECTED"
                        record(self._settings.log_dir, _policy(),
                               event="http_error", tool="",
                               client_ip=ident.get("client_ip", ""), **_ident_kw(ident),
                               outcome="error" if status >= 500 else "invalid_request",
                               reject=make_reject(
                                   code,
                                   f"MCP 协议层/路由层拒绝了请求（HTTP {status}），"
                                   "工具未被调用；多为参数不符合工具签名或路由不存在",
                                   {"http_status": status, "method": method, "path": path}),
                               detail={"http_status": status, "path": path, "method": method})
            except Exception:  # noqa: BLE001
                log.warning("http_error 审计失败", exc_info=True)
            await send(message)

        return wrapped

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        ident = _resolve_identity(scope, headers, self._settings)
        client_ip = ident.get("client_ip", "")
        _CTX_IP.set(client_ip)
        _CTX_IDENT.set(ident)
        _CTX_AUDITED.set(False)
        if method in ("GET", "HEAD") and path.rstrip("/") in ("/healthz", "/health"):
            ddl_rs = _live_ruleset()                   # 实时（热加载后即为最新）
            await _send_json(send, 200, {
                "ok": True, "service": self._settings.name,
                "display_name": _display_name(),
                "version": __import__("sql_review_ddl").version,
                "scope": "ddl",
                "standard_version": standard_version(self._settings),
                "standard_sha256": (getattr(ddl_rs, "sha256", "") or "")[:16],
                "rules_count": len(getattr(ddl_rs, "rules", []) or []),
                "rules_version": getattr(ddl_rs, "rules_version", "unknown"),
                "ddl_available": ddl_rs is not None,
                # v1.0.1：网关透传身份（只回显配置的头名，不含任何密钥，便于核对是否生效）
                "identity": {"enabled": bool(getattr(self._settings, "record_identity", True)),
                             "agent_id_headers": list(getattr(self._settings, "agent_id_headers", []) or []),
                             "client_ip_headers": list(getattr(self._settings, "client_ip_headers", []) or []),
                             "trusted_proxies": list(getattr(self._settings, "trusted_proxies", []) or [])},
                # 不暴露安装路径（免鉴权端点只给版本/条数）
            })
            return
        if not self._token:
            await self._app(scope, receive, self._observe(send, ident, path, method))
            return
        if path == self._path or path == self._path + "/" or path.startswith(self._path + "/"):
            provided = (headers.get(self._header) or "").strip()
            if not provided or not hmac.compare_digest(provided, self._token):
                _CTX_AUDITED.set(True)
                code = "TOKEN_MISSING" if not provided else "TOKEN_INVALID"
                text = (f"缺少服务准入令牌（请求头 {self._settings.token_header}）"
                        if not provided else "服务准入令牌无效")
                record(self._settings.log_dir, _policy(),
                       event="auth_denied", tool="gateway_token",
                       client_ip=client_ip, **_ident_kw(ident),
                       outcome="denied",
                       reject=make_reject(code, text,
                                          {"path": path, "method": method, "via": "middleware",
                                           "header": self._settings.token_header}),
                       detail={"reason": "missing" if not provided else "invalid",
                               "path": path})
                await _send_json(send, 403, {"ok": False, "error": {
                    "code": "FORBIDDEN",
                    "message": f"缺少或无效的服务令牌（请求头 {self._settings.token_header}）"}})
                return
        await self._app(scope, receive, self._observe(send, ident, path, method))


def build_http_app(settings: Settings):
    factory = getattr(mcp, "http_app", None) or getattr(mcp, "streamable_http_app", None)
    if factory is None:
        return None
    for kwargs in ({"path": settings.path}, {}):
        try:
            return TokenMiddleware(factory(**kwargs), settings)
        except TypeError:
            continue
    return None


def standard_report(ddl_rs, dml_rs=None, ddl_warn: str = "") -> str:
    """DDL 规范加载结果的可读报告（--check-standard / 部署预检用）。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"DDL 规范：{getattr(ddl_rs, 'path', '') or '（不可用）'}")
    lines.append("=" * 78)
    if ddl_rs is None:
        lines.append(f"❌ 不可用：{ddl_warn or '未加载'}")
        lines.append("")
        return "\n".join(lines)
    enabled = len(ddl_rs.enabled_rules())
    lines.append(f"版本        : {ddl_rs.rules_version}")
    lines.append(f"sha256     : {ddl_rs.sha256}")
    lines.append(f"规则数      : {len(ddl_rs.rules)}（启用 {enabled}，停用 {len(ddl_rs.rules) - enabled}"
                 f"），允许项 {len(ddl_rs.permits)} 条")
    for r in ddl_rs.rules:
        who = "检查" if r.check else "判定"
        flag = "" if r.enabled else "（停用）"
        lines.append(f"  - {r.id:7s} {r.level:5s} {who}:{r.check or 'expr':18s} "
                     f"第{r.line}行 {r.title}{flag}")
    if ddl_rs.thresholds:
        lines.append("阈值        : " + ", ".join(f"{k}={v}"
                                               for k, v in sorted(ddl_rs.thresholds.items())))
    if ddl_warn:
        lines.append("警告        : " + ddl_warn)
    lines.append("")
    lines.append("✅ DDL 规范加载成功，改 md 后下次调用即生效（无需重启）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sql-review-ddl-mcp",
                                     description="SQL Review DDL - MySQL DDL 预审核 MCP")
    parser.add_argument("--config", default=None, help="settings.yaml 路径")
    parser.add_argument("--transport", default=os.environ.get("DDM_TRANSPORT", "http"),
                        choices=["stdio", "http"])
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check-standard", action="store_true",
                        help="只解析 standards/ddl.md 并打印规则清单，不启动服务")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("DDM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = init_state(args.config)
    except ConfigError as exc:
        log.error("配置加载失败: %s", exc)
        return 2

    # 日志级别以配置为准（未配置时用环境变量/INFO）
    try:
        logging.getLogger().setLevel(settings.log_level)
    except (ValueError, TypeError):
        log.warning("配置的 log.level 无效：%r，沿用默认", settings.log_level)
    if not os.environ.get("DDM_LOG_LEVEL") and settings.log_level:
        os.environ["DDM_LOG_LEVEL"] = settings.log_level

    ddl_rs, ddl_warn = get_ruleset(settings, "ddl", force=True)
    ok = ddl_rs is not None
    if args.check_standard:
        print(standard_report(ddl_rs, ddl_warn=ddl_warn))
        return 0 if ok else 2

    # fail-fast：DDL 规范缺失/写坏 → 直接拒绝启动，不允许带病上线
    if not ok:
        bad = [f"ddl: {ddl_warn}"]
        record(settings.log_dir, _policy(), event="startup_failed", tool="",
               outcome="error",
               reject=make_reject(
                   "DEP_STANDARD_INCOMPLETE",
                   "DDL 规范加载失败，服务拒绝启动；" + "；".join(bad),
                   {"errors": bad, "ddl": str(settings.standard_path)}),
               detail={"version": __import__("sql_review_ddl").version,
                       "standard_version": standard_version(settings)})
        log.error("DDL 规范加载失败，拒绝启动：%s", "；".join(bad))
        log.error("%s", standard_report(ddl_rs, ddl_warn=ddl_warn))
        return 2
    _STATE["ddl_ruleset"] = ddl_rs

    # 启动即做一次留存（30 天删除 / 7 天压缩），并留一行启动审计
    purge(settings.log_dir, _policy(), force=True)
    record(settings.log_dir, _policy(), event="startup", tool="", outcome="info",
           detail={"version": __import__("sql_review_ddl").version,
                   "scope": "ddl",
                   "standard_version": standard_version(settings),
                   "standard_sha256": (ddl_rs.sha256 or "")[:16],
                   "rules_count": len(ddl_rs.rules),
                   "rules_version": ddl_rs.rules_version,
                   "envs": sorted(settings.envs),
                   "port": args.port or settings.port,
                   "auth": "on" if settings.token else "off",
                   "audit_retention_days": settings.audit_retention_days,
                   "audit_compress_after_days": settings.audit_compress_after_days})

    log.info("SQL Review DDL(%s) 启动: envs=%s metadata=%s standard=%s "
             "ddl_rules=%d auth=%s version=%s",
             _display_name(),
             sorted(settings.envs), settings.metadata_backend, standard_version(settings),
             len(ddl_rs.rules), "on" if settings.token else "off",
             __import__("sql_review_ddl").version)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    host = args.host or settings.host
    port = args.port or settings.port
    app = build_http_app(settings)
    if app is None:
        log.warning("未取到 ASGI app，回退 SDK 内置 http 服务（令牌校验改由工具内 _gate 兜底）")
        mcp.run(transport="http", host=host, port=port, path=settings.path)
        return 0
    import uvicorn

    uvicorn.run(app, host=host, port=port,
                log_level=(os.environ.get("DDM_LOG_LEVEL") or settings.log_level).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
