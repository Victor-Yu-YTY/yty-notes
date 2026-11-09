"""事实注册表：把解析结果 + 环境 + 对象元数据算成规则可引用的统一事实集。

四类事实：
  S 语句自身（不连库）  G 环境（实例运行态）  O 对象（元数据）  D 派生（本模块统一算）

规则文件只能引用 FACT_SPECS 里注册过的名字，写错了加载即报错。
任何取不到的值一律返回 None 并记入 missing，绝不用默认值糊弄。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import parser as P
from .backend import Provider

# ---------------------------------------------------------------- 事实定义


@dataclass
class FactSpec:
    dtype: str          # bool/int/float/str/list/enum
    group: str          # S/G/O/D
    desc: str


FACT_SPECS: dict[str, FactSpec] = {
    # ---- S 语句自身 ----
    "stmt_kind": FactSpec("enum", "S", "语句类型"),
    "statement_count": FactSpec("int", "S", "批次语句数"),
    "sql_bytes": FactSpec("int", "S", "SQL 字节数"),
    "target_db": FactSpec("str", "S", "目标库"),
    "target_table": FactSpec("str", "S", "目标表"),
    "table_name_length": FactSpec("int", "S", "表名字符数"),
    "alter_ops": FactSpec("list", "S", "ALTER 的操作类型列表"),
    "add_columns": FactSpec("list", "S", "新增的列"),
    "modify_columns": FactSpec("list", "S", "被变更/删除的列"),
    "index_name": FactSpec("str", "S", "新建索引名"),
    "index_columns": FactSpec("list", "S", "新建索引的列"),
    "insert_columns": FactSpec("list", "S", "INSERT 显式列名"),
    "insert_rows": FactSpec("int", "S", "INSERT 写入行数（元组数）"),
    "insert_is_select": FactSpec("bool", "S", "是否 INSERT ... SELECT"),
    "insert_ignore": FactSpec("bool", "S", "是否 INSERT IGNORE"),
    "insert_on_duplicate": FactSpec("bool", "S", "是否 ON DUPLICATE KEY UPDATE"),
    "set_columns": FactSpec("list", "S", "UPDATE 的 SET 列"),
    "has_where": FactSpec("bool", "S", "UPDATE 是否带 WHERE"),
    "update_multi_table": FactSpec("bool", "S", "是否多表 UPDATE"),
    "batch_has_ddl": FactSpec("bool", "S", "批次含 DDL"),
    "batch_has_dml": FactSpec("bool", "S", "批次含 DML"),
    # ---- G 环境 ----
    "env": FactSpec("str", "G", "环境名"),
    "env_enabled": FactSpec("bool", "G", "环境是否开放执行"),
    "allowed_types": FactSpec("list", "G", "本环境开放的语句类型"),
    "database": FactSpec("str", "G", "库名"),
    "database_exists": FactSpec("bool", "G", "库是否存在"),
    "missing_privileges": FactSpec("list", "G", "账号缺少的权限"),
    "replica_lag_seconds": FactSpec("int", "G", "从库延迟秒数"),
    "long_trx_count": FactSpec("int", "G", "长事务个数"),
    "disk_headroom_gb": FactSpec("float", "G", "可用磁盘空间"),
    "in_window": FactSpec("bool", "G", "当前是否在执行窗口内"),
    "window": FactSpec("str", "G", "执行窗口"),
    "default_engine": FactSpec("str", "G", "实例默认存储引擎"),
    "recent_same_sql_count": FactSpec("int", "G", "近期相同 SQL 执行次数"),
    # ---- O 对象 ----
    "table_exists": FactSpec("bool", "O", "表是否存在"),
    "table_rows": FactSpec("int", "O", "表行数（估算）"),
    "table_size_gb": FactSpec("float", "O", "表体积 GB"),
    "table_engine": FactSpec("str", "O", "表引擎"),
    "column_names": FactSpec("list", "O", "现有列名"),
    "notnull_columns": FactSpec("list", "O", "NOT NULL 且无默认值的列"),
    "index_names": FactSpec("list", "O", "现有索引名"),
    "table_triggers": FactSpec("list", "O", "表上的触发器"),
    "create_table_engine": FactSpec("str", "O", "建表语句指定的引擎"),
    # ---- D 派生 ----
    "add_columns_existing": FactSpec("list", "D", "要新增但已存在的列"),
    "modify_columns_missing": FactSpec("list", "D", "要变更但不存在的列"),
    "index_names_existing": FactSpec("list", "D", "要创建但已存在的索引名"),
    "index_columns_missing": FactSpec("list", "D", "索引引用但不存在的列"),
    "insert_columns_missing": FactSpec("list", "D", "插入列中不存在的列"),
    "set_columns_missing": FactSpec("list", "D", "SET 列中不存在的列"),
    "notnull_columns_unfilled": FactSpec("list", "D", "NOT NULL 但未给值的列"),
    "insert_has_pk_column": FactSpec("bool", "D", "插入是否含主键列"),
    "redundant_index_hit": FactSpec("bool", "D", "新索引是否与现有索引前缀重复"),
    "existing_index": FactSpec("str", "D", "被判定冗余的现有索引名"),
    "ddl_algorithm": FactSpec("enum", "D", "INSTANT/INPLACE/COPY"),
    "required_disk_gb": FactSpec("float", "D", "本次 DDL 需要的临时空间"),
    "estimated_minutes": FactSpec("int", "D", "预估耗时分钟"),
    "rows_estimate": FactSpec("int", "D", "UPDATE 影响行数估算"),
    "rows_estimate_method": FactSpec("enum", "D", "values_count/count_query/limit/unknown"),
    "where_index_hit": FactSpec("bool", "D", "WHERE 是否命中索引"),
}


@dataclass
class FactSet:
    values: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)

    def get(self, name, default=None):
        return self.values.get(name, default)

    def has(self, name) -> bool:
        return name in self.values

    def put(self, name: str, value) -> None:
        # 值一律存下（含 None）：None 表示「取到了但为空」，同时记入 missing 供 fail-closed 判断
        self.values[name] = value
        if value is None:
            self.missing.append(name)

    def merge(self, other: "FactSet") -> None:
        self.values.update(other.values)
        self.missing.extend(other.missing)

    @property
    def only(self) -> dict:
        return {k: v for k, v in self.values.items()}


@dataclass
class CollectCtx:
    env: str
    env_cfg: dict
    scope: dict
    thresholds: dict
    provider: Provider
    default_db: str = ""
    recent_same_sql_count: int = 0
    now: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------- 工具


def _norm(name: str | None) -> str:
    return (name or "").strip().upper()


def _in_window(window: str | None, now: datetime) -> bool:
    if not window:
        return True
    m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", window)
    if not m:
        return True
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _required_privs(kind: str) -> set[str]:
    return {
        P.K_CREATE_TABLE: {"CREATE"},
        P.K_ALTER_TABLE: {"ALTER"},
        P.K_CREATE_INDEX: {"INDEX", "ALTER"},
        P.K_INSERT: {"INSERT"},
        P.K_UPDATE: {"UPDATE"},
    }.get(kind, set())


def _varchar_len(type_sql: str) -> int | None:
    m = re.match(r"varchar\s*\(\s*(\d+)", (type_sql or "").strip(), re.I)
    return int(m.group(1)) if m else None


def _op_algorithm(op: P.AlterOp, cols: dict[str, dict]) -> str | None:
    """单个 ALTER op 在 MySQL 8.0 上需要的算法；None = 判不了（不猜）。"""
    kind = op.kind
    if kind == P.OP_ADD_COLUMN:
        if re.search(r"\b(AFTER|FIRST)\b", op.raw, re.I):
            return "COPY"
        return "INSTANT"
    if kind == P.OP_MODIFY_COLUMN:
        col = cols.get(_norm(op.column))
        if col is None:
            # 列本来就不存在（会被 EX-A03 拦），评估耗时没有意义，别硬塞一个 COPY
            return None
        old_type = col.get("type", "")
        # 直接在原始子句里找 VARCHAR(n)，不依赖拆分位置
        m = re.search(r"VARCHAR\s*\(\s*(\d+)", op.raw, re.I)
        new_len = int(m.group(1)) if m else None
        old_len = _varchar_len(old_type)
        if old_len is not None and new_len is not None:
            same_side = (old_len <= 255) == (new_len <= 255)
            return "INPLACE" if same_side else "COPY"
        return "COPY"
    if kind == P.OP_CHANGE_COLUMN:
        return None if cols.get(_norm(op.column)) is None else "COPY"
    if kind == P.OP_DROP_COLUMN:
        return "INPLACE"
    if kind == P.OP_ADD_INDEX:
        return "INPLACE"
    if kind == P.OP_ADD_PRIMARY:
        return "COPY"
    if kind == P.OP_DROP_INDEX:
        return "INPLACE"
    return "COPY"


_ALG_WEIGHT = {"INSTANT": 0, "INPLACE": 1, "COPY": 2}


def _ddl_algorithm(ops: list[P.AlterOp], cols: dict[str, dict]) -> str | None:
    if not ops:
        return None
    algs = [a for a in (_op_algorithm(op, cols) for op in ops) if a is not None]
    if not algs:
        return None
    return max(algs, key=lambda a: _ALG_WEIGHT.get(a, 2))


def _redundant(cols: list[str], indexes: list[dict] | None) -> str | None:
    new = [_norm(c) for c in cols]
    if not new or not indexes:
        return None
    for ix in indexes:
        old = [_norm(c) for c in ix.get("columns") or []]
        if not old:
            continue
        if len(new) <= len(old) and old[:len(new)] == new:
            return ix.get("name")
    return None


# ---------------------------------------------------------------- 采集


def collect_batch(stmts: list[P.Statement], ctx: CollectCtx) -> FactSet:
    fs = FactSet()
    fs.put("statement_count", len(stmts))
    fs.put("sql_bytes", sum(len(s.raw.encode("utf-8")) for s in stmts))
    kinds = {s.kind for s in stmts}
    fs.put("batch_has_ddl", bool(kinds & P.DDL_KINDS))
    fs.put("batch_has_dml", bool(kinds & P.DML_KINDS))
    return fs


def collect_env(stmts: list[P.Statement], ctx: CollectCtx) -> FactSet:
    fs = FactSet()
    prov = ctx.provider
    fs.put("env", ctx.env)
    fs.put("env_enabled", bool(ctx.env_cfg.get("enabled", False)))
    # 环境级开放类型：EX-G01 判的就是这个。原来它取 scope.allowed_types，
    # 一旦某个环境单独收窄类型，判定阶段会按全局放行、执行阶段才被拒，两边打架。
    fs.put("allowed_types", list(ctx.env_cfg.get("allowed_types")
                                 or ctx.scope.get("allowed_types") or []))
    fs.put("window", ctx.env_cfg.get("window"))
    fs.put("in_window", _in_window(ctx.env_cfg.get("window"), ctx.now))
    fs.put("recent_same_sql_count", ctx.recent_same_sql_count)

    dbs = {s.database for s in stmts if s.database}
    db = next(iter(dbs), ctx.default_db)
    fs.put("database", db)
    # 库级隔离交给执行账号的 GRANT：代码层不再维护库白名单。
    # 库不存在时由 EX-T02（建表）/ 各类型的「表不存在」检查兜住。
    fs.put("database_exists", prov.database_exists(db) if db else None)

    sv = prov.server_vars() or {}
    fs.put("default_engine", sv.get("default_storage_engine"))

    privs = prov.privileges()
    if privs is None:
        fs.put("missing_privileges", None)
    else:
        need: set[str] = {"SELECT"}
        for s in stmts:
            need |= _required_privs(s.kind)
        if "ALL PRIVILEGES" in privs:
            fs.values["missing_privileges"] = []
        else:
            missing = sorted(p for p in need if p not in privs and not (p == "INDEX" and "ALTER" in privs))
            fs.values["missing_privileges"] = missing

    fs.put("replica_lag_seconds", prov.replica_lag())
    fs.put("long_trx_count", prov.long_trx_count(ctx.thresholds.get("long_trx_seconds", 60)))
    fs.put("disk_headroom_gb", prov.disk_free_gb())
    return fs


def collect_statement(st: P.Statement, ctx: CollectCtx) -> FactSet:
    fs = FactSet()
    prov = ctx.provider
    db = st.database or ctx.default_db
    tbl = st.table

    fs.put("stmt_kind", st.kind)
    fs.put("target_db", db)
    fs.put("target_table", tbl)
    fs.put("table_name_length", len(tbl) if tbl else None)

    if st.kind == P.K_CREATE_TABLE:
        # 未显式指定时给空串而不是 None，让 EX-T04 的 not_in 判定能命中
        fs.put("create_table_engine", st.engine or "")
    if st.kind == P.K_ALTER_TABLE:
        fs.put("alter_ops", [op.kind for op in st.ops])
        fs.put("add_columns", [op.column for op in st.ops if op.kind == P.OP_ADD_COLUMN and op.column])
        fs.put("modify_columns", [op.column for op in st.ops
                                  if op.kind in (P.OP_MODIFY_COLUMN, P.OP_CHANGE_COLUMN,
                                                 P.OP_DROP_COLUMN) and op.column])
        fs.put("index_name", None)
        fs.put("index_columns", [])
    if st.kind == P.K_CREATE_INDEX:
        fs.put("index_name", st.index_name)
        fs.put("index_columns", st.index_columns)
    if st.kind == P.K_INSERT:
        fs.put("insert_columns", st.insert_columns)
        fs.put("insert_rows", st.insert_rows)
        fs.put("insert_is_select", st.insert_is_select)
        fs.put("insert_ignore", st.insert_ignore)
        fs.put("insert_on_duplicate", st.on_duplicate_key)
    if st.kind == P.K_UPDATE:
        fs.put("set_columns", st.set_columns)
        fs.put("has_where", st.has_where)
        fs.put("update_multi_table", st.multi_table)

    # 对象事实
    info = prov.table_info(db, tbl) if (db and tbl) else None
    if info is None:
        fs.put("table_exists", None)
        fs.put("table_rows", None)
        fs.put("table_size_gb", None)
        fs.put("table_engine", None)
    else:
        fs.values["table_exists"] = bool(info.get("exists"))
        fs.values["table_rows"] = info.get("rows")
        fs.values["table_size_gb"] = info.get("size_gb")
        fs.values["table_engine"] = info.get("engine")

    exists = fs.get("table_exists")
    if exists is False:
        # 表明确不存在 = 没有列、没有索引，用空集合表达，避免后续检查因为「取不到」而二次拦截
        cols: dict | None = {}
        idxs: list | None = []
        fs.values["column_names"] = []
        fs.values["index_names"] = []
    else:
        cols = prov.columns(db, tbl) if (db and tbl and exists) else None
        idxs = prov.indexes(db, tbl) if (db and tbl and exists) else None
        if cols is None:
            fs.put("column_names", None)
        else:
            fs.values["column_names"] = list(cols.keys())
        if idxs is None:
            fs.put("index_names", None)
        else:
            fs.values["index_names"] = [ix.get("name") for ix in idxs]
    if exists:
        fs.put("table_triggers", prov.triggers(db, tbl))
    else:
        fs.values["table_triggers"] = []

    # ---- 派生 ----
    colset = set(_norm(c) for c in (fs.get("column_names") or []))
    have_cols = fs.get("column_names") is not None

    if st.kind == P.K_ALTER_TABLE:
        adds = fs.get("add_columns") or []
        mods = fs.get("modify_columns") or []
        if have_cols:
            fs.values["add_columns_existing"] = [c for c in adds if _norm(c) in colset]
            fs.values["modify_columns_missing"] = [c for c in mods if _norm(c) not in colset]
        name_set = set(_norm(n) for n in (fs.get("index_names") or []))
        want = [op.index_name for op in st.ops
                if op.kind in (P.OP_ADD_INDEX, P.OP_ADD_PRIMARY) and op.index_name]
        if fs.get("index_names") is not None:
            fs.values["index_names_existing"] = [n for n in want if _norm(n) in name_set]

        alg = _ddl_algorithm(st.ops, cols or {})
        fs.put("ddl_algorithm", alg)
        size = fs.get("table_size_gb")
        factor = ctx.thresholds.get("ddl_disk_factor", 1.5)
        floor = ctx.thresholds.get("ddl_disk_floor_gb", 5)
        if alg == "COPY" and size is not None:
            fs.values["required_disk_gb"] = round(max(size * factor, floor), 2)
            fs.values["estimated_minutes"] = max(int(size * 2), 1)
        elif alg in ("INSTANT", "INPLACE"):
            fs.values["required_disk_gb"] = 0.0
            fs.values["estimated_minutes"] = 1

        new_cols: list[str] = []
        for op in st.ops:
            if op.kind == P.OP_ADD_INDEX and op.columns:
                new_cols = op.columns
                break
        red = _redundant(new_cols, idxs)
        fs.put("redundant_index_hit", bool(red) if new_cols else False)
        fs.put("existing_index", red)

        if st.ops:
            want_cols = [c for op in st.ops if op.columns for c in op.columns]
            if have_cols and want_cols:
                fs.values["index_columns_missing"] = [c for c in want_cols if _norm(c) not in colset]

    if st.kind == P.K_CREATE_INDEX:
        if have_cols:
            fs.values["index_columns_missing"] = [
                c for c in st.index_columns if _norm(c) not in colset]
        name_set = set(_norm(n) for n in (fs.get("index_names") or []))
        if fs.get("index_names") is not None and st.index_name:
            fs.values["index_names_existing"] = (
                [st.index_name] if _norm(st.index_name) in name_set else [])
        red = _redundant(st.index_columns, idxs)
        fs.put("redundant_index_hit", bool(red) if st.index_columns else False)
        fs.put("existing_index", red)
        size = fs.get("table_size_gb")
        fs.values.setdefault("required_disk_gb", 0.0)

    if st.kind == P.K_INSERT:
        if have_cols:
            fs.values["insert_columns_missing"] = [
                c for c in st.insert_columns if _norm(c) not in colset]
            notnull = []
            pk = []
            for key, meta in (cols or {}).items():
                if not meta.get("nullable") and not meta.get("has_default") \
                        and not meta.get("auto_increment"):
                    notnull.append(meta.get("name", key))
                if meta.get("primary"):
                    pk.append(key)
            fs.values["notnull_columns"] = notnull
            if st.insert_columns:
                given = set(_norm(c) for c in st.insert_columns)
                fs.values["notnull_columns_unfilled"] = [
                    c for c in notnull if _norm(c) not in given]
                fs.values["insert_has_pk_column"] = bool(pk) and any(
                    _norm(c) in set(pk) for c in st.insert_columns)
            else:
                fs.values["notnull_columns_unfilled"] = []
                fs.values["insert_has_pk_column"] = None
        fs.values["rows_estimate"] = st.insert_rows
        fs.values["rows_estimate_method"] = "values_count" if st.insert_rows is not None else "unknown"

    if st.kind == P.K_UPDATE:
        if have_cols:
            fs.values["set_columns_missing"] = [
                c for c in st.set_columns if _norm(c) not in colset]
        if st.limit_value is not None:
            fs.values["rows_estimate"] = st.limit_value
            fs.values["rows_estimate_method"] = "limit"
        elif st.has_where and st.where_sql and db and tbl:
            n = prov.count_where(db, tbl, st.where_sql,
                                 ctx.thresholds.get("count_timeout_seconds", 3))
            fs.put("rows_estimate", n)
            fs.values["rows_estimate_method"] = "count_query" if n is not None else "unknown"
        else:
            fs.put("rows_estimate", None)
            fs.values["rows_estimate_method"] = "unknown"

        if db and tbl:
            t = prov.explain_type(db, st.raw)
            fs.put("where_index_hit", (t != "ALL") if t else None)

    return fs


def collect(stmts: list[P.Statement], ctx: CollectCtx) -> list[FactSet]:
    """每条语句一份 FactSet，批次与环境事实对每条都复制一份。"""
    batch = collect_batch(stmts, ctx)
    env = collect_env(stmts, ctx)
    out: list[FactSet] = []
    for st in stmts:
        fs = FactSet()
        fs.merge(batch)
        fs.merge(env)
        fs.merge(collect_statement(st, ctx))
        fs.missing = sorted(set(fs.missing))
        out.append(fs)
    return out
