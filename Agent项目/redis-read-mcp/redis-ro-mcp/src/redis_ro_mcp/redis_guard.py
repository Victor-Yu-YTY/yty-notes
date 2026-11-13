"""只读防线与输出控制（Redis 版 sql_guard / mongo_guard）。

Redis 侧的只读边界与 MongoDB/MySQL 有一个本质差别：**实例无密码且 Redis 5.0.6
没有 ACL，数据库层拿不出任何写拒绝能力**。所以本服务的三层防线是：

* **接口层（最硬）**：工具面只登记读操作，**没有** `execute` / `eval` /
  `execute_command` 之类通用命令入口。想写必须改代码，而改代码要过评审。
* **语义层**：`allowed_commands` 白名单（本服务可能发出的命令全集）与
  `forbidden_commands` 写命令黑名单。黑名单同时是**静态自检的断言源**——
  `scripts/checks/verify.sh` 与 `scripts/tools/selftest.py` 会在源码里 grep
  这些写方法，命中即判失败（`server.py` 里一个都不该出现）。
* **资源层**：SCAN `COUNT`、成员数、采样数、Top N、单字段字符数、响应字节数、
  socket 超时——全部有上限，避免一次调用把实例或网关拖住。

另外两条与 Predixy 代理有关的硬约束（2026-11-10 三环境实测，见 README §2）：

* `proxy_rejected_commands` 里的命令（`DBSIZE`/`OBJECT`/`MEMORY`/`SLOWLOG`/`CLIENT`/
  `CLUSTER`）**一律不调用**：调了也是代理拒绝，不如在工具面就不提供；
* 键枚举一律 `SCAN`，**绝不 `KEYS`**（`KEYS` 会阻塞实例，且不在白名单）。

注意：这些是有意的"能力边界"，不是安全边界的全部。实例无密码这一事实意味着
**网络位置与网关准入令牌才是最终边界**——详见 README §6 已知限制。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

# key / pattern 允许的字符：可打印 ASCII（Redis key 是二进制安全的，但本服务不接受
# 控制字符——避免日志注入与终端错乱）。长度由配置的 max_key_len 控制。
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# zrangebyscore 的分数字面量：数字、-inf/+inf、以及带 ( 的开区间写法
_SCORE_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class GuardError(ValueError):
    """被只读防线拒绝。code 直接对应 audit.REJECT_CODES 里的原因码。"""

    def __init__(self, code: str, message: str, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}


# --------------------------------------------------------------------------
# 入参校验
# --------------------------------------------------------------------------
def check_key(value: Any, max_len: int) -> str:
    """key 合法性：非空、无控制字符、不超长。返回清洗后的 key。"""
    text = str(value or "")
    if not text:
        raise GuardError("ARG_KEY_INVALID", "key 不能为空")
    if len(text) > max_len:
        raise GuardError(
            "ARG_KEY_INVALID",
            "key 超长（%d > %d 字符），请确认没有把整个 JSON/HTML 当 key 传进来"
            % (len(text), max_len),
            {"length": len(text), "max": max_len},
        )
    if _CTRL_RE.search(text):
        raise GuardError(
            "ARG_KEY_INVALID",
            "key 含控制字符（不可见字符/换行），请检查调用方编码",
            {"preview": text[:32]},
        )
    return text


def check_pattern(value: Any, max_len: int) -> str:
    """SCAN pattern 合法性：允许空（等价 `*`）、无控制字符、不超长。"""
    text = str(value if value is not None else "*")
    if text == "":
        return "*"
    if len(text) > max_len:
        raise GuardError(
            "ARG_PATTERN_INVALID",
            "pattern 超长（%d > %d 字符）；SCAN 是前缀匹配，不要塞整行文本"
            % (len(text), max_len),
            {"length": len(text), "max": max_len},
        )
    if _CTRL_RE.search(text):
        raise GuardError(
            "ARG_PATTERN_INVALID",
            "pattern 含控制字符（不可见字符/换行）",
            {"preview": text[:32]},
        )
    return text


def clamp_int(name: str, value: Any, low: int, high: int) -> int:
    """整数参数夹到 [low, high]；非整数一律拒绝（不静默兜底）。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise GuardError(
            "ARG_LIMIT_INVALID", "%s 必须是整数，收到 %r" % (name, value),
            {"field": name},
        ) from None
    return max(low, min(num, high))


def check_range(name: str, start: Any, stop: Any, max_members: int) -> tuple[int, int]:
    """读区间：start/stop 为整数（支持负数下标），并夹住窗口大小。"""
    try:
        s = int(start)
        e = int(stop)
    except (TypeError, ValueError):
        raise GuardError(
            "ARG_RANGE_INVALID",
            "%s 的 start/stop 必须是整数（支持 -1 表示末尾），收到 %r / %r"
            % (name, start, stop),
        ) from None
    if e < s:
        raise GuardError(
            "ARG_RANGE_INVALID", "%s 的 stop(%d) 小于 start(%d)" % (name, e, s),
            {"start": s, "stop": e},
        )
    return s, min(e, s + max_members - 1)


def check_score(name: str, value: Any) -> str:
    """zset 分数字面量：数字、-inf/+inf、或 `(数字` 开区间。"""
    text = str(value if value is not None else "").strip()
    low = text.lower()
    if low in ("-inf", "+inf", "inf", "-infinity", "+infinity"):
        return text
    body = text[1:] if text.startswith("(") else text
    if not _SCORE_RE.match(body):
        raise GuardError(
            "ARG_SCORE_INVALID",
            "%s 不合法: %r（只接受数字、-inf、+inf 或 (数字 表示开区间）" % (name, value),
            {"field": name},
        )
    return text


# --------------------------------------------------------------------------
# 命令白名单断言（供工具内部与自检共用）
# --------------------------------------------------------------------------
def guard_command(name: str, allowed: Iterable[str], forbidden: Iterable[str]) -> None:
    """断言本次要发出的命令在只读白名单内、且不在写命令黑名单内。"""
    cmd = str(name or "").strip().lower()
    banned = {str(x).lower() for x in (forbidden or ())}
    if cmd in banned:
        raise GuardError(
            "GUARD_COMMAND_DENIED",
            "命令 %r 属于写命令，本服务只读，一律拒绝" % cmd,
            {"command": cmd},
        )
    allow = {str(x).lower() for x in (allowed or ())}
    if allow and cmd not in allow:
        raise GuardError(
            "GUARD_COMMAND_DENIED",
            "命令 %r 不在只读白名单内（白名单以 README §2 的 Predixy 实测为准）" % cmd,
            {"command": cmd},
        )


# --------------------------------------------------------------------------
# 输出控制
# --------------------------------------------------------------------------
def shrink(obj: Any, max_chars: int) -> Any:
    """递归截断超长字符串字段（保留前缀与原始长度提示）。"""
    if isinstance(obj, dict):
        return {k: shrink(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [shrink(v, max_chars) for v in obj]
    if isinstance(obj, str) and len(obj) > max_chars:
        return obj[:max_chars] + "…[截断，共 %d 字符]" % len(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    return obj


def dumps(payload: Any, max_chars: int) -> str:
    return json.dumps(
        shrink(payload, max_chars), ensure_ascii=False, indent=1, default=str
    )


def clamp_bytes(text: str, max_bytes: int) -> str:
    """按 **字节** 兜底截断（CJK 按字符截会算错，这里先编码再切，避免切出半个字）。"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", "ignore") + "\n…[输出超限已按 %d 字节截断]" % max_bytes


def cap_items(items: Sequence[Any], limit: int, unit: str = "个") -> tuple[list[Any], str, bool]:
    """成员/元素列表兜底：超过 limit 就截断并给出真实总数提示。"""
    seq = list(items)
    if len(seq) > limit:
        return seq[:limit], "⚠️ 共 %d %s，仅返回前 %d（可用参数缩小范围）" % (
            len(seq), unit, limit), True
    return seq, "共 %d %s" % (len(seq), unit), False


def mask_secret(text: str) -> str:
    """日志里绝不打出口令原文（本服务当前无口令，留着防未来翻车）。"""
    return "***" if text else ""
