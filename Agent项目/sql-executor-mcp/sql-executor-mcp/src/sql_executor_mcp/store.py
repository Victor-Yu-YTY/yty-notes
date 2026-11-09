"""执行记录的落地：一个 execution 一个 JSON + 一行审计。

目录布局（logs/）：
    execution-<id>.json        单次执行完整记录（含回滚 SQL 全文指针）
    undo-<id>.jsonl            逐行旧值 / DDL 反向语句，写前落盘
    execution-YYYYMMDD.jsonl   按天追加的审计流水，只记摘要与 undo 文件指纹

审计流水不塞旧值：一次 UPDATE 一万行会让审计文件失控，所以主日志只留
undo 文件路径、行数与 sha256，事后凭 execution_id 直接找回。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from .undo import UndoLog, tighten

ID_RE = re.compile(r"^exe-\d{8}-\d{6}-[0-9a-f]{4}$")


class ExecutionStore:
    def __init__(self, logs_dir: Path):
        self.dir = Path(logs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- id
    def new_id(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        for _ in range(50):
            eid = f"exe-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            if not self.record_path(eid).exists():
                return eid
        raise RuntimeError("生成 execution_id 失败：连续 50 次撞号")

    @staticmethod
    def valid_id(execution_id: str) -> bool:
        # 防路径穿越：只接受固定格式
        return bool(ID_RE.match(execution_id or ""))

    # -- 路径
    def record_path(self, execution_id: str) -> Path:
        return self.dir / f"execution-{execution_id}.json"

    def undo_path(self, execution_id: str) -> Path:
        return self.dir / f"undo-{execution_id}.jsonl"

    def open_undo(self, execution_id: str) -> UndoLog:
        return UndoLog(self.undo_path(execution_id))

    # -- 读写
    def save(self, record: dict) -> Path:
        path = self.record_path(record["execution_id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        tighten(tmp)
        os.replace(tmp, path)      # 原子替换，避免读到半截文件
        tighten(path)
        return path

    def load(self, execution_id: str) -> dict | None:
        if not self.valid_id(execution_id):
            return None
        path = self.record_path(execution_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def append_audit(self, summary: dict, now: datetime | None = None) -> Path:
        now = now or datetime.now()
        path = self.dir / f"execution-{now:%Y%m%d}.jsonl"
        line = json.dumps(summary, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            tighten(path)
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return path
