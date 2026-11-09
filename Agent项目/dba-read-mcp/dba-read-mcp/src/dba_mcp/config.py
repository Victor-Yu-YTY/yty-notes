"""配置加载：environments.yaml -> Settings。

v0.2.4 变更（只做一件事：下掉假鉴权）：
  - 移除第二层鉴权配置：`auth.mode` / `auth.verify_url` / `auth.timeout` /
    `auth.cache_ttl` / `auth.scope_template` 全部不再存在；
  - 原 `auth.gateway` 子段上提为 `auth` 本层（准入令牌是唯一的鉴权配置）；
  - 新增一条启动期硬校验：`auth.require_token_on_start` 且令牌为空 -> 拒绝启动。

**本版不动监听面**：`server.host` 仍是 0.0.0.0、`server.port` 仍是 8000，
网关侧登记的上游地址与调用方式完全不变。监听收敛（回环 + nginx 前置）
是下一版的事，见《交付说明》。

密码等敏感值在 yaml 中以 ${ENV_NAME} 形式引用，在此处从环境变量展开，
保证 yaml 文件本身可以进版本库。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# 库名 / 表名 / 列名白名单，用于防止拼接 SQL 时被注入
IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")


class ConfigError(RuntimeError):
    pass


class UnknownEnvironment(ConfigError):
    """环境名不在配置里。"""


def _resolve(value: Any) -> Any:
    """把 ${VAR} 展开为环境变量值。"""
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"环境变量未设置: {name}")
            return os.environ[name]
        return value
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


@dataclass
class DbEnv:
    name: str
    display_name: str
    host: str
    port: int
    user: str
    password: str
    database: str | None
    pool_size: int
    charset: str
    connect_timeout: int
    read_timeout: int
    source_instance_map: dict[str, str] = field(default_factory=dict)


@dataclass
class AuthConfig:
    """唯一一层鉴权：准入令牌（证明"请求来自受管入口 / 网关"）。

    这里**故意没有** mode / verify_url / scope 之类字段：
    调用方的身份与授权由 AgentID 在网关侧逐请求核验，MCP 不参与，
    也不再自建第二套用户凭证体系（《MCP 统一鉴权方案》v1.1 §1.3）。
    """

    enabled: bool = True
    header: str = "X-DB-READ-MCP-TOKEN"
    legacy_header: str = ""          # 兼容期旧头名；空字符串表示不接受
    token: str = ""
    require_token_on_start: bool = True

    def accepted_headers(self) -> tuple[str, ...]:
        """返回全部可接受的请求头名（小写），新头名在前。"""
        names = [self.header, self.legacy_header]
        return tuple(n.strip().lower() for n in names if n and n.strip())


@dataclass
class GuardConfig:
    default_limit: int = 200
    max_limit: int = 1000
    statement_timeout_ms: int = 3000
    forbidden_schemas: set[str] = field(default_factory=lambda: {"mysql", "performance_schema", "sys"})


@dataclass
class CacheConfig:
    path: str
    metadata_ttl: int = 3600
    query_ttl: int = 300


@dataclass
class ServerConfig:
    name: str = "dba-mcp"
    host: str = "0.0.0.0"
    port: int = 8000
    path: str = "/mcp"


@dataclass
class ExportConfig:
    """查询结果导出：每次工具调用落一个 .json 文件，供研发下载（本版保持不变）。"""

    enabled: bool = True
    dir: str = ""
    ttl_seconds: int = 3600
    base_url: str = ""
    sign_with_gateway_token: bool = True


@dataclass
class Settings:
    environments: dict[str, DbEnv]
    auth: AuthConfig
    guard: GuardConfig
    cache: CacheConfig
    audit_dir: str
    server: ServerConfig
    export: ExportConfig

    def env(self, name: str) -> DbEnv:
        try:
            return self.environments[name]
        except KeyError:
            raise UnknownEnvironment(
                f"未知环境: {name}，可选: {sorted(self.environments)}"
            ) from None


def _build_db_env(name: str, raw: dict[str, Any]) -> DbEnv:
    required = ("host", "user", "password")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ConfigError(f"环境 {name} 缺少必填字段: {missing}")
    return DbEnv(
        name=name,
        display_name=raw.get("display_name", name),
        host=raw["host"],
        port=int(raw.get("port", 3306)),
        user=raw["user"],
        password=raw["password"],
        database=raw.get("database") or None,
        pool_size=int(raw.get("pool_size", 5)),
        charset=raw.get("charset", "utf8mb4"),
        connect_timeout=int(raw.get("connect_timeout", 5)),
        read_timeout=int(raw.get("read_timeout", 10)),
        source_instance_map=dict(raw.get("source_instance_map") or {}),
    )


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    cfg_path = Path(path) if path else Path(os.environ.get("DBA_MCP_CONFIG", "config/environments.yaml"))
    if not cfg_path.is_absolute() and not cfg_path.exists():
        # 允许从项目根目录启动
        candidate = Path(__file__).resolve().parents[2] / cfg_path
        if candidate.exists():
            cfg_path = candidate
    if not cfg_path.exists():
        raise ConfigError(f"配置文件不存在: {cfg_path}")

    raw = _resolve(yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {})
    if not raw.get("environments"):
        raise ConfigError(f"配置文件未定义 environments: {cfg_path}")

    cache_raw = raw.get("cache") or {}
    if not cache_raw.get("path"):
        raise ConfigError("cache.path 未配置")

    server_raw = raw.get("server") or {}
    server = ServerConfig(**{k: v for k, v in server_raw.items() if k in ServerConfig.__annotations__})

    auth = AuthConfig(**{k: v for k, v in (raw.get("auth") or {}).items() if k in AuthConfig.__annotations__})
    if auth.require_token_on_start and auth.enabled and not (auth.token or "").strip():
        raise ConfigError(
            "准入令牌为空：/etc/dba-mcp/dba-mcp.env 里的 GATEWAY_TOKEN 没有注入。"
            "本版禁止『无令牌启动』，请先补令牌再启动（见部署文档第 3 步）。"
        )
    if auth.enabled and not auth.accepted_headers():
        raise ConfigError("auth.header 未配置：准入令牌没有头名可校验")

    export_raw = raw.get("export") or {}
    export_dir = str(
        export_raw.get("dir")
        or os.path.join(os.path.dirname(cache_raw["path"]) or ".", "exports")
    )
    # export.base_url：优先环境变量 EXPORT_BASE_URL，其次 yaml，最后按服务地址兜底
    export_base = str(
        os.environ.get("EXPORT_BASE_URL")
        or export_raw.get("base_url")
        or f"http://{server_raw.get('host', '127.0.0.1')}:{server_raw.get('port', 8000)}"
    ).rstrip("/")
    if "0.0.0.0" in export_base:
        export_base = export_base.replace("0.0.0.0", "127.0.0.1")

    return Settings(
        environments={name: _build_db_env(name, cfg) for name, cfg in raw["environments"].items()},
        auth=auth,
        guard=GuardConfig(
            **{
                **(raw.get("guard") or {}),
                "forbidden_schemas": {s.lower() for s in (raw.get("guard") or {}).get("forbidden_schemas", [])},
            }
        ),
        cache=CacheConfig(**{k: v for k, v in cache_raw.items() if k in CacheConfig.__annotations__}),
        audit_dir=str(raw.get("audit", {}).get("dir", "./logs")),
        server=server,
        export=ExportConfig(
            enabled=bool(export_raw.get("enabled", True)),
            dir=export_dir,
            ttl_seconds=int(export_raw.get("ttl_seconds", 3600)),
            base_url=export_base,
            sign_with_gateway_token=bool(export_raw.get("sign_with_gateway_token", True)),
        ),
    )
