"""配置加载：settings.yaml + targets.yaml + 标准文件路径。

密钥一律走环境变量（`${VAR}` 展开），配置文件里不写明文。
标准（规则）文件路径默认 `standards/executor.md`，相对「安装根目录」。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def project_root() -> Path:
    """src/sql_executor_mcp/config.py -> 仓库根（安装根目录）。"""
    return Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    name: str = "sql-executor-mcp"
    display_name: str = "SQL-Executor"
    host: str = "0.0.0.0"
    port: int = 8040
    path: str = "/mcp"
    token_header: str = "X-SQL-EXECUTOR-MCP-TOKEN"
    token: str = ""
    base: Path = field(default_factory=project_root)
    config_dir: Path = field(default_factory=lambda: project_root() / "config")
    standards_dir: Path = field(default_factory=lambda: project_root() / "standards")
    logs_dir: Path = field(default_factory=lambda: project_root() / "logs")
    standard_file: Path = field(
        default_factory=lambda: project_root() / "standards" / "executor.md")
    targets_file: Path = field(
        default_factory=lambda: project_root() / "config" / "targets.yaml")
    backend: str = "mysql"          # mysql | stub
    log_level: str = "INFO"

    @property
    def policy_file(self) -> Path:
        """兼容旧调用方的别名：现在指向标准 md 文件。"""
        return self.standard_file

    @classmethod
    def load(cls, config_dir: str | os.PathLike | None = None) -> "Settings":
        s = cls()
        if config_dir:
            s.config_dir = Path(config_dir).resolve()
            s.base = s.config_dir.parent
        else:
            env_dir = os.environ.get("EXECUTOR_CONFIG_DIR")
            if env_dir:
                s.config_dir = Path(env_dir).resolve()
                s.base = s.config_dir.parent

        path = s.config_dir / "settings.yaml"
        if path.exists():
            raw = _expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
            srv = raw.get("server") or {}
            s.name = srv.get("name", s.name)
            s.display_name = srv.get("display_name", s.display_name)
            s.host = srv.get("host", s.host)
            s.port = int(srv.get("port", s.port))
            s.path = srv.get("path", s.path)

            auth = raw.get("auth") or {}
            s.token_header = auth.get("header", s.token_header)
            s.token = auth.get("token", s.token) or os.environ.get("EXECUTOR_MCP_TOKEN", "")

            paths = raw.get("paths") or {}
            base = paths.get("base") or ""
            if base:
                s.base = Path(base)
                s.config_dir = s.base / (paths.get("config") or "config")
            s.logs_dir = s.base / (paths.get("logs") or "logs")
            s.standards_dir = s.base / (paths.get("standards") or "standards")

            # 标准（规则）文件与目标环境
            std = raw.get("standard") or raw.get("policy") or {}
            s.standard_file = s.base / (std.get("file") or "standards/executor.md")
            s.targets_file = s.base / (std.get("targets") or "config/targets.yaml")

            s.backend = (raw.get("backend") or s.backend).lower()
            s.log_level = (raw.get("log") or {}).get("level", s.log_level)

        s.backend = os.environ.get("EXECUTOR_BACKEND", s.backend).lower()
        return s

    def load_targets(self) -> dict:
        if not self.targets_file.exists():
            return {"environments": {}}
        return _expand(yaml.safe_load(self.targets_file.read_text(encoding="utf-8")) or {})

    def env_cfg(self, env: str) -> dict:
        return (self.load_targets().get("environments") or {}).get(env) or {}
