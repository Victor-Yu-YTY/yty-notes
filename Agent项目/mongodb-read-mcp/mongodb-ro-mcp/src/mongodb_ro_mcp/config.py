"""配置加载：config/environments.yaml → Settings。

设计对齐 db-read-mcp v1.0.0 / sql-review-ddl-mcp v1.0.1：

1. **YAML 进版本库、密钥零落盘**：环境连接里的账号口令一律写 `${VAR}`，
   由本模块在启动时从环境变量展开（systemd EnvironmentFile 注入）。
2. **启动期硬校验**（不允许"什么都没校验也在跑"）：
   - `auth.enabled` 且令牌为空 → 拒绝启动；
   - `environments` 为空 → 拒绝启动；
   - 任一环境既没有 `uri` 也没有可拼装的 `hosts`/`user`/`password` → 拒绝启动。
3. **环境别名**：MongoDB 侧内部环境名是 `dev` / `test` / `mirror`，
   而 MySQL 体系与网关编组对外统一叫 `dev` / `test` / `stage`。
   为不改动现有连接串与 DBA 习惯，`mirror` 声明 `aliases: [stage, sim]`，
   调用方传 `stage` 时自动落到 `mirror`，响应里同时回显 `env`（请求名）与
   `env_actual`（实际环境名），审计里两个都记。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

# 允许从项目根目录启动
DEFAULT_CONFIG = "config/environments.yaml"

# ${VAR} 整串引用（值里不允许拼接，避免"看起来是变量其实不是"）
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ConfigError(RuntimeError):
    """配置不可用：启动期直接失败，不带病运行。"""


class UnknownEnvironment(ConfigError):
    """环境名不在配置里（含别名）。"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class MongoEnv:
    """一个 MongoDB 环境。"""

    name: str                      # 内部对外统一用请求名（dev/test/stage）
    actual: str                    # 实际环境名（dev/test/mirror）
    display_name: str
    uri: str
    aliases: list[str] = field(default_factory=list)
    note: str = ""
    connect_timeout_ms: int = 8000


@dataclass
class AuthConfig:
    """唯一一层鉴权：准入令牌（证明"请求来自受管入口 / 网关"）。

    这里**故意没有**用户凭证校验：调用方身份与授权由 AgentID 在网关侧逐请求核验，
    本服务不再自建第二套凭证体系（《MCP 统一鉴权方案》v1.1 §1.3）。
    """

    enabled: bool = True
    header: str = "X-MONGODB-RO-MCP-TOKEN"
    legacy_header: str = "X-MONGODB-MCP-TOKEN"
    token: str = ""
    require_token_on_start: bool = True

    def accepted_headers(self) -> tuple[str, ...]:
        names = [self.header, self.legacy_header]
        return tuple(n.strip().lower() for n in names if n and n.strip())


@dataclass
class GuardConfig:
    """只读防线与输出限额（全部可配置，改这里不用改代码）。"""

    default_limit: int = 20             # find/aggregate 未指定时的条数
    max_limit: int = 100               # 调用方可指定的上限
    timeout_ms: int = 30000            # 单次查询 maxTimeMS
    max_field_chars: int = 2000        # 单个字符串字段截断长度
    max_bytes: int = 1048576           # 单次响应字节上限（1 MiB）
    forbidden_operators: set[str] = field(
        default_factory=lambda: {"$where", "$function", "$accumulator", "$out", "$merge"}
    )
    forbidden_dbs: set[str] = field(default_factory=lambda: {"admin", "config", "local"})
    allow_disk_use: bool = False       # 聚合是否允许落盘（默认禁止）


@dataclass
class AuditConfig:
    """审计日志（<paths.logs>/audit-mongo-YYYY-MM-DD.jsonl）。"""

    enabled: bool = True
    dir: str = ""
    filename_prefix: str = "audit-mongo"
    ext: str = ".jsonl"
    retention_days: int = 30
    compress_after_days: int = 7
    log_http_errors: bool = True        # POST 且 HTTP>=400（协议层拒绝）也留一行
    record_query_text: bool = False     # 默认只落 sha256 与长度，不落查询明文


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
    name: str = "mongodb-ro-mcp"
    display_name: str = "MongoDB Read"
    host: str = "0.0.0.0"
    port: int = 8040
    path: str = "/mcp"


@dataclass
class Settings:
    environments: dict[str, MongoEnv]        # key = 对外请求名（dev/test/stage）
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

    def env(self, name: str) -> MongoEnv:
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
            key = m.group(1)
            if key not in os.environ:
                raise ConfigError(
                    "环境变量未设置: %s（应在 /etc/%s/%s.env 里注入，见部署说明）"
                    % (key, "mongodb-ro-mcp", "mongodb-ro-mcp")
                )
            return os.environ[key]
        return value
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


# --------------------------------------------------------------------------
# 连接串拼装
# --------------------------------------------------------------------------
def _build_uri(raw: dict[str, Any]) -> str:
    """优先用显式 uri；否则由 hosts/user/password 等字段拼装（口令做 URL 转义）。"""
    explicit = str(raw.get("uri") or "").strip()
    if explicit:
        return explicit

    hosts = str(raw.get("hosts") or "").strip()
    user = str(raw.get("user") or "").strip()
    password = str(raw.get("password") or "")
    if not hosts or not user or password == "":
        raise ConfigError(
            "环境缺少连接信息：需要 uri，或 hosts + user + password（当前 hosts=%r user=%r）"
            % (hosts, user)
        )

    opts = [("authSource", str(raw.get("auth_source") or "admin"))]
    if raw.get("replica_set"):
        opts.insert(0, ("replicaSet", str(raw["replica_set"])))
    if raw.get("read_preference"):
        opts.append(("readPreference", str(raw["read_preference"])))
    if raw.get("auth_mechanism"):
        opts.append(("authMechanism", str(raw["auth_mechanism"])))
    opts.append(("retryWrites", "false"))          # 只读服务，关掉可重试写
    if raw.get("app_name"):
        opts.append(("appName", str(raw["app_name"])))
    query = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in opts)
    return "mongodb://%s:%s@%s/?%s" % (quote(user, safe=""), quote(password, safe=""), hosts, query)


def _build_env(raw_name: str, raw: dict[str, Any]) -> MongoEnv:
    aliases = [str(a) for a in (raw.get("aliases") or []) if str(a).strip()]
    return MongoEnv(
        name=raw_name,
        actual=str(raw.get("actual") or raw_name),
        display_name=str(raw.get("display_name") or raw_name),
        uri=_build_uri(raw),
        aliases=aliases,
        note=str(raw.get("note") or ""),
        connect_timeout_ms=int(raw.get("connect_timeout_ms", 8000)),
    )


def _pick(cls: type, raw: dict[str, Any] | None) -> Any:
    """只取 dataclass 已声明的字段，忽略 YAML 里的说明性多余键。"""
    raw = raw or {}
    return cls(**{k: v for k, v in raw.items() if k in cls.__annotations__})


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    cfg_path = Path(path) if path else Path(os.environ.get("MONGO_MCP_CONFIG", DEFAULT_CONFIG))
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
    environments: dict[str, MongoEnv] = {}
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
            "准入令牌为空：/etc/mongodb-ro-mcp/mongodb-ro-mcp.env 里的 GATEWAY_TOKEN 没有注入。"
            "本服务禁止『无令牌启动』，请先补令牌再启动（见部署说明第 3 步）。"
        )

    # ---- 审计目录：相对路径相对安装根目录 ----
    audit = _pick(AuditConfig, raw.get("audit"))
    pkg_root = Path(__file__).resolve().parents[2]
    audit_dir = str(audit.dir or "logs")
    audit.dir = audit_dir if os.path.isabs(audit_dir) else str((pkg_root / audit_dir).resolve())
    audit.filename_prefix = str(audit.filename_prefix or "audit-mongo")

    # ---- 只读防线 ----
    guard_raw = raw.get("guard") or {}
    guard = _pick(GuardConfig, guard_raw)
    if guard_raw.get("forbidden_operators"):
        guard.forbidden_operators = {str(x) for x in guard_raw["forbidden_operators"]}
    if guard_raw.get("forbidden_dbs"):
        guard.forbidden_dbs = {str(x).lower() for x in guard_raw["forbidden_dbs"]}
    guard.default_limit = max(1, int(guard.default_limit))
    guard.max_limit = max(guard.default_limit, int(guard.max_limit))
    guard.timeout_ms = max(1000, int(guard.timeout_ms))
    guard.max_field_chars = max(100, int(guard.max_field_chars))
    guard.max_bytes = max(16 * 1024, int(guard.max_bytes))

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
    server.host = os.environ.get("MONGO_MCP_HOST", server.host)
    server.port = int(os.environ.get("MONGO_MCP_PORT", server.port))

    return Settings(
        environments=environments,
        server=server,
        auth=auth,
        guard=guard,
        audit=audit,
        identity=identity,
    )
