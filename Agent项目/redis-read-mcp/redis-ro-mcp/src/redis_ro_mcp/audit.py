"""审计日志：JSON Lines，按天切分，自动留存（删除 / 压缩）。

设计要点（与 sql-review-ddl-mcp v1.0.1 / mongodb-ro-mcp 的 audit.py 同口径）
------------------------------------------------------------------
1. **只记事实、语义最小化**：默认**不落键值明文**，只落 `query_sha256` 与
   `query_len`（`audit.record_query_text: true` 时才落 ≤500 字符的清洗摘要）。
   Redis 侧的"查询"指 key 或 SCAN pattern 的指纹。
2. **来源信息两项**：`client_ip`（调用方机器 IP）与 `agent_id`（调用方 AgentID），
   均由网关透传、由 identity.py 解析后写入 —— 用于"哪台机器、哪个 Agent 调了哪个
   工具"。另记 `socket_ip` / `*_source` / `identity_trusted` 三项佐证（防伪造）。
3. **拒绝必留原因**：任何未直接放行的结论都带 `reject{code,stage,text,evidence}`。
4. **留存**：`<prefix>-YYYY-MM-DD.jsonl` 超过 `retention_days` 删除，超过
   `compress_after_days` 转 `.gz`；只在每天首次写入时执行，失败只告警。
5. **审计绝不阻断业务**：任何异常只写 WARNING。

字段映射（Redis 侧沿用同一份 JSONL schema，便于与 MySQL/MongoDB 服务统一用 jq 分析）：
    database    = 库序号，形如 "db0"
    collection  = 被访问的 key（无单 key 语义的工具为 None，细节看 detail）
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("redis_ro_mcp.audit")

_lock = threading.Lock()

# 读取用：匹配任意前缀的「前缀-YYYY-MM-DD.jsonl(.gz)」
FILE_RE = re.compile(
    r"^(?P<prefix>.+?)-(?P<date>\d{4}-\d{2}-\d{2})(?P<ext>\.jsonl)(?P<gz>\.gz)?$"
)

# --------------------------------------------------------------------------
# 拒绝 / 未放行原因码表（只增不改：已发布的 code 永远不变）
#   stage: auth 鉴权 / args 入参 / proto 协议层 / guard 只读防线 /
#          dep 依赖不可用 / internal 服务内部异常
# --------------------------------------------------------------------------
REJECT_CODES: dict[str, tuple[str, str]] = {
    # ---- 鉴权 ----
    "TOKEN_MISSING": ("auth", "缺少服务准入令牌（请求头 X-REDIS-RO-MCP-TOKEN）"),
    "TOKEN_INVALID": ("auth", "服务准入令牌无效"),
    # ---- 入参 ----
    "ARG_ENV_UNKNOWN": ("args", "env 不是本服务登记的环境（dev/test/stage）"),
    "ARG_KEY_INVALID": ("args", "key 不合法（含控制字符、超长或为空）"),
    "ARG_PATTERN_INVALID": ("args", "SCAN pattern 不合法（含控制字符或超长）"),
    "ARG_LIMIT_INVALID": ("args", "limit/count 必须是整数且在允许范围内"),
    "ARG_RANGE_INVALID": ("args", "start/stop/offset/length 区间不合法"),
    "ARG_SCORE_INVALID": ("args", "min_score/max_score 必须是数字、-inf、+inf 或 (数字"),
    # ---- 协议层（框架直接拒，未进入工具） ----
    "PROTO_REJECTED": ("proto", "MCP 协议层/路由层拒绝了请求"),
    # ---- 只读防线 ----
    "GUARD_COMMAND_DENIED": ("guard", "命令不在只读白名单内（写命令一律拒绝）"),
    # ---- 依赖 ----
    "DEP_REDIS_UNREACHABLE": ("dep", "目标 Redis/Predixy 环境不可达"),
    "DEP_AUTH_FAILED": ("dep", "Redis 认证失败（口令或 ACL 用户不正确）"),
    "DEP_PROXY_DENIED": ("dep", "命令被 Predixy 代理白名单拒绝（非本服务缺陷）"),
    "DEP_TIMEOUT": ("dep", "命令执行超时（socket_timeout 到点）"),
    # ---- 内部 ----
    "INTERNAL_EXCEPTION": ("internal", "服务内部异常"),
}


def make_reject(code: str, text: str | None = None,
                evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    stage, default_text = REJECT_CODES.get(code, ("internal", code))
    return {
        "code": code,
        "stage": stage,
        "text": (text or default_text).strip(),
        "evidence": evidence or {},
    }


STATUS_BY_OUTCOME = {
    "ok": "ok",
    "info": "ok",
    "denied": "denied",
    "invalid_request": "rejected",
    "rejected": "rejected",
    "error": "error",
}


# --------------------------------------------------------------------------
# 留存策略
# --------------------------------------------------------------------------
@dataclass
class AuditPolicy:
    enabled: bool = True
    filename_prefix: str = "audit-redis"
    ext: str = ".jsonl"
    retention_days: int = 30
    compress_after_days: int = 7

    @classmethod
    def from_config(cls, cfg: Any) -> "AuditPolicy":
        return cls(
            enabled=bool(getattr(cfg, "enabled", True)),
            filename_prefix=str(getattr(cfg, "filename_prefix", "audit-redis") or "audit-redis"),
            ext=str(getattr(cfg, "ext", ".jsonl") or ".jsonl"),
            retention_days=int(getattr(cfg, "retention_days", 30)),
            compress_after_days=int(getattr(cfg, "compress_after_days", 7)),
        )


def _own_re(policy: AuditPolicy) -> re.Pattern:
    """按配置生成「本服务自己的」审计文件名正则（清理时只碰这些文件）。"""
    return re.compile(
        r"^" + re.escape(policy.filename_prefix)
        + r"-(\d{4}-\d{2}-\d{2})" + re.escape(policy.ext) + r"(\.gz)?$"
    )


_LAST_PURGE = {"day": ""}


def audit_path(log_dir: str | Path, day: _dt.date | None = None,
               prefix: str = "audit-redis", ext: str = ".jsonl") -> Path:
    day = day or _dt.date.today()
    return Path(log_dir) / ("%s-%s%s" % (prefix, day.isoformat(), ext))


def purge(log_dir: str | Path, policy: AuditPolicy, force: bool = False) -> dict[str, int]:
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

    own = _own_re(policy)
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
                tmp = p.with_name(p.name + ".gz.tmp")      # 先写临时名，失败不留半成品
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


def fingerprint(payload: Any) -> tuple[str, int]:
    """查询指纹：只留 sha256 与长度，永不落明文（Redis 侧等价于 SQL 的 sql_sha256）。"""
    if payload in (None, ""):
        return "", 0
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text)


def preview(payload: Any, limit: int = 500) -> str:
    """可选：查询摘要（仅 audit.record_query_text=true 时使用），先清洗再截断。"""
    if payload in (None, ""):
        return ""
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, default=str
    )
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit]


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def record(log_dir: str | Path, policy: AuditPolicy, *,
           event: str = "tool_call",
           tool: str = "",
           outcome: str = "ok",
           reject: dict | None = None,
           env: str | None = None,
           env_actual: str | None = None,
           client_ip: str = "",
           agent_id: str = "",
           socket_ip: str = "",
           client_ip_source: str = "",
           agent_id_source: str = "",
           identity_trusted: bool | None = None,
           database: str | None = None,
           collection: str | None = None,
           query_sha256: str = "",
           query_len: int = 0,
           query_text: str = "",
           detail: dict | None = None,
           ts: str | None = None) -> dict[str, Any]:
    """写一行审计。返回实际写入的 payload（便于自测/断言复用）。"""
    payload: dict[str, Any] = {
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "tool": tool,
        "status": STATUS_BY_OUTCOME.get(outcome, "ok"),
        "outcome": outcome,
        # 来源：网关透传的「机器 IP + AgentID」（只追溯，不授权）
        "client_ip": client_ip,
        "agent_id": agent_id,
        "socket_ip": socket_ip,
        "client_ip_source": client_ip_source,
        "agent_id_source": agent_id_source,
        "identity_trusted": identity_trusted,
        # 业务上下文
        "env": env,
        "env_actual": env_actual,
        "database": database,          # redis: "db0"
        "collection": collection,      # redis: 被访问的 key
        "query_sha256": query_sha256,
        "query_len": query_len,
        "query_text": query_text,
        "reject": reject,
        "detail": detail or {},
    }
    if not policy.enabled:
        return payload

    try:
        purge(log_dir, policy)                    # 每天首次写入时顺带做留存
        path = audit_path(log_dir, prefix=policy.filename_prefix, ext=policy.ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:                             # noqa: BLE001 审计失败绝不影响业务
        log.warning("审计写入失败", exc_info=True)
    return payload


# --------------------------------------------------------------------------
# 读取（自测、验收脚本共用）
# --------------------------------------------------------------------------
def iter_records(log_dir: str | Path, days: int | None = None) -> Iterator[dict[str, Any]]:
    """按时间顺序读出审计记录（自动含 .gz）。days=None 表示全部。"""
    d = Path(log_dir)
    if not d.is_dir():
        return
    since = _dt.date.today() - _dt.timedelta(days=days) if days and days > 0 else None
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
                import gzip as _gz
                with _gz.open(p, "rt", encoding="utf-8") as f:
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
