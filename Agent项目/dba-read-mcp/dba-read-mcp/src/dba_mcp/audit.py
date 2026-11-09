"""审计日志：JSON Lines，按天切分。

每条记录一行，字段固定，方便后续直接灌进日志系统或做统计。

v0.3.0：记录里增加 identity 段（agent_id / agent_name / request_id）。
这些值来自网关注入的请求头，**只作追溯锚点**，不参与授权判定；
服务端不会把任何凭证原文写进来。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()


def write_audit(audit_dir: str, event: dict[str, Any]) -> None:
    """写一条审计记录；写失败只记日志，不影响主流程。"""
    try:
        os.makedirs(audit_dir, exist_ok=True)
        filename = f"audit-{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        path = os.path.join(audit_dir, filename)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # 审计不能阻断业务
        try:
            import logging

            logging.getLogger("dba_mcp.audit").warning("审计写入失败: %s", exc)
        except Exception:
            pass
