"""DML 事实注册表：把 DmlStmt + TableFact 算成规则文件里能引用的原子事实。

四类事实
--------
S 类  语句自身（解析器产出，永远可用）
F 类  表事实（需要 env + database，来自 information_schema）
D 类  派生事实（本文件统一计算，D 类缺依赖时返回 None，绝不猜）
B 类  批次级（整段 SQL 跑一次）

约定
----
``None`` 一律表示「这个事实算不出来」，而不是「否」。规则层据此标 ⏸ 未判定，
不会把"没查"当成"通过"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .common import TableFact
from .dml import DmlStmt

# --------------------------------------------------------------------------
# 类型族：隐式转换判定用
# --------------------------------------------------------------------------

STR_FAMILY = {
    "CHAR", "VARCHAR", "TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT",
    "ENUM", "SET", "JSON", "BINARY", "VARBINARY",
    "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB",
}
NUM_FAMILY = {
    "TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT",
    "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL", "BIT", "YEAR",
    "BOOL", "BOOLEAN",
}
TEMPORAL_FAMILY = {"DATE", "DATETIME", "TIMESTAMP", "TIME"}


def base_type(column_type: str) -> str:
    """``int(10) unsigned`` → ``INT``。"""
    out = []
    for ch in (column_type or "").strip():
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out).upper()


def type_family(column_type: str) -> str:
    bt = base_type(column_type)
    if bt in STR_FAMILY:
        return "str"
    if bt in NUM_FAMILY:
        return "num"
    if bt in TEMPORAL_FAMILY:
        return "time"
    return "other"


# --------------------------------------------------------------------------
# 事实定义
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FactDef:
    name: str
    kind: str                       # bool / int / str / list / enum / map
    note: str
    layer: str                      # S / F / D / B
    requires: tuple[str, ...] = ()   # 依赖的其它事实名（缺失即本事实不可算）


FACT_DEFS: list[FactDef] = [
    # ---------------- S 类 ----------------
    FactDef("stmt_kind", "enum", "insert / update / delete / replace", "S"),
    FactDef("target_db", "str", "目标库名（可为空）", "S"),
    FactDef("target_table", "str", "目标表名", "S"),
    FactDef("multi_table", "bool", "表引用数 > 1（JOIN 或多表更新）", "S"),
    FactDef("has_where", "bool", "是否有 WHERE", "S"),
    FactDef("where_complex", "bool", "WHERE 含 OR / 子查询 / 抽不出谓词列，谓词不可靠", "S"),
    FactDef("where_columns", "list", "WHERE 顶层谓词涉及的列", "S"),
    FactDef("has_order_by", "bool", "是否有 ORDER BY", "S"),
    FactDef("has_limit", "bool", "是否有 LIMIT", "S"),
    FactDef("limit_value", "int", "LIMIT 的数值；无 LIMIT 时为 null", "S"),
    FactDef("insert_rows", "int", "VALUES 元组数；INSERT ... SELECT 为 null", "S"),
    FactDef("insert_columns", "list", "显式列名；未写则为空", "S"),
    FactDef("insert_has_null", "bool", "VALUES / SET 中出现 NULL 字面量", "S"),
    FactDef("insert_select", "bool", "INSERT ... SELECT", "S"),
    FactDef("on_duplicate_key", "bool", "带 ON DUPLICATE KEY UPDATE（REPLACE 恒为 true）", "S"),
    FactDef("ignore_modifier", "bool", "IGNORE / LOW_PRIORITY / QUICK 等修饰符", "S"),
    FactDef("set_targets", "list", "SET 左侧的列", "S"),
    FactDef("has_subquery", "bool", "SET 或 WHERE 中出现子查询", "S"),
    FactDef("subquery_target_self", "bool", "子查询引用了被更新的同一张表", "S"),
    FactDef("nondeterministic_calls", "list", "NOW / CURDATE / RAND / UUID 等非确定函数", "S"),
    # ---------------- F 类 ----------------
    FactDef("table_exists", "bool", "目标表是否存在", "F"),
    FactDef("table_rows", "int", "目标表估算行数", "F"),
    FactDef("column_names", "list", "目标表全部列名", "F"),
    FactDef("column_types", "map", "列名 → 类型定义", "F"),
    FactDef("column_nullable", "map", "列名 → 是否允许 NULL", "F"),
    FactDef("index_first_columns", "list", "各索引的首列", "F"),
    # ---------------- D 类 ----------------
    FactDef("missing_insert_columns", "list", "INSERT 列名中目标表不存在的列",
            "D", ("insert_columns", "column_names")),
    FactDef("null_into_notnull", "list", "被赋 NULL 但目标列 NOT NULL 的列",
            "D", ("column_nullable",)),
    FactDef("implicit_conversion_hits", "list", "可能触发隐式类型转换的写法",
            "D", ("column_types",)),
    FactDef("where_index_hit", "bool", "WHERE 列是否有任一命中某索引首列",
            "D", ("index_first_columns", "where_columns")),
    FactDef("where_has_id", "bool", "WHERE 是否带 ID 或 *_ID 条件", "D"),
    FactDef("indexed_col_func_calls", "list", "索引列上的函数运算 / 前置 %LIKE",
            "D", ("index_first_columns",)),
    FactDef("affected_rows_upper", "int", "影响行数上界：有 LIMIT 取 LIMIT，否则取表行数",
            "D", ("table_rows",)),
    # ---------------- B 类 ----------------
    FactDef("batch_has_dml", "bool", "批次里含 DML", "B"),
    FactDef("batch_has_ddl", "bool", "批次里含 DDL", "B"),
    FactDef("batch_in_transaction", "bool", "整段被 START TRANSACTION / BEGIN … COMMIT 包裹", "B"),
]

FACT_NAMES: dict[str, FactDef] = {d.name: d for d in FACT_DEFS}


# --------------------------------------------------------------------------
# 取值渲染（填充 message / fix 里的 {事实名}）
# --------------------------------------------------------------------------


def render_value(value: Any) -> str:
    if value is None:
        return "（未知）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
        return "、".join(items) if items else "（无）"
    if isinstance(value, dict):
        return "、".join(f"{k}={v}" for k, v in value.items()) or "（无）"
    return str(value)


# --------------------------------------------------------------------------
# 事实集
# --------------------------------------------------------------------------


@dataclass
class FactSet:
    """一条语句（或一个批次）的全部可用事实，按需惰性计算 + 缓存。"""

    stmt: DmlStmt
    fact: TableFact | None = None
    batch: dict = field(default_factory=dict)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    # ---------------- 对外 ----------------

    def get(self, name: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        calc = _CALC.get(name)
        if calc is None:
            raise KeyError(f"未注册的事实：{name}")
        # 先占位，避免依赖环导致递归爆栈
        self._cache[name] = None
        value = calc(self)
        self._cache[name] = value
        return value

    def snapshot(self, names: list[str] | None = None) -> dict:
        """调试/审计用：把本次用到的事实快照成可序列化的 dict。"""
        keys = names or list(FACT_NAMES)
        return {k: self.get(k) for k in keys if k in _CALC}

    @property
    def assignments(self) -> dict[str, list]:
        """本次写入的「列 → 值 tokens」：INSERT 的 VALUES / SET，UPDATE 的 SET。"""
        out: dict[str, list] = {}
        if self.stmt.kind == "update":
            out.update(self.stmt.set_values)
        else:
            out.update(self.stmt.insert_col_values)
            out.update(self.stmt.set_values)
        return out

    # ---------------- S 类 ----------------

    def _s_stmt_kind(self):
        return self.stmt.kind

    def _s_target_db(self):
        return self.stmt.db

    def _s_target_table(self):
        return self.stmt.table

    def _s_multi_table(self):
        return bool(self.stmt.multi_table)

    def _s_has_where(self):
        return bool(self.stmt.has_where)

    def _s_where_complex(self):
        return bool(self.stmt.where_complex)

    def _s_where_columns(self):
        return list(self.stmt.where_columns)

    def _s_has_order_by(self):
        return bool(self.stmt.has_order_by)

    def _s_has_limit(self):
        return bool(self.stmt.has_limit)

    def _s_limit_value(self):
        return self.stmt.limit_value

    def _s_insert_rows(self):
        return self.stmt.insert_rows

    def _s_insert_columns(self):
        return list(self.stmt.insert_columns)

    def _s_insert_has_null(self):
        return bool(self.stmt.insert_has_null)

    def _s_insert_select(self):
        return bool(self.stmt.insert_select)

    def _s_on_duplicate_key(self):
        return bool(self.stmt.on_duplicate_key)

    def _s_ignore_modifier(self):
        return bool(self.stmt.ignore_modifier)

    def _s_set_targets(self):
        return list(self.stmt.set_targets)

    def _s_has_subquery(self):
        return bool(self.stmt.has_subquery)

    def _s_subquery_target_self(self):
        return bool(self.stmt.subquery_target_self)

    def _s_nondeterministic_calls(self):
        return list(self.stmt.nondeterministic_calls)

    # ---------------- F 类 ----------------

    def _f_table_exists(self):
        return None if self.fact is None else self.fact.exists

    def _f_table_rows(self):
        return None if self.fact is None else self.fact.table_rows

    def _f_column_names(self):
        if self.fact is None or not self.fact.columns:
            return None
        return sorted(self.fact.columns)

    def _f_column_types(self):
        if self.fact is None or not self.fact.columns:
            return None
        return dict(self.fact.columns)

    def _f_column_nullable(self):
        if self.fact is None or not self.fact.nullable:
            return None
        return dict(self.fact.nullable)

    def _f_index_first_columns(self):
        # 取不到表结构 → None（不猜）；表结构取到了但一个索引都没有 → []（真的是无索引）
        if self.fact is None or self.fact.exists is None or not self.fact.columns:
            return None
        out: list[str] = []
        for cols in self.fact.indexes.values():
            if cols and cols[0] and cols[0].upper() not in out:
                out.append(cols[0].upper())
        return out

    # ---------------- D 类 ----------------

    def _d_missing_insert_columns(self):
        names = self.get("column_names")
        if names is None:
            return None
        cols = self.get("insert_columns") or []
        if not cols:
            return []                      # 没写列名是 DM-11 的事，这里不重复报
        upper = {n.upper() for n in names}
        return [c for c in cols if c.upper() not in upper]

    def _d_null_into_notnull(self):
        nullable = self.get("column_nullable")
        if nullable is None:
            return None
        hits: list[str] = []
        for col, toks in self.assignments.items():
            if len(toks) != 1:
                continue
            t = toks[0]
            if t.kind == "word" and t.value.upper() == "NULL":
                if nullable.get(col.upper()) is False and col not in hits:
                    hits.append(col)
        return hits

    def _d_implicit_conversion_hits(self):
        types = self.get("column_types")
        if types is None:
            return None
        hits: list[str] = []
        # ① WHERE：字符串列与数字常量比较 → 列被转成数字，索引失效
        for p in self.stmt.where_predicates:
            col = str(p.get("col") or "")
            fam = type_family(types.get(col.upper(), ""))
            kind = p.get("kind") or ""
            if fam == "str" and kind == "number":
                hits.append(f"WHERE {col} {p.get('op')} {p.get('value')}（字符串列比数字）")
            elif fam == "num" and kind == "string":
                hits.append(f"WHERE {col} {p.get('op')} '{p.get('value')}'（数字列比字符串）")
        # ② 赋值：SET / VALUES 两侧类型不符
        for col, toks in self.assignments.items():
            if len(toks) != 1:
                continue
            fam = type_family(types.get(col.upper(), ""))
            t = toks[0]
            if fam == "str" and t.kind == "number":
                hits.append(f"{col} = {t.value}（字符串列赋数字）")
            elif fam == "num" and t.kind == "string":
                hits.append(f"{col} = '{t.value}'（数字列赋字符串）")
        return hits

    def _d_where_index_hit(self):
        firsts = self.get("index_first_columns")
        if firsts is None:
            return None
        cols = self.get("where_columns") or []
        if not cols:
            return None                    # 谓词抽不准 → 不猜
        firsts_u = {c.upper() for c in firsts}
        return any(c.upper() in firsts_u for c in cols)

    def _d_where_has_id(self):
        return bool(self.stmt.where_has_id)

    def _d_indexed_col_func_calls(self):
        firsts = self.get("index_first_columns")
        if firsts is None:
            return None
        firsts_u = {c.upper() for c in firsts}
        out: list[str] = []
        for col in self.stmt.func_wrapped_columns:
            if col.upper() in firsts_u:
                out.append(f"{col} 被函数包裹")
        for col in self.stmt.leading_like_columns:
            if col.upper() in firsts_u:
                out.append(f"{col} LIKE '%…' 前置通配")
        return out

    def _d_affected_rows_upper(self):
        if self.get("has_limit"):
            return self.get("limit_value")
        return self.get("table_rows")

    # ---------------- B 类 ----------------

    def _b(self, key: str):
        return self.batch.get(key)

    def _b_batch_has_dml(self):
        return self._b("batch_has_dml")

    def _b_batch_has_ddl(self):
        return self._b("batch_has_ddl")

    def _b_batch_in_transaction(self):
        return self._b("batch_in_transaction")


_CALC: dict[str, Callable[[FactSet], Any]] = {
    # S
    "stmt_kind": FactSet._s_stmt_kind,
    "target_db": FactSet._s_target_db,
    "target_table": FactSet._s_target_table,
    "multi_table": FactSet._s_multi_table,
    "has_where": FactSet._s_has_where,
    "where_complex": FactSet._s_where_complex,
    "where_columns": FactSet._s_where_columns,
    "has_order_by": FactSet._s_has_order_by,
    "has_limit": FactSet._s_has_limit,
    "limit_value": FactSet._s_limit_value,
    "insert_rows": FactSet._s_insert_rows,
    "insert_columns": FactSet._s_insert_columns,
    "insert_has_null": FactSet._s_insert_has_null,
    "insert_select": FactSet._s_insert_select,
    "on_duplicate_key": FactSet._s_on_duplicate_key,
    "ignore_modifier": FactSet._s_ignore_modifier,
    "set_targets": FactSet._s_set_targets,
    "has_subquery": FactSet._s_has_subquery,
    "subquery_target_self": FactSet._s_subquery_target_self,
    "nondeterministic_calls": FactSet._s_nondeterministic_calls,
    # F
    "table_exists": FactSet._f_table_exists,
    "table_rows": FactSet._f_table_rows,
    "column_names": FactSet._f_column_names,
    "column_types": FactSet._f_column_types,
    "column_nullable": FactSet._f_column_nullable,
    "index_first_columns": FactSet._f_index_first_columns,
    # D
    "missing_insert_columns": FactSet._d_missing_insert_columns,
    "null_into_notnull": FactSet._d_null_into_notnull,
    "implicit_conversion_hits": FactSet._d_implicit_conversion_hits,
    "where_index_hit": FactSet._d_where_index_hit,
    "where_has_id": FactSet._d_where_has_id,
    "indexed_col_func_calls": FactSet._d_indexed_col_func_calls,
    "affected_rows_upper": FactSet._d_affected_rows_upper,
    # B
    "batch_has_dml": FactSet._b_batch_has_dml,
    "batch_has_ddl": FactSet._b_batch_has_ddl,
    "batch_in_transaction": FactSet._b_batch_in_transaction,
}

FACT_NAMES_REGISTERED = frozenset(_CALC)


def batch_facts(kinds: list[str], ddl_present: bool, in_transaction: bool) -> dict:
    """B 类事实：``kinds`` 是本批各语句的 DML 类型（非 DML 不传）。"""
    return {
        "batch_has_dml": bool(kinds),
        "batch_has_ddl": bool(ddl_present),
        "batch_in_transaction": bool(in_transaction),
    }
