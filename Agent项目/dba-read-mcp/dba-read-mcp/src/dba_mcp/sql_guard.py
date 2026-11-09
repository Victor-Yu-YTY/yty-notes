"""只读 SQL 校验：解析优先，拒绝一切非读操作，并自动补 LIMIT。

用 sqlglot 做 AST 解析而不是正则，避免注释、换行、子查询绕过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


def _types(*names: str) -> tuple[type, ...]:
    """安全取 sqlglot 表达式类型，缺失的静默跳过（跨版本兼容）。"""
    found = []
    for name in names:
        cls = getattr(exp, name, None)
        if cls is not None:
            found.append(cls)
    return tuple(found)


# 允许的顶层语句类型
# 实测 sqlglot 30.x（mysql 方言）的解析结果：
#   SELECT ...        -> Select
#   SHOW CREATE TABLE -> Show
#   DESCRIBE / DESC   -> Describe
#   EXPLAIN SELECT .. -> Describe
ROOT_ALLOWED = _types("Select", "Show", "Describe", "Explain")
# AST 里任何位置出现这些节点都拒绝
FORBIDDEN_NODES = _types(
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Replace", "Merge",
    "Call", "Grant", "Revoke", "Set", "TruncateTable", "RenameTable", "Kill",
    "LoadData", "Command",
)
# SELECT ... INTO OUTFILE / DUMPFILE（sqlglot 对这类语法会直接解析失败，
# 因此这里先用正则拦一道，给出明确原因，而不是笼统的“解析失败”）
INTO_OUTFILE_RE = re.compile(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", re.IGNORECASE)
# FOR UPDATE / FOR SHARE
LOCK_NODES = _types("Lock")


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    reason: str = ""


def _strip_sql(sql: str) -> str:
    s = sql.strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def validate_readonly(sql: str, *, forbidden_schemas: set[str]) -> GuardResult:
    """校验一条 SQL 是否只读；通过则返回规范化后的 SQL。"""
    s = _strip_sql(sql or "")
    if not s:
        return GuardResult(ok=False, reason="SQL 为空")

    # 多语句：sqlglot 会把 `;` 分隔的语句解析成多棵 AST
    if ";" in s:
        return GuardResult(ok=False, reason="不允许一次执行多条语句")

    if INTO_OUTFILE_RE.search(s):
        return GuardResult(ok=False, reason="禁止 SELECT ... INTO OUTFILE/DUMPFILE（会写文件）")

    try:
        statements = [st for st in sqlglot.parse(s, read="mysql") if st is not None]
    except Exception as exc:
        return GuardResult(ok=False, reason=f"SQL 解析失败: {exc}")

    if len(statements) != 1:
        return GuardResult(ok=False, reason="只允许单条语句")
    stmt = statements[0]

    for node in stmt.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return GuardResult(ok=False, reason=f"禁止的语句类型: {type(node).__name__}")
        if LOCK_NODES and isinstance(node, LOCK_NODES):
            return GuardResult(ok=False, reason="禁止 FOR UPDATE / FOR SHARE")

    for table in stmt.find_all(exp.Table):
        schema = (table.db or "").strip()
        if schema and schema.lower() in forbidden_schemas:
            return GuardResult(ok=False, reason=f"禁止访问 schema: {schema}")

    if not isinstance(stmt, ROOT_ALLOWED):
        return GuardResult(ok=False, reason=f"不允许的语句类型: {type(stmt).__name__}")

    # Command 类型（如 SHOW CREATE TABLE）再用前缀白名单兜一层
    if type(stmt).__name__ == "Command" and not s.upper().startswith(STATEMENT_PREFIXES):
        return GuardResult(ok=False, reason="语句前缀不在白名单内")

    return GuardResult(ok=True, sql=s)


def ensure_limit(sql: str, limit: int) -> str:
    """顶层 SELECT 未带 LIMIT 时自动补上，防止拖垮线下库。"""
    try:
        statements = [st for st in sqlglot.parse(sql, read="mysql") if st is not None]
    except Exception:
        return sql
    if len(statements) != 1:
        return sql
    stmt = statements[0]
    if isinstance(stmt, exp.Select) and stmt.args.get("limit") is None:
        stmt.set("limit", exp.Limit(expression=exp.Literal.number(int(limit))))
        return stmt.sql(dialect="mysql")
    return sql
