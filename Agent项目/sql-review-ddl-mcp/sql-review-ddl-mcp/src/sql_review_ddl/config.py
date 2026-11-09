"""配置加载：settings.yaml + 环境变量注入（密钥不落盘）。

规则本体一律不在本目录的 yaml 里，而是全部来自 standards/ 下的规范文档：

    standards/mysql.md   总纲（人读 + 条款对照表，不参与判定）
    standards/ddl.md     DDL 判定细则 → spec.py 解析 + ddlchecks.py 检查
    standards/dml.md     DML 判定细则 → spec.py 解析 + engine.py 表达式求值

级别 / 说明 / 整改 / 阈值 / 参数 / 停用 / 反问都在 md 里；本文件只负责路径、
连接信息与阈值兜底。

v1.0.1 起另外负责「调用方身份」的采信规则（settings.yaml 的 identity 段）：
网关透传的机器 IP / AgentID 取哪些请求头、哪些直连方算可信代理、是否落审计。
身份只用于日志追溯，**不参与任何审核判定**。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import Thresholds

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(RuntimeError):
    pass


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class EnvConfig:
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 5
    read_timeout: int = 10


@dataclass
class Settings:
    name: str = "sql-review-ddl-mcp"
    host: str = "0.0.0.0"
    port: int = 8010
    path: str = "/mcp"
    token_header: str = "X-SQL-REVIEW-DDL-MCP-TOKEN"
    token: str = ""
    home: Path = Path(".")
    config_dir: Path = Path("./config")
    standards_dir: Path = Path("./standards")
    log_dir: Path = Path("./logs")
    data_dir: Path = Path("./data")
    standard_version: str = "unknown"
    metadata_backend: str = "direct"
    metadata_timeout: int = 10
    envs: dict[str, EnvConfig] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    # v1.2.1：审计留存与兜底（不落 SQL 明文，不记身份字段）
    audit_enabled: bool = True
    audit_retention_days: int = 30
    audit_compress_after_days: int = 7
    audit_log_http_errors: bool = True
    # v1.0.1：调用方身份（网关透传的机器 IP + AgentID）——只落审计，不参与任何判定
    record_identity: bool = True
    agent_id_headers: list[str] = field(default_factory=lambda: [
        "X-Agent-Id", "X-AgentID", "X-Agent-ID", "Agent-Id"])
    client_ip_headers: list[str] = field(default_factory=lambda: [
        "X-Forwarded-For", "X-Real-IP", "X-Client-IP"])
    trusted_proxies: list[str] = field(default_factory=lambda: [
        "10.0.0.219", "127.0.0.1", "::1"])
    # 规则来源（规范自然语言文件 + 人工兜底覆盖 + 反问目录）
    standard_path: Path = Path("./standards/ddl.md")          # DDL 判定细则
    dml_standard_path: Path = Path("./standards/dml.md")      # DML 判定细则
    override_path: Path = Path("./config/rules.override.yaml")
    questions_path: Path = Path("./config/questions.yaml")
    standard_version_file: Path = Path("./config/STANDARD_VERSION")
    source_url: str = ""
    scope_note: str = ""
    # v1.4：规则集对象（由 reviewer 热加载后注入，便于检查函数读取文档参数）
    ddl_ruleset: object | None = None
    dml_ruleset: object | None = None
    total_standard_path: Path = Path("./standards/mysql.md")   # 总纲（人读，不参与判定）
    # v1.2.1 迭代：存放位置 / 上报目录 / 显示名 / 日志级别 / 单次请求限额 / 审计文件命名
    display_name: str = "SQL Review DDL"
    report_dir: Path = Path("./logs")
    log_level: str = "INFO"
    max_statements: int = 20
    max_sql_bytes: int = 20000
    audit_filename_prefix: str = "audit"
    audit_ext: str = ".jsonl"


def load_settings(config_path: str | None = None) -> Settings:
    import yaml  # 延迟导入：引擎自检不需要 PyYAML

    home = Path(__file__).resolve().parents[2]   # <项目>/src/sql_review_ddl → <项目>
    cfg = Path(config_path) if config_path else (home / "config" / "settings.yaml")
    if not cfg.exists():
        raise ConfigError(f"找不到配置文件 {cfg}")
    raw = _expand(yaml.safe_load(cfg.read_text(encoding="utf-8")) or {})

    s = Settings()
    srv = raw.get("server") or {}
    auth = raw.get("auth") or {}
    paths = raw.get("paths") or {}
    md = raw.get("metadata") or {}
    th = raw.get("thresholds") or {}
    audit = raw.get("audit") or {}

    s.name = srv.get("name", s.name)
    s.display_name = str(srv.get("display_name") or s.display_name)
    s.host = srv.get("host", s.host)
    s.port = int(srv.get("port", s.port))
    s.path = srv.get("path", s.path)
    s.token_header = auth.get("header", s.token_header)
    s.token = (auth.get("token") or "").strip()

    # 存放位置：paths.base 为空 → 跟随包所在目录；填绝对路径 → 整包搬到别处也能跑
    base = str(paths.get("base") or "").strip()
    if base:
        base_p = Path(base)
        s.home = base_p if base_p.is_absolute() else (home / base_p).resolve()
    else:
        s.home = home
    s.config_dir = s.home / (paths.get("config") or "config")
    s.standards_dir = s.home / (paths.get("standards") or "standards")
    s.log_dir = s.home / (paths.get("logs") or "logs")
    s.data_dir = s.home / (paths.get("data") or "data")
    s.report_dir = s.home / (paths.get("report") or (paths.get("logs") or "logs"))

    std = raw.get("standard") or {}
    # ddl 兼容旧键 path（v1.2.x 曾直接指向 mysql.md）
    s.standard_path = (home / str(std.get("ddl") or std.get("path")
                                  or "standards/ddl.md")).resolve()
    s.dml_standard_path = (home / str(std.get("dml") or "standards/dml.md")).resolve()
    s.total_standard_path = (home / str(std.get("total")
                                        or "standards/mysql.md")).resolve()
    s.override_path = (home / str(std.get("override") or "config/rules.override.yaml")).resolve()
    s.questions_path = (home / str(std.get("questions") or "config/questions.yaml")).resolve()
    s.source_url = str(std.get("source_url") or "")
    s.scope_note = str(std.get("scope_note") or "")

    s.standard_version_file = (s.home / str(std.get("version_file")
                                           or (s.config_dir.name + "/STANDARD_VERSION"))).resolve()
    ver = s.standard_version_file
    s.standard_version = ver.read_text(encoding="utf-8").strip() if ver.exists() else "unknown"

    s.metadata_backend = (md.get("backend") or "direct").lower()
    s.metadata_timeout = int(md.get("timeout", 10))     # 各环境未单独配置时的默认超时
    for name, cfg_env in (md.get("environments") or {}).items():
        s.envs[name.lower()] = EnvConfig(
            host=cfg_env.get("host", ""), port=int(cfg_env.get("port", 3306)),
            user=cfg_env.get("user", ""), password=cfg_env.get("password", ""),
            charset=cfg_env.get("charset", "utf8mb4"),
            connect_timeout=int(cfg_env.get("connect_timeout",
                                            min(5, s.metadata_timeout))),
            read_timeout=int(cfg_env.get("read_timeout", s.metadata_timeout)),
        )
    s.thresholds = Thresholds(
        big_table_rows=int(th.get("big_table_rows", 5_000_000)),
        big_table_size_gb=int(th.get("big_table_size_gb", 3)),
        modify_column_rows=int(th.get("modify_column_rows", 8_000_000)),
        varchar_instant_bytes=int(th.get("varchar_instant_bytes", 255)),
        max_columns=int(th.get("max_columns", 30)),
        max_indexes=int(th.get("max_indexes", 5)),
        max_index_cols=int(th.get("max_index_cols", 5)),
        max_unique_cols=int(th.get("max_unique_cols", 3)),
        varchar_prefix_max=int(th.get("varchar_prefix_max", 8)),
        varchar_prefix_threshold=int(th.get("varchar_prefix_threshold", 64)),
    )
    # v1.2.1 审计：保留 30 天、7 天后压缩；save_sql 已废弃（本服务永不落 SQL 明文）
    s.audit_enabled = bool(audit.get("enabled", True))
    s.audit_retention_days = int(audit.get("retention_days", 30))
    s.audit_compress_after_days = int(audit.get("compress_after_days", 7))
    s.audit_log_http_errors = bool(audit.get("log_http_errors", True))
    s.audit_filename_prefix = str(audit.get("filename_prefix") or "audit").strip() or "audit"
    ext = str(audit.get("ext") or ".jsonl").strip()
    s.audit_ext = ext if ext.startswith(".") else "." + ext

    # v1.0.1：调用方身份（网关透传的机器 IP / AgentID）——只落审计，不参与判定
    ident = raw.get("identity") or {}
    s.record_identity = bool(ident.get("record", s.record_identity))
    for key, attr in (("agent_id_headers", "agent_id_headers"),
                      ("client_ip_headers", "client_ip_headers"),
                      ("trusted_proxies", "trusted_proxies")):
        values = ident.get(key)
        if isinstance(values, list):
            cleaned = [str(v).strip() for v in values if str(v).strip()]
            if cleaned:
                setattr(s, attr, cleaned)

    limits = raw.get("limits") or {}
    s.max_statements = int(limits.get("max_statements", 20))
    s.max_sql_bytes = int(limits.get("max_sql_bytes", 20000))

    log_cfg = raw.get("log") or {}
    s.log_level = str(log_cfg.get("level") or "INFO").upper()
    if "save_sql" in audit:
        import logging as _logging
        _logging.getLogger("sql_review_ddl.config").warning(
            "audit.save_sql 已废弃并被忽略（本服务永不落 SQL 明文），请从 settings.yaml 删除该项")
    return s


def load_questions(settings: Settings) -> dict:
    """反问目录（Q-*）。缺失不致命：没有它只是少了「该问什么」的建议清单。"""
    import yaml  # 延迟导入

    p = Path(settings.questions_path)
    if not p.exists():
        return {"questions": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
