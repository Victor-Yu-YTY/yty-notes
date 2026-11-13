"""配置加载：config/environments.yaml → Settings。

设计对齐 db-read-mcp v1.0.0 / sql-review-ddl-mcp v1.0.1 / mongodb-ro-mcp：

1. **YAML 进版本库、密钥零落盘**：口令一律写 `${VAR}`，由本模块在启动时从环境变量
   展开（systemd EnvironmentFile 注入）。支持两种引用：
   - `${VAR}`   —— **必填**，环境变量缺失即启动失败（适用于"必须有口令"的环境）；
   - `${VAR:-}` —— **可选**，缺失时展开成空串（当前 Redis 实例无密码，日后加密也不必改配置）。
2. **启动期硬校验**（不允许"什么都没校验也在跑"）：
   - `auth.enabled` 且令牌为空 → 拒绝启动；
   - `environments` 为空 → 拒绝启动；
   - 任一环境缺 `host` / `port` / `db` 不合法 → 拒绝启动。
3. **环境别名**：Redis 侧内部环境名是 `dev` / `test` / `mirror`，
   而 MySQL/MongoDB 体系与网关编组对外统一叫 `dev` / `test` / `stage`。
   为不改动 DBA 习惯与既有连接信息，`mirror` 声明为 `stage` 的 `actual`，
   并把 `mirror` / `sim` 作为别名：调用方传 `stage`、`mirror`、`sim` 都落到同一实例；
   响应里同时回显 `env`（规范名）与 `env_actual`（实际环境名），审计里两个都记。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 允许从项目根目录启动
DEFAULT_CONFIG = "config/environments.yaml"

# ${VAR} / ${VAR:-} 整串引用（值里不允许拼接，避免"看起来是变量其实不是"）
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(:-)?\}$")

# Redis 侧只读白名单（与 README §2 的 Predixy 实测表一一对应）
DEFAULT_ALLOWED_COMMANDS = {
    "ping", "info", "config|get",
    "scan", "randomkey", "type", "ttl", "exists",
    "strlen", "getrange",
    "lrange", "llen", "hgetall", "hget", "hlen", "hkeys",
    "smembers", "scard", "zrange", "zcard", "zrangebyscore",
}

# 写命令黑名单：源码里**一个都不许出现**（selftest/verify.sh 做静态断言）
DEFAULT_FORBIDDEN_COMMANDS = {
    "set", "setnx", "setex", "psetex", "getset", "append", "setrange", "mset", "msetnx",
    "incr", "decr", "incrby", "decrby", "incrbyfloat",
    "hset", "hmset", "hsetnx", "hincrby", "hincrbyfloat", "hdel",
    "lpush", "rpush", "lpushx", "rpushx", "lpop", "rpop", "lset", "linsert", "ltrim", "lrem",
    "sadd", "srem", "spop", "smove",
    "zadd", "zrem", "zincrby", "zremrangebyrank", "zremrangebyscore", "zpopmin", "zpopmax",
    "del", "unlink", "expire", "pexpire", "expireat", "persist", "rename", "renamenx",
    "move", "copy", "restore", "flushall", "flushdb", "swapdb",
    "eval", "evalsha", "script", "function", "multi", "exec", "watch", "publish", "spublish",
}

# Predixy 1.0.5 实测拒绝的命令（2026-11-10 三环境）：工具面必须绕开
DEFAULT_PROXY_REJECTED = {"dbsize", "object", "memory", "slowlog", "client", "cluster"}


class ConfigError(RuntimeError):
    """配置不可用：启动期直接失败，不带病运行。"""


class UnknownEnvironment(ConfigError):
    """环境名不在配置里（含别名）。"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class RedisEnv:
    """一个 Redis 环境（经 Predixy 代理访问）。"""

    name: str                      # 对外请求名（dev/test/stage）
    actual: str                    # 实际环境名（dev/test/mirror）
    display_name: str
    host: str
    port: int = 25900
    db: int = 0
    password: str = ""             # 当前实例无密码；日后加密时由 ${VAR} 注入
    username: str = ""             # Redis 6+ ACL 用户；5.0.6 恒为空
    aliases: list[str] = field(default_factory=list)
    note: str = ""
    socket_timeout_ms: int = 5000
    connect_timeout_ms: int = 5000
    protocol: int = 2              # ★ 必须 2：Redis 5 不认 RESP3 的 HELLO 握手
    lib_name: str = ""             # ★ 必须空：Predixy 白名单拒绝 CLIENT SETINFO

    @property
    def endpoint(self) -> str:
        """不含口令的连接描述（日志/响应/预检共用）。"""
        return "%s:%d/db%d" % (self.host, self.port, self.db)

    @property
    def has_auth(self) -> bool:
        return bool(self.password or self.username)


@dataclass
class AuthConfig:
    """唯一一层鉴权：准入令牌（证明"请求来自受管入口 / 网关"）。

    这里**故意没有**用户凭证校验：调用方身份与授权由 AgentID 在网关侧逐请求核验，
    本服务不再自建第二套凭证体系（《MCP 统一鉴权方案》v1.1 §1.3）。
    """

    enabled: bool = True
    header: str = "X-REDIS-RO-MCP-TOKEN"
    legacy_header: str = "X-REDIS-MCP-TOKEN"
    token: str = ""
    require_token_on_start: bool = True

    def accepted_headers(self) -> tuple[str, ...]:
        names = [self.header, self.legacy_header]
        return tuple(n.strip().lower() for n in names if n and n.strip())


@dataclass
class GuardConfig:
    """只读防线与输出限额（全部可配置，改这里不用改代码）。"""

    default_count: int = 100           # SCAN 单轮 COUNT
    max_count: int = 500               # 调用方可指定的 COUNT 上限
    max_members: int = 100             # 集合类单次返回成员上限
    max_limit: int = 500               # find_keys 收集上限
    max_sample: int = 2000             # bigkey_scan 采样上限
    max_top: int = 100                 # bigkey_scan Top N 上限
    max_field_chars: int = 2000        # 单个字符串值截断长度
    max_bytes: int = 1048576           # 单次响应字节上限（1 MiB）
    max_scan_rounds: int = 60          # find_keys/bigkey_scan 的 SCAN 轮数上限
    max_key_len: int = 512             # key 长度上限
    max_pattern_len: int = 256         # SCAN pattern 长度上限
    socket_timeout_ms: int = 5000      # 单条命令超时
    connect_timeout_ms: int = 5000     # 建连超时
    allowed_commands: set[str] = field(
        default_factory=lambda: set(DEFAULT_ALLOWED_COMMANDS)
    )
    forbidden_commands: set[str] = field(
        default_factory=lambda: set(DEFAULT_FORBIDDEN_COMMANDS)
    )
    proxy_rejected_commands: set[str] = field(
        default_factory=lambda: set(DEFAULT_PROXY_REJECTED)
    )


@dataclass
class AuditConfig:
    """审计日志（<paths.logs>/audit-redis-YYYY-MM-DD.jsonl）。"""

    enabled: bool = True
    dir: str = ""
    filename_prefix: str = "audit-redis"
    ext: str = ".jsonl"
    retention_days: int = 30
    compress_after_days: int = 7
    log_http_errors: bool = True        # POST 且 HTTP>=400（协议层拒绝）也留一行
    record_query_text: bool = False     # 默认只落 sha256 与长度，不落 key 明文


@dataclass
class IdentityConfig:
    """网关透传的调用方身份（只写审计，不参与授权判定）。"""

    record: bool = True
    agent_id_headers: list[str] = field(
        default_factory=lambda: ["X-Agent-Id", "X-AgentID", "X-Agent-ID", "Agent-Id"]
    )
    client_ip_headers: list[str] = field(
        default_factory=lambda: ["X-Forwarded-For", "X-Real-IP", "X-Client-IP"]
    )
    trusted_proxies: list[str] = field(
        default_factory=lambda: ["10.0.0.219", "127.0.0.1", "::1"]
    )


@dataclass
class ServerConfig:
    name: str = "redis-ro-mcp"
    display_name: str = "Redis Read"
    host: str = "0.0.0.0"
    port: int = 8050
    path: str = "/mcp"


@dataclass
class Settings:
    environments: dict[str, RedisEnv]        # key = 对外请求名（dev/test/stage）
    server: ServerConfig
    auth: AuthConfig
    guard: GuardConfig
    audit: AuditConfig
    identity: IdentityConfig

    def resolve(self, name: str) -> str:
        """请求名 → 对外请求名（先按请求名精确匹配，再按 aliases / 实际名兜底）。"""
        key = (name or "").strip()
        if not key:
            raise UnknownEnvironment("env 不能为空，可选: %s" % sorted(self.environments))
        if key in self.environments:
            return key
        low = key.lower()
        for req, env in self.environments.items():
            if low == req.lower() or low == env.actual.lower():
                return req
            if any(low == a.lower() for a in env.aliases):
                return req
        raise UnknownEnvironment(
            "未知环境: %s，可选: %s" % (name, sorted(self.environments))
        )

    def env(self, name: str) -> RedisEnv:
        return self.environments[self.resolve(name)]

    def names(self) -> list[str]:
        return sorted(self.environments)


# --------------------------------------------------------------------------
# ${VAR} 展开
# --------------------------------------------------------------------------
def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            key, optional = m.group(1), bool(m.group(2))
            if key not in os.environ:
                if optional:
                    return ""
                raise ConfigError(
                    "环境变量未设置: %s（应在 /etc/redis-ro-mcp/redis-ro-mcp.env 里注入，"
                    "见部署说明）" % key
                )
            return os.environ[key]
        return value
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


# --------------------------------------------------------------------------
# 环境构造
# --------------------------------------------------------------------------
def _build_env(raw_name: str, raw: dict[str, Any]) -> RedisEnv:
    aliases = [str(a) for a in (raw.get("aliases") or []) if str(a).strip()]
    host = str(raw.get("host") or "").strip()
    if not host:
        raise ConfigError(
            "环境 %s 缺少 host（Predixy 代理地址），如 host: 10.0.0.32" % raw_name
        )
    port = int(raw.get("port", 25900))
    db = int(raw.get("db", 0))
    if not (1 <= port <= 65535):
        raise ConfigError("环境 %s 的 port 不合法: %r" % (raw_name, port))
    if db < 0:
        raise ConfigError("环境 %s 的 db 不合法: %r（应为非负整数）" % (raw_name, db))
    return RedisEnv(
        name=raw_name,
        actual=str(raw.get("actual") or raw_name),
        display_name=str(raw.get("display_name") or raw_name),
        host=host,
        port=port,
        db=db,
        password=str(raw.get("password") or ""),
        username=str(raw.get("username") or ""),
        aliases=aliases,
        note=str(raw.get("note") or ""),
        socket_timeout_ms=int(raw.get("socket_timeout_ms", 5000)),
        connect_timeout_ms=int(raw.get("connect_timeout_ms", 5000)),
        protocol=int(raw.get("protocol", 2)),
        lib_name=str(raw.get("lib_name", "") or ""),
    )


def _pick(cls: type, raw: dict[str, Any] | None) -> Any:
    """只取 dataclass 已声明的字段，忽略 YAML 里的说明性多余键。"""
    raw = raw or {}
    return cls(**{k: v for k, v in raw.items() if k in cls.__annotations__})


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    cfg_path = Path(path) if path else Path(os.environ.get("REDIS_MCP_CONFIG", DEFAULT_CONFIG))
    if not cfg_path.is_absolute() and not cfg_path.exists():
        candidate = Path(__file__).resolve().parents[2] / cfg_path   # <包根>/config/...
        if candidate.exists():
            cfg_path = candidate
    if not cfg_path.exists():
        raise ConfigError("配置文件不存在: %s（可用 --config 指定）" % cfg_path)

    raw = _resolve(yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {})
    if not raw.get("environments"):
        raise ConfigError("配置文件未定义 environments: %s" % cfg_path)

    # ---- 环境 ----
    environments: dict[str, RedisEnv] = {}
    for raw_name, raw_env in raw["environments"].items():
        env = _build_env(str(raw_name), raw_env or {})
        environments[env.name] = env

    # 别名冲突检查：同一个别名不许指向两个环境
    seen: dict[str, str] = {}
    for env in environments.values():
        seen[env.name.lower()] = env.name
    for env in environments.values():
        for alias in env.aliases:
            key = alias.lower()
            if key in seen and seen[key] != env.name:
                raise ConfigError(
                    "环境别名冲突：%r 同时指向 %s 与 %s" % (alias, seen[key], env.name)
                )
            seen[key] = env.name

    # ---- 鉴权（启动期硬校验）----
    auth = _pick(AuthConfig, raw.get("auth"))
    auth.token = (auth.token or "").strip()
    if auth.enabled and not auth.accepted_headers():
        raise ConfigError("auth.header 未配置：准入令牌没有头名可校验")
    if auth.enabled and auth.require_token_on_start and not auth.token:
        raise ConfigError(
            "准入令牌为空：/etc/redis-ro-mcp/redis-ro-mcp.env 里的 GATEWAY_TOKEN 没有注入。"
            "本服务禁止『无令牌启动』，请先补令牌再启动（见部署说明第 3 步）。"
        )

    # ---- 审计目录：相对路径相对安装根目录 ----
    audit = _pick(AuditConfig, raw.get("audit"))
    pkg_root = Path(__file__).resolve().parents[2]
    audit_dir = str(audit.dir or "logs")
    audit.dir = audit_dir if os.path.isabs(audit_dir) else str((pkg_root / audit_dir).resolve())
    audit.filename_prefix = str(audit.filename_prefix or "audit-redis")

    # ---- 只读防线 ----
    guard_raw = raw.get("guard") or {}
    guard = _pick(GuardConfig, guard_raw)
    for attr, default_set in (
        ("allowed_commands", DEFAULT_ALLOWED_COMMANDS),
        ("forbidden_commands", DEFAULT_FORBIDDEN_COMMANDS),
        ("proxy_rejected_commands", DEFAULT_PROXY_REJECTED),
    ):
        values = guard_raw.get(attr)
        if values:
            setattr(guard, attr, {str(x).strip().lower() for x in values if str(x).strip()})
        else:
            setattr(guard, attr, {str(x).lower() for x in default_set})
    guard.default_count = max(1, int(guard.default_count))
    guard.max_count = max(guard.default_count, int(guard.max_count))
    guard.max_members = max(1, int(guard.max_members))
    guard.max_limit = max(1, int(guard.max_limit))
    guard.max_sample = max(10, int(guard.max_sample))
    guard.max_top = max(1, int(guard.max_top))
    guard.max_field_chars = max(100, int(guard.max_field_chars))
    guard.max_bytes = max(16 * 1024, int(guard.max_bytes))
    guard.max_scan_rounds = max(1, int(guard.max_scan_rounds))
    guard.max_key_len = max(1, int(guard.max_key_len))
    guard.max_pattern_len = max(1, int(guard.max_pattern_len))
    guard.socket_timeout_ms = max(100, int(guard.socket_timeout_ms))
    guard.connect_timeout_ms = max(100, int(guard.connect_timeout_ms))

    # 环境级超时缺省跟随 guard（环境里写了的以环境为准）
    for env in environments.values():
        if env.socket_timeout_ms <= 0:
            env.socket_timeout_ms = guard.socket_timeout_ms
        if env.connect_timeout_ms <= 0:
            env.connect_timeout_ms = guard.connect_timeout_ms

    # ---- 身份 ----
    ident_raw = raw.get("identity") or {}
    identity = _pick(IdentityConfig, ident_raw)
    identity.agent_id_headers = [str(x) for x in (identity.agent_id_headers or [])]
    identity.client_ip_headers = [str(x) for x in (identity.client_ip_headers or [])]
    identity.trusted_proxies = [str(x) for x in (identity.trusted_proxies or [])]

    # ---- 服务 ----
    server = _pick(ServerConfig, raw.get("server"))
    if not str(server.path).startswith("/"):
        server.path = "/" + str(server.path)

    # 环境变量可覆盖监听面（部署脚本与 systemd 用命令行参数，这里只做兜底）
    server.host = os.environ.get("REDIS_MCP_HOST", server.host)
    server.port = int(os.environ.get("REDIS_MCP_PORT", server.port))

    return Settings(
        environments=environments,
        server=server,
        auth=auth,
        guard=guard,
        audit=audit,
        identity=identity,
    )
