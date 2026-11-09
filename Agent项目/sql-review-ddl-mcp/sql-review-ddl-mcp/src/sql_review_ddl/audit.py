"""审计日志（v1.2.1）：JSON Lines，按天切分，保留 30 天、7 天后压缩。

设计要点
--------
1. **只记事实、语义最小化**：SQL 只落 sha256 与长度，不落明文；不落 UA / 会话内容。
   来源信息两项：``client_ip``（调用方机器 IP）与 ``agent_id``（调用方 AgentID）——
   均由网关透传、由 server 解析后写入（见 settings.yaml 的 ``identity`` 段），
   仅用于"哪台机器、哪个 Agent 调了哪个工具"的追溯与跨系统对账。
   （v1.0.0 及以前只记 ``client_ip``；``identity.record=false`` 可退回旧行为。）
2. **拒绝必留原因**：任何未直接放行的结论都带 ``reject{code,stage,text,evidence}``，
   ``outcome != pass`` 时 ``reject`` 必须非空（自测脚本会扫日志断言）。
3. **留存**：``audit-YYYY-MM-DD.jsonl`` 超过 ``retention_days`` 删除，超过
   ``compress_after_days`` 转 ``.gz``；只在服务启动与每天首次写入时执行，失败只告警。
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("sql_review_ddl.audit")

# 读取用：匹配任意前缀的「前缀-YYYY-MM-DD.jsonl(.gz)」，这样改过前缀也能读到历史文件
FILE_RE = re.compile(r"^(?P<prefix>.+?)-(?P<date>\d{4}-\d{2}-\d{2})"
                     r"(?P<ext>\.jsonl)(?P<gz>\.gz)?$")

# --------------------------------------------------------------------------
# 拒绝 / 未放行原因码表（只增不改：已发布的 code 永远不变）
#   code → (stage, 默认中文说明)
#   stage: auth 鉴权 / args 入参 / proto 协议层 / scope 能力边界 /
#          rule 规范判定 / dep 依赖不可用 / internal 服务内部异常
# --------------------------------------------------------------------------
REJECT_CODES: dict[str, tuple[str, str]] = {
    # ---- 鉴权 ----
    "TOKEN_MISSING": ("auth", "缺少服务准入令牌（请求头 X-SQL-REVIEW-DDL-MCP-TOKEN）"),
    "TOKEN_INVALID": ("auth", "服务准入令牌无效"),
    "TOOL_GATE_DENIED": ("auth", "工具级兜底鉴权未通过（stdio 模式或中间件未生效）"),
    # ---- 入参 ----
    "ARG_SQL_EMPTY": ("args", "SQL 为空"),
    "ARG_ENV_UNSUPPORTED": ("args", "env 只支持 dev/test/stage，收到不支持的值"),
    "ARG_TOO_MANY_STATEMENTS": ("args", "单次最多审核 20 条语句"),
    "ARG_NO_STATEMENT": ("args", "未识别到任何 SQL 语句"),
    "ARG_TOOL_TYPE_MISMATCH": ("args", "该工具不接受此语句类型（请改用 review_ddl）"),
    "ARG_SQL_UNPARSABLE": ("args", "SQL 解析失败，无法审核"),
    "ARG_SQL_TOO_LONG": ("args", "单条请求 SQL 超过长度上限，请分批提交"),
    "ARG_SCOPE_UNSUPPORTED": ("args", "get_rules 的 scope 只支持 ddl / all（本服务只提供 DDL 规则）"),
    # ---- 协议层（框架直接拒，未进入工具） ----
    "PROTO_REJECTED": ("proto", "MCP 协议层/路由层拒绝了请求"),
    # ---- 能力边界（不属拒绝，outcome=info） ----
    "SCOPE_NON_DDL": ("scope", "非 DDL 语句，本服务只审 DDL（DML 请走 SQL Review DML 服务）"),
    "SCOPE_UNCALLABLE": ("scope",
                         "本次没有可审核的语句（本服务只收 DDL），"
                         "SELECT 类查询请走 dba-mcp，DML 请走 SQL Review DML 服务"),
    # ---- 规范判定 ----
    "RULE_REJECTED_DESTRUCTIVE": ("rule",
                                  "物理删除类操作（DROP TABLE / TRUNCATE / DROP DATABASE）一律驳回"),
    "RULE_REJECTED_CTAS": ("rule", "CTAS（建表即灌数据）不允许自动改写"),
    "RULE_REJECTED_DML_DESTRUCTIVE": ("rule",
                                      "DELETE / REPLACE INTO 属不可逆写入，本通道不出审核结论，请改走工单系统"),
    "RULE_REJECTED": ("rule", "存在不可自动修复的违规"),
    "RULE_FIX_REQUIRED": ("rule", "已自动改写，另有需人工确认项（非拒绝）"),
    "RULE_NEED_INFO": ("rule", "缺少事实信息，无法判定（非拒绝，补齐后可复审）"),
    # ---- 依赖 ----
    "DEP_DB_UNREACHABLE": ("dep", "dev/test/stage 只读元数据库不可用"),
    "DEP_STANDARD_INCOMPLETE": ("dep", "规范加载/校验不完整（缺失或格式写错）"),
    # ---- 内部 ----
    "INTERNAL_EXCEPTION": ("internal", "服务内部异常"),
}


def make_reject(code: str, text: str | None = None,
                evidence: dict[str, Any] | None = None) -> dict:
    """构造 reject 块。text 建议写清「违反了什么 + 依据哪条规则 + 规范第几行」。"""
    stage, default_text = REJECT_CODES.get(code, ("internal", code))
    return {
        "code": code,
        "stage": stage,
        "text": (text or default_text).strip(),
        "evidence": evidence or {},
    }


# --------------------------------------------------------------------------
# 留存策略
# --------------------------------------------------------------------------


@dataclass
class AuditPolicy:
    enabled: bool = True
    retention_days: int = 30        # <=0 表示永久保留
    compress_after_days: int = 7    # <=0 表示不压缩
    log_http_errors: bool = True
    filename_prefix: str = "audit"  # 可配置：审计文件名前缀
    ext: str = ".jsonl"             # 可配置：扩展名


def _name_re(policy: "AuditPolicy") -> re.Pattern:
    """按配置生成「本服务自己的」审计文件名正则（清理时只碰这些文件）。"""
    return re.compile(r"^" + re.escape(policy.filename_prefix)
                      + r"-(\d{4}-\d{2}-\d{2})" + re.escape(policy.ext) + r"(\.gz)?$")


_LAST_PURGE = {"day": ""}


def audit_path(log_dir: Path, day: _dt.date | None = None,
               prefix: str = "audit", ext: str = ".jsonl") -> Path:
    day = day or _dt.date.today()
    return Path(log_dir) / f"{prefix}-{day.isoformat()}{ext}"


def purge(log_dir: Path, policy: AuditPolicy, force: bool = False) -> dict:
    """按保留策略删除/压缩历史审计文件。每天最多跑一次；任何异常只告警。"""
    result = {"deleted": 0, "compressed": 0, "errors": 0}
    if not policy.enabled:
        return result
    today = _dt.date.today()
    if not force and _LAST_PURGE.get("day") == today.isoformat():
        return result
    _LAST_PURGE["day"] = today.isoformat()
    d = Path(log_dir)
    if not d.is_dir():
        return result
    try:
        entries = sorted(d.iterdir())
    except OSError as exc:
        log.warning("审计留存扫描失败: %s", exc)
        return result
    own = _name_re(policy)
    for p in entries:
        m = own.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            day = _dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        age = (today - day).days
        gz = bool(m.group(2))
        try:
            if policy.retention_days > 0 and age > policy.retention_days:
                p.unlink()
                result["deleted"] += 1
            elif (policy.compress_after_days > 0 and age >= policy.compress_after_days
                  and not gz):
                tmp = p.with_name(p.name + ".gz.tmp")   # 先写临时名，失败不留半成品
                with p.open("rb") as src, gzip.open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp.replace(p.with_name(p.name + ".gz"))
                p.unlink()
                result["compressed"] += 1
        except OSError as exc:
            result["errors"] += 1
            log.warning("审计留存处理失败 %s: %s", p.name, exc)
    if result["deleted"] or result["compressed"]:
        log.info("审计留存：删除 %d 个、压缩 %d 个（保留 %d 天，%d 天后压缩）",
                 result["deleted"], result["compressed"],
                 policy.retention_days, policy.compress_after_days)
    return result


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

STATUS_BY_OUTCOME = {
    "pass": "ok",
    "fix_required": "ok",
    "need_more_info": "ok",
    "rejected": "ok",
    "info": "ok",
    "invalid_request": "rejected",
    "denied": "denied",
    "error": "error",
}

# 必须有 reject 的结论（"未直接放行"）；pass/info 为 null
NOT_PASS_OUTCOMES = {"denied", "invalid_request", "rejected", "error",
                     "fix_required", "need_more_info"}
REJECTING_OUTCOMES = {"denied", "invalid_request", "rejected", "error"}


def record(log_dir: Path, policy: AuditPolicy, *,
           event: str = "tool_call",
           tool: str = "",
           outcome: str = "info",
           reject: dict | None = None,
           client_ip: str = "",
           agent_id: str = "",            # v1.0.1：网关透传的调用方 AgentID
           socket_ip: str = "",           # 直连方（网关）IP
           client_ip_source: str = "",    # client_ip 取自哪个头 / socket
           agent_id_source: str = "",     # agent_id 取自哪个头
           identity_trusted: bool | None = None,   # 直连方是否可信代理（防伪造）
           sql: str = "",                 # 只用于算 sha256/长度，不落明文
           sql_sha256: str = "",
           standard: dict | None = None,
           detail: dict | None = None,
           status: str | None = None,
           ts: str | None = None) -> dict:
    """写一行审计。返回实际写入的 payload（便于自测/断言复用）。"""
    if sql_sha256:
        sha = sql_sha256
    elif sql:
        sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    else:
        sha = ""
    payload = {
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "tool": tool,
        "status": status or STATUS_BY_OUTCOME.get(outcome, "ok"),   # 兼容旧查询
        "outcome": outcome,                                        # 建议只查这个
        "client_ip": client_ip,                     # 调用方机器 IP（网关透传 → 最左一跳）
        "agent_id": agent_id,                       # 调用方 AgentID（网关透传）
        "socket_ip": socket_ip,                     # 直连方 IP（正常=网关）
        "client_ip_source": client_ip_source,       # socket / X-Forwarded-For / X-Real-IP …
        "agent_id_source": agent_id_source,         # X-Agent-Id / … 或 untrusted-peer
        "identity_trusted": identity_trusted,       # 是否来自可信代理（防伪造）
        "reject": reject,
        "standard": standard,
        "sql_sha256": sha,
        "sql_len": len(sql or ""),
        "detail": detail or {},
    }
    if not policy.enabled:
        return payload
    try:
        purge(log_dir, policy)               # 每天首次写入时顺带做留存
        path = audit_path(log_dir, prefix=policy.filename_prefix, ext=policy.ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  审计失败绝不影响业务
        log.warning("审计写入失败", exc_info=True)
    return payload


# --------------------------------------------------------------------------
# 读取（自测、audit_report.py 共用）
# --------------------------------------------------------------------------


def iter_records(log_dir: Path, days: int | None = None) -> Iterator[dict]:
    """按时间顺序读出审计记录（自动含 .gz）。days=None 表示全部。"""
    d = Path(log_dir)
    if not d.is_dir():
        return
    since = None
    if days and days > 0:
        since = _dt.date.today() - _dt.timedelta(days=days)
    for p in sorted(d.iterdir()):
        m = FILE_RE.match(p.name)
        if not m:
            continue
        try:
            day = _dt.date.fromisoformat(m.group("date"))
        except ValueError:
            continue
        if since and day < since:
            continue
        try:
            if m.group("gz"):
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            yield json.loads(line)
            else:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            yield json.loads(line)
        except (OSError, ValueError) as exc:
            log.warning("审计读取失败 %s: %s", p.name, exc)
