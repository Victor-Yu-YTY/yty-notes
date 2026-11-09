"""回滚 SQL 生成：只生成，不执行。

本版拍板口径：回滚本身是 DROP / DELETE 类破坏性写，执行账号没有这些权限，
也不该有。这里产出的语句一律由 DBA 人工确认后执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from . import parser as P
from .snapshot import TableSnapshot


def q(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def qname(database: str, table: str) -> str:
    return f"{q(database)}.{q(table)}" if database else q(table)


_ESC = {"\\": "\\\\", "'": "\\'", "\0": "\\0", "\n": "\\n", "\r": "\\r", "\x1a": "\\Z"}


def sql_literal(value) -> str:
    """把 Python 值渲染成 SQL 字面量。只用于回滚语句，全部走转义，不拼裸字符串。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return "'" + value.isoformat(sep=" ") + "'"
    if isinstance(value, (date, time)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    text = str(value)
    return "'" + "".join(_ESC.get(ch, ch) for ch in text) + "'"


@dataclass
class RollbackPlan:
    available: bool = False
    sql: list[str] = field(default_factory=list)
    undone_by: str = "none"     # ddl_text | undo_log | none
    note: str = ""
    truncated: int = 0          # 返回体里省略掉的条数

    def to_dict(self, limit: int | None = None) -> dict:
        out = {
            "available": self.available,
            "undone_by": self.undone_by,
            "note": self.note,
            "count": len(self.sql),
            "sql": self.sql,
        }
        if limit is not None and len(self.sql) > limit:
            out["sql"] = self.sql[:limit]
            out["truncated"] = len(self.sql) - limit
            out["note_full_file"] = True
        elif self.truncated:
            out["truncated"] = self.truncated
        return out


def _unavailable(note: str, undone_by: str = "none") -> RollbackPlan:
    return RollbackPlan(available=False, sql=[], undone_by=undone_by, note=note)


# ---------------------------------------------------------------- DDL


def ddl_reverse(st: P.Statement, snap: TableSnapshot | None, database: str) -> RollbackPlan:
    table = st.table or ""
    full = qname(database, table)

    if st.kind == P.K_CREATE_TABLE:
        return RollbackPlan(
            available=True,
            sql=[f"DROP TABLE {full};"],
            undone_by="ddl_text",
            note="回滚即删表：若建表后已写入数据，会连数据一起删除；执行前确认该表无业务依赖",
        )

    if st.kind == P.K_CREATE_INDEX:
        if not st.index_name:
            return _unavailable("CREATE INDEX 未显式命名索引，无法生成 DROP INDEX")
        return RollbackPlan(
            available=True,
            sql=[f"DROP INDEX {q(st.index_name)} ON {full};"],
            undone_by="ddl_text",
            note="仅回滚索引本身，不影响表数据",
        )

    # ALTER TABLE：按 op 逆序生成
    if snap is None:
        return _unavailable("取不到执行前的表结构快照（SHOW CREATE TABLE 失败），无法推导反向语句")

    stmts: list[str] = []
    notes: list[str] = []
    ok = True
    for op in reversed(st.ops):
        line, note, good = _alter_op_reverse(op, snap, full)
        if good and line:
            stmts.append(line)
        else:
            ok = False
        if note:
            notes.append(note)

    if not stmts and not ok:
        return _unavailable("；".join(notes) or "无可用的反向语句", undone_by="ddl_text")

    default = "按 op 逆序生成；若变更后已写入数据，无法保证回到变更前的数据状态"
    return RollbackPlan(
        available=ok,
        sql=stmts,
        undone_by="ddl_text",
        note="；".join(notes) if notes else default,
    )


def _alter_op_reverse(op: P.AlterOp, snap: TableSnapshot,
                      full: str) -> tuple[str | None, str | None, bool]:
    kind = op.kind

    if kind == P.OP_ADD_COLUMN:
        if not op.column:
            return None, "ADD COLUMN 未识别出列名（多列合并写法），无法反向", False
        return f"ALTER TABLE {full} DROP COLUMN {q(op.column)};", \
            f"DROP COLUMN {op.column} 会丢弃该列已有数据", True

    if kind == P.OP_MODIFY_COLUMN:
        old = snap.column(op.column)
        if not old:
            return None, f"快照里没有列 {op.column} 的原定义，无法反向 MODIFY", False
        return f"ALTER TABLE {full} MODIFY COLUMN {old};", None, True

    if kind == P.OP_CHANGE_COLUMN:
        old = snap.column(op.column)
        target = op.new_name or op.column
        if not old:
            return None, f"快照里没有列 {op.column} 的原定义，无法反向 CHANGE", False
        return f"ALTER TABLE {full} CHANGE COLUMN {q(target)} {old};", \
            "改名回退按原定义重建，列内数据保留", True

    if kind == P.OP_DROP_COLUMN:
        old = snap.column(op.column)
        if not old:
            return None, f"快照里没有列 {op.column} 的原定义，无法恢复", False
        return f"ALTER TABLE {full} ADD COLUMN {old};", \
            f"列 {op.column} 的结构可重建，但被 DROP 的数据只能从备份恢复", True

    if kind == P.OP_ADD_INDEX:
        name = op.index_name or (op.columns[0] if op.columns else None)
        if not name:
            return None, "ADD INDEX 未识别出索引名，无法反向 DROP", False
        note = "索引名由 MySQL 按首列自动生成，回滚时按同名删除" if not op.index_name else None
        return f"ALTER TABLE {full} DROP INDEX {q(name)};", note, True

    if kind == P.OP_ADD_PRIMARY:
        return f"ALTER TABLE {full} DROP PRIMARY KEY;", \
            "主键变更影响聚簇索引，回滚耗时接近重建表", True

    if kind == P.OP_DROP_INDEX:
        idx = snap.index(op.index_name)
        if idx is None:
            return None, f"快照里没有索引 {op.index_name} 的定义，无法恢复", False
        prefix = "ADD PRIMARY KEY" if idx.primary else f"ADD {idx.raw}"
        return f"ALTER TABLE {full} {prefix};", None, True

    if kind == P.OP_RENAME_TABLE:
        return None, "表名变更无法自动反向（新名需人工确认），建议不做改名操作", False

    if kind == P.OP_TABLE_OPTION:
        return None, f"表选项变更（{op.raw}）无法自动撤销，需按原值手工改回", False

    return None, f"无法识别的 ALTER 操作（{op.raw}），无法生成回滚", False


# ---------------------------------------------------------------- INSERT


def values_tuples(sql: str) -> list[list[str]]:
    """从 INSERT ... VALUES 里取出每一行的字面量原文，按列顺序。取不到返回 []。"""
    toks = P.tokenize(sql)
    ki = -1
    for i, t in enumerate(toks):
        if t.kind == "word" and t.up in ("VALUES", "VALUE"):
            ki = i
            break
    if ki < 0:
        return []
    rest = sql[toks[ki].pos + len(toks[ki].value):]
    # 截断 ON DUPLICATE KEY UPDATE 尾巴
    m = re.search(r"\bON\s+DUPLICATE\s+KEY\b", rest, re.I)
    if m:
        rest = rest[:m.start()]
    rows: list[list[str]] = []
    for part in P.split_top(rest):
        part = part.strip()
        if not part.startswith("("):
            continue
        end = P.match_paren(part, 0)
        inner = part[1:end] if end > 0 else part[1:]
        rows.append([v.strip() for v in P.split_top(inner)])
    return rows


def explicit_pk_values(st: P.Statement, pk_columns: list[str]) -> tuple[list[dict] | None, str]:
    """INSERT 显式给了主键时，把主键字面量原文取出来。原文直接拼进 DELETE，不做二次转义。

    只接受字面量。`INSERT ... VALUES (UUID(), 'x')` 这种主键是函数调用的写法，
    原文拼进 DELETE 会得到 `WHERE id IN (UUID())` —— available=true 却一行都删不掉，
    属于「看起来能回滚、实际回滚无效」，比直接说不可用危险得多，这里一律拒收。

    返回 (主键值列表, 拒收原因)。拒收原因非空时调用方要把它记进 undo，供事后解释。
    """
    if not pk_columns:
        return None, "表无主键列"
    if not st.insert_columns:
        return None, "INSERT 未列出列名，无法定位主键位置"
    idx = {c.upper(): i for i, c in enumerate(st.insert_columns)}
    if not all(p.upper() in idx for p in pk_columns):
        return None, "INSERT 的列清单里没有主键列，改由自增区间定位"
    rows = values_tuples(st.raw)
    if not rows:
        return None, "取不到 VALUES 字面量（可能不是 INSERT ... VALUES 形态）"
    out: list[dict] = []
    for row in rows:
        rec: dict = {}
        for p in pk_columns:
            i = idx[p.upper()]
            if i >= len(row):
                return None, "VALUES 的列数少于列清单，无法按位置取主键"
            if not _is_literal(row[i]):
                return None, f"主键值 {row[i]} 不是字面量（函数/表达式/占位符），无法生成精确 DELETE"
            rec[p] = row[i]
        out.append(rec)
    return out, ""


_LITERAL_RE = re.compile(
    r"""^(?:NULL|TRUE|FALSE"""
    r"""|[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"""
    r"""|0x[0-9A-Fa-f]+|X'[0-9A-Fa-f]*'"""
    r"""|'(?:[^'\\]|\\.|'')*'"""
    r""")$""",
    re.I,
)


def _is_literal(text: str) -> bool:
    return bool(_LITERAL_RE.match((text or "").strip()))


def _mine(undo: list[dict], stmt_index: int | None, *types: str) -> list[dict]:
    """按语句号挑记录。批次里同类型语句可以有多条，不按语句号过滤会全部串到第一条上。"""
    out = []
    for r in undo:
        if r.get("type") not in types:
            continue
        if stmt_index is not None and r.get("stmt_index") != stmt_index:
            continue
        out.append(r)
    return out


def insert_reverse(undo: list[dict], database: str, table: str,
                   stmt_index: int | None = None) -> RollbackPlan:
    """insert 记录给主键/显式值；自增区间只能执行后补记，所以两处都要看。"""
    recs = _mine(undo, stmt_index, "insert")
    rec = recs[0] if recs else None
    if rec is None:
        return _unavailable("未采集到 INSERT 的 undo 记录")
    afters = _mine(undo, stmt_index, "insert_after")
    after = afters[0] if afters else None

    full = qname(database, table)
    pk_cols: list[str] = rec.get("pk_columns") or []
    explicit = rec.get("explicit_pk_values")

    if explicit:
        cols = ", ".join(q(c) for c in pk_cols)
        if len(pk_cols) == 1:
            vals = ", ".join(v[pk_cols[0]] for v in explicit)
            return RollbackPlan(
                available=True,
                sql=[f"DELETE FROM {full} WHERE {q(pk_cols[0])} IN ({vals});"],
                undone_by="undo_log",
                note="主键值取自原 INSERT 语句的字面量，精确匹配",
            )
        tuples = ", ".join("(" + ", ".join(v[c] for c in pk_cols) + ")" for v in explicit)
        return RollbackPlan(
            available=True,
            sql=[f"DELETE FROM {full} WHERE ({cols}) IN ({tuples});"],
            undone_by="undo_log",
            note="复合主键按元组匹配，精确删除本次插入的行",
        )

    rng = rec.get("pk_range") or (after or {}).get("pk_range")
    if rng and rng.get("start") is not None and rng.get("end") is not None:
        col = rng.get("column")
        note = "按自增区间删除"
        if not rng.get("continuous", False):
            note += "；区间内行数与写入行数不一致，可能有其它会话插入的行，执行前需人工核对"
        return RollbackPlan(
            available=True,
            sql=[f"DELETE FROM {full} WHERE {q(col)} BETWEEN {rng['start']} AND {rng['end']};"],
            undone_by="undo_log",
            note=note,
        )

    rejected = rec.get("explicit_rejected") or ""
    return _unavailable(
        "INSERT 未提供主键列且拿不到自增区间，无法定位写入的行；请按业务唯一键人工补全 DELETE"
        + (f"（{rejected}）" if rejected else ""))


# ---------------------------------------------------------------- UPDATE


def update_reverse(undo: list[dict], database: str, table: str,
                   stmt_index: int | None = None) -> RollbackPlan:
    rows = _mine(undo, stmt_index, "update_row")
    if not rows:
        return _unavailable("未采集到 UPDATE 的 before-image，回滚不可用")

    full = qname(database, table)
    stmts: list[str] = []
    for rec in rows:
        pk = rec.get("pk") or {}
        old = rec.get("old") or {}
        if not pk or not old:
            continue
        sets = ", ".join(f"{q(c)}={sql_literal(v)}" for c, v in old.items())
        where = " AND ".join(f"{q(c)}={sql_literal(v)}" for c, v in pk.items())
        stmts.append(f"UPDATE {full} SET {sets} WHERE {where};")

    if not stmts:
        return _unavailable("before-image 记录缺少主键或旧值，无法生成逐行反向 UPDATE")

    return RollbackPlan(
        available=True,
        sql=stmts,
        undone_by="undo_log",
        note="逐行反向 UPDATE，按主键定位；若这些行在本次执行后又被改过，回滚会覆盖后一次修改",
    )
