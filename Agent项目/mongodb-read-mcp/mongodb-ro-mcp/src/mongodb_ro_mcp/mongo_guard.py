"""只读防线与输出控制（MongoDB 版 sql_guard）。

三层只读里的「语义层」与「资源层」都在这里：

* **语义层**：递归遍历 JSON 全部 key，命中黑名单直接拒绝。
  - 聚合管道：`$out` / `$merge`（落盘写库）、`$where` / `$function` /
    `$accumulator`（服务端 JS 执行）；
  - 查询条件：`$where` / `$function` / `$accumulator`。
  递归而不是字符串匹配，是因为 `$out` 可能藏在 `$facet` / `$lookup.pipeline`
  的任意深度；按 key 判定才不会漏。
* **资源层**：`maxTimeMS`、单次条数上限、单字段字符上限、响应字节上限、
  聚合默认 `allowDiskUse=false`。
* **标识符与库白名单**：库名/集合名做字符校验；`admin` / `config` / `local`
  默认禁止通过数据查询工具访问（巡检类工具走内部代码路径，不经此处）。

注意：这些是有意的"能力边界"，不是安全边界的全部。
**数据库侧的只读账号（read / readAnyDatabase）才是最终边界** ——
仅靠 MCP 层拦不住绕过服务端的路径。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

# 库名 / 集合名允许的字符：字母数字下划线，以及 - . $（Mongo 允许，但不允许空格与控制字符）
IDENT_RE = re.compile(r"^[A-Za-z0-9_\-$.]{1,120}$")


class GuardError(ValueError):
    """被只读防线拒绝。code 直接对应 audit.REJECT_CODES 里的原因码。"""

    def __init__(self, code: str, message: str, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}


# --------------------------------------------------------------------------
# 算子黑名单
# --------------------------------------------------------------------------
def _walk_keys(obj: Any, found: set[str], depth: int = 0) -> None:
    if depth > 40:                       # 防御性深度上限（正常管道远达不到）
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.startswith("$"):
                found.add(key)
            _walk_keys(value, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_keys(item, found, depth + 1)


def _hits(payload: Any, forbidden: Iterable[str]) -> list[str]:
    keys: set[str] = set()
    _walk_keys(payload, keys)
    return sorted(keys & set(forbidden))


def guard_pipeline(pipeline: Any, forbidden: Iterable[str]) -> None:
    """聚合管道递归检查；命中所禁算子直接抛 GuardError。"""
    hits = _hits(pipeline, forbidden)
    if hits:
        raise GuardError(
            "GUARD_FORBIDDEN_OPERATOR",
            "管道包含被禁止的算子：%s（只读服务不允许写库或执行服务端 JS）" % ", ".join(hits),
            {"operator": hits, "stage": "aggregate"},
        )


def guard_filter(flt: Any, forbidden: Iterable[str] | None = None) -> None:
    """查询条件检查：只拦服务端 JS 执行类算子（写算子在这里本来也不成立）。"""
    banned = set(forbidden or {"$where", "$function", "$accumulator"})
    hits = _hits(flt, banned)
    if hits:
        raise GuardError(
            "GUARD_FORBIDDEN_OPERATOR",
            "查询条件包含被禁止的算子：%s" % ", ".join(hits),
            {"operator": hits, "stage": "filter"},
        )


# --------------------------------------------------------------------------
# 标识符 / 库白名单
# --------------------------------------------------------------------------
def check_ident(kind: str, value: str) -> None:
    if not isinstance(value, str) or not IDENT_RE.match(value or ""):
        raise GuardError(
            "ARG_IDENT_INVALID",
            "%s 不合法: %r（只允许字母、数字、下划线、点、短横线和 $，长度 1~120）"
            % (kind, value),
            {"field": kind, "value": str(value)[:64]},
        )


def check_db_allowed(database: str, forbidden_dbs: Iterable[str] | None) -> None:
    banned = {str(x).lower() for x in (forbidden_dbs or ())}
    if database and database.lower() in banned:
        raise GuardError(
            "GUARD_FORBIDDEN_DB",
            "库 %r 在禁止清单内（%s）；数据查询只允许业务库，巡检请用 current_operations / "
            "server_status / replica_status 三个专用工具" % (database, ", ".join(sorted(banned))),
            {"database": database},
        )


def load_json(raw: str, *, field: str, default: Any = None) -> Any:
    """把工具参数里的 JSON 字符串解析成对象；空串返回 default。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuardError(
            "ARG_JSON_INVALID",
            "%s 不是合法 JSON：%s（示例见工具说明）" % (field, exc),
            {"field": field},
        ) from None


# --------------------------------------------------------------------------
# 输出控制
# --------------------------------------------------------------------------
def shrink(obj: Any, max_chars: int) -> Any:
    """递归截断超长字符串字段（保留前缀与原始长度提示）。"""
    if isinstance(obj, dict):
        return {k: shrink(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shrink(v, max_chars) for v in obj]
    if isinstance(obj, str) and len(obj) > max_chars:
        return obj[:max_chars] + "…[截断，共 %d 字符]" % len(obj)
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


def summarize_docs(docs: list, limit: int) -> tuple[list, str, bool]:
    """limit+1 探测：多取一条只为判断"还有更多"，不额外跑 count。"""
    if len(docs) > limit:
        return docs[:limit], "⚠️ 已达 limit=%d，可能还有更多结果（可加 filter 或调大 limit）" % limit, True
    return docs, "共 %d 条" % len(docs), False
