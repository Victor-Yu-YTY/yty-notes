"""undo 日志：写前落盘（write-ahead）。

已拍板不建 undo 表，回滚所需数据全部写进日志文件。

关键约束（设计 §8.3）：**先写日志并 fsync，再执行业务语句。**
业务事务 ROLLBACK 时日志文件不会跟着消失 —— 这正是把 undo 从「数据库表」
换成「文件」要解决的核心问题，顺序反了这个方案就不成立。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any


def tighten(path) -> None:
    """日志里有 SQL 明文和逐行旧值，权限收到 600。

    Windows 上 chmod 语义有限，失败不影响功能，静默跳过。
    """
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass


def jsonable(value: Any) -> Any:
    """把游标取回的值转成可 JSON 序列化的形式，保留原始语义。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


class UndoLog:
    """一行一条记录的 JSONL，每条写完立即 flush + fsync。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        tighten(self.path)
        self.lines = 0
        self.bytes = 0
        self._sha = hashlib.sha256()

    def write(self, record: dict) -> None:
        line = json.dumps(jsonable(record), ensure_ascii=False, sort_keys=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())     # 写前落盘：不 fsync 等于没写
        self.lines += 1
        self.bytes += len(line.encode("utf-8")) + 1
        self._sha.update(line.encode("utf-8") + b"\n")

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    def digest(self) -> dict:
        return {
            "file": self.path.name,
            "path": str(self.path),
            "lines": self.lines,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "UndoLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_records(path: Path) -> list[dict]:
    """读回 undo 日志，供 get_execution 展示与人工核对。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_unparsable": line[:200]})
    return out
