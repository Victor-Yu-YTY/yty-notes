"""查询结果导出：把每次工具调用的 JSON 结果落成 .json 文件，供研发下载。

设计：
- 每个工具调用（成功 / 被拒绝 / 出错）都会把最终 JSON 结果写到 export.dir/<uuid>.json；
- 响应 JSON 里多一个 "export" 字段，含下载链接（url）、文件名（file）和有效期（expires_in）；
- 下载地址形如 GET /export/<uuid>.json，由 MCP 服务自身提供（不走 MCP 协议、不校验 token）；
- 文件按 TTL 自动清理（默认 3600 秒），uuid 随机不可猜。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


class ExportStore:
    """本地导出目录：<dir>/<uuid>.json，按 TTL 自动清理。"""

    def __init__(self, directory: str, ttl_seconds: int = 3600) -> None:
        self._dir = directory
        self._ttl = max(60, int(ttl_seconds))

    def save(self, payload: dict[str, Any]) -> dict[str, str]:
        """写入一个 JSON 文件，返回 {"uuid": ..., "file": ...}。"""
        os.makedirs(self._dir, exist_ok=True)
        self._prune()
        key = uuid.uuid4().hex
        path = os.path.join(self._dir, f"{key}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return {"uuid": key, "file": f"db-read-mcp-{key[:8]}.json"}

    def read(self, key: str) -> bytes | None:
        """读取导出文件；不存在或已过期返回 None。"""
        if not _KEY_RE.match(key):
            return None
        path = os.path.join(self._dir, f"{key}.json")
        try:
            if not os.path.isfile(path):
                return None
            if time.time() - os.path.getmtime(path) > self._ttl:
                os.remove(path)
                return None
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def _prune(self) -> None:
        try:
            now = time.time()
            for name in os.listdir(self._dir):
                if not name.endswith(".json"):
                    continue
                key = name[: -len(".json")]
                if not _KEY_RE.match(key):
                    continue
                path = os.path.join(self._dir, name)
                try:
                    if now - os.path.getmtime(path) > self._ttl:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass
