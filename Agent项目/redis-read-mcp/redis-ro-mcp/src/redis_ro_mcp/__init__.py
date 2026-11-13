"""DBA Redis 只读 MCP（单服务多环境｜对外 stage / 内部 mirror）。

技术标识：redis-ro-mcp
"""

from __future__ import annotations

from pathlib import Path

_FALLBACK = "1.0.0"


def _read_version() -> str:
    """版本以 VERSION 文件为准（编写规约：VERSION 与 __version__ 双写，代码优先读 VERSION）。"""
    try:
        # __file__ = <包根>/src/redis_ro_mcp/__init__.py → parents[2] = <包根>
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text(
            encoding="utf-8"
        ).strip() or _FALLBACK
    except OSError:
        return _FALLBACK


__version__ = _read_version()
