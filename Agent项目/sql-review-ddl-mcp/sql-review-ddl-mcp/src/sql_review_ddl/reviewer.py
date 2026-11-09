"""审核编排：多语句拆分 → 类型识别 → 文档驱动判定 → 聚合四态结论。

对外只有一个入口 ``review_sql()``，由 server.py 的各工具复用。

规则全部来自文档（standards/ddl.md、standards/dml.md）：
  * DDL：按文档顺序跑 ``ddlchecks.run_scope()``（检查 + 参数 + 级别 + 文案都在文档里）；
  * DML：把语句解析成事实，再按文档的判定表达式求值（``engine.run_dml_rules``）。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import replace
from pathlib import Path

from . import metadata as M
from . import spec
from .alter import AlterTable, merge_alters, parse_alter_table, render_alter
from .common import ERROR, INFO, UNKNOWN, ReviewResult, TableFact
from .config import Settings
from .ddlchecks import check_specs, run_scope
from .dmfacts import FactSet, FACT_NAMES_REGISTERED, batch_facts
from .dml import DmlStmt, is_transaction_control, parse_dml
from .engine import missing_facts as dml_missing_facts, run_dml_rules
from .model import (Column, Index, IndexCol, parse_create_table, render_create_index,
                    render_table)
from .tokens import ParseError, find_matching, split_statements, split_top, tokenize

TYPE_CREATE_TABLE = "CREATE TABLE"
TYPE_ALTER_TABLE = "ALTER TABLE"
TYPE_CREATE_INDEX = "CREATE INDEX"
TYPE_DROP_INDEX = "DROP INDEX"
TYPE_DROP_TABLE = "DROP TABLE"
TYPE_TRUNCATE = "TRUNCATE"
TYPE_DROP_DATABASE = "DROP DATABASE"
TYPE_INSERT = "INSERT"
TYPE_UPDATE = "UPDATE"
TYPE_DELETE = "DELETE"
TYPE_REPLACE = "REPLACE"
TYPE_TRANSACTION = "TRANSACTION"
TYPE_UNSUPPORTED = "UNSUPPORTED"

# 语句类型 → standards/*.md「适用」取值
DML_SCOPE = {
    TYPE_INSERT: "insert",
    TYPE_UPDATE: "update",
    TYPE_DELETE: "delete",
    TYPE_REPLACE: "replace",
}
DDL_SCOPE = {
    TYPE_CREATE_TABLE: "create_table",
    TYPE_ALTER_TABLE: "alter_table",
    TYPE_CREATE_INDEX: "create_index",
    TYPE_DROP_INDEX: "drop_index",
    TYPE_DROP_TABLE: "drop_table",
    TYPE_TRUNCATE: "truncate",
    TYPE_DROP_DATABASE: "drop_database",
}
DML_TYPES = tuple(DML_SCOPE)
DDL_TYPES = tuple(DDL_SCOPE)

STATEMENT_LABEL = {
    TYPE_CREATE_TABLE: "建表",
    TYPE_ALTER_TABLE: "变更表",
    TYPE_CREATE_INDEX: "建索引",
    TYPE_DROP_INDEX: "删索引",
    TYPE_DROP_TABLE: "删表",
    TYPE_TRUNCATE: "清空表",
    TYPE_DROP_DATABASE: "删库",
    TYPE_INSERT: "插入数据",
    TYPE_UPDATE: "更新数据",
    TYPE_DELETE: "删除数据",
    TYPE_REPLACE: "覆盖写入",
    TYPE_TRANSACTION: "事务控制",
    TYPE_UNSUPPORTED: "不支持的语句",
}


# --------------------------------------------------------------------------
# 规则集：按 mtime 热加载（改 md 不用重启）
# --------------------------------------------------------------------------

_RULESETS: dict[str, tuple[tuple, object, str]] = {}


def _mtime(p) -> float:
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return 0.0


# v1.4.0 修订：规范版本号按 mtime 惰性重读（改 STANDARD_VERSION 也不用重启）
_VER_CACHE: dict[str, tuple[tuple, str]] = {}


def standard_version(settings: Settings) -> str:
    """config/STANDARD_VERSION 的热加载读法（文件不存在时回退到启动时读到的值）。"""
    p = Path(getattr(settings, "standard_version_file", "") or "")
    key = str(p)
    try:
        st = p.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return getattr(settings, "standard_version", "unknown") or "unknown"
    hit = _VER_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        ver = p.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        ver = getattr(settings, "standard_version", "unknown") or "unknown"
    _VER_CACHE[key] = (stamp, ver)
    return ver


# 热加载成功后的回调（server 用它把最新规则集回写进 _STATE，healthz/审计才不会是旧版本）
_RELOAD_HOOKS: list = []


def on_reload(fn) -> None:
    """注册"规则集重新加载完成"的回调：fn(doc, ruleset, warning)。"""
    if fn not in _RELOAD_HOOKS:
        _RELOAD_HOOKS.append(fn)


def get_ruleset(settings: Settings, doc: str, force: bool = False):
    """返回 (RuleSet|None, warning)。doc = 'ddl' / 'dml'。

    按 (mtime, size) 热加载；写坏不切换、继续用上一版并带 warning。
    """
    path = settings.standard_path if doc == "ddl" else settings.dml_standard_path
    key = f"{doc}:{path}"
    stamp = (_mtime(path), _mtime(settings.override_path) if doc == "ddl" else 0.0)
    hit = _RULESETS.get(key)
    if hit and hit[0] == stamp and not force:
        return hit[1], hit[2]
    rs, warn = spec.get_ruleset(path, doc, FACT_NAMES_REGISTERED, check_specs(), force=force)
    if rs is not None and doc == "ddl":
        rs = _apply_override(rs, settings)
    _RULESETS[key] = (stamp, rs, warn)
    for hook in list(_RELOAD_HOOKS):
        try:
            hook(doc, rs, warn)
        except Exception:        # noqa: BLE001  回调异常不能影响审核
            pass
    return rs, warn


def _apply_override(rs, settings: Settings):
    """应急兜底：config/rules.override.yaml 覆盖级别/参数/停用（正常留空）。"""
    p = Path(getattr(settings, "override_path", "") or "")
    if not p.exists():
        return rs
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        rs.warnings.append(f"override 解析失败（已忽略）：{exc}")
        return rs
    by_id = {r.id: r for r in rs.rules}
    for item in (data.get("rules") or []):
        rid = str(item.get("id") or "")
        rule = by_id.get(rid)
        if rule is None:
            rs.warnings.append(f"override 里忽略了未知规则：{rid}")
            continue
        if "level" in item:
            lv = spec.LEVEL_BY_MARK.get(str(item["level"]), str(item["level"]))
            if lv in spec.LEVELS:
                rule.level = lv
        if isinstance(item.get("params"), dict):
            rule.params.update({k: v for k, v in item["params"].items()})
        if "enabled" in item:
            rule.enabled = bool(item["enabled"])
        if "message" in item and item["message"]:
            rule.message = str(item["message"])
        if "fix" in item and item["fix"]:
            rule.fix = str(item["fix"])
    return rs


def doc_thresholds(settings: Settings, ddl_rs, dml_rs) -> dict:
    """阈值优先级：文档阈值表 > 规则参数 > settings.yaml 兜底。"""
    th = {}
    base = getattr(settings, "thresholds", None)
    if base is not None:
        th.update({k: getattr(base, k) for k in getattr(base, "__dataclass_fields__", {})})
    for rs in (ddl_rs, dml_rs):
        if rs is None:
            continue
        th.update({k: v for k, v in rs.thresholds.items()})
    return th


# --------------------------------------------------------------------------
# 语句类型识别
# --------------------------------------------------------------------------


def classify(sql: str) -> str:
    toks = tokenize(sql)
    words = [t.value.upper() for t in toks if t.kind == "word"]
    if not words:
        return TYPE_UNSUPPORTED
    head = words[0]
    if head == "CREATE":
        if len(words) > 1 and words[1] in ("TABLE", "TEMPORARY"):
            return TYPE_CREATE_TABLE
        if "INDEX" in words[:4]:
            return TYPE_CREATE_INDEX
        return TYPE_UNSUPPORTED
    if head == "ALTER":
        if len(words) > 1 and words[1] == "TABLE":
            return TYPE_ALTER_TABLE
        return TYPE_UNSUPPORTED
    if head == "DROP":
        if len(words) > 1 and words[1] in ("TABLE", "TEMPORARY"):
            return TYPE_DROP_TABLE
        if len(words) > 1 and words[1] in ("DATABASE", "SCHEMA"):
            return TYPE_DROP_DATABASE
        if len(words) > 1 and words[1] == "INDEX":
            return TYPE_DROP_INDEX
        return TYPE_UNSUPPORTED
    if head == "TRUNCATE":
        return TYPE_TRUNCATE
    if head == "INSERT":
        return TYPE_INSERT
    if head == "UPDATE":
        return TYPE_UPDATE
    if head == "DELETE":
        return TYPE_DELETE
    if head == "REPLACE":
        return TYPE_REPLACE
    if is_transaction_control(sql):
        return TYPE_TRANSACTION
    return TYPE_UNSUPPORTED


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _col_from_type(name: str, type_sql: str) -> Column:
    m = re.match(r"([A-Za-z]+)\s*(?:\(([^)]*)\))?\s*(unsigned)?", (type_sql or "").strip(), re.I)
    typ = (m.group(1).upper() if m else "VARCHAR")
    args = [a.strip() for a in m.group(2).split(",")] if (m and m.group(2)) else []
    col = Column(name=name, type=typ, args=args)
    if m and m.group(3):
        col.signed = "UNSIGNED"
    return col


def _coltypes_from_fact(fact: TableFact | None) -> dict[str, Column] | None:
    if not fact or not fact.columns:
        return None
    return {k: _col_from_type(k, v) for k, v in fact.columns.items()}


def _fetch(settings: Settings, env: str | None, db: str, table: str, cache) -> TableFact | None:
    if not (env and db and table):
        return None
    return M.fetch_fact(settings, env, db, table, cache)


def _types_of(rs, rule_id: str) -> list[str]:
    """取某条规则（如 TB-03）参数里的 types，供 AL-05 复用。"""
    if rs is None:
        return []
    r = rs.get(rule_id)
    return list(r.p_list("types")) if r is not None else []


# --------------------------------------------------------------------------
# 各类语句审核（判定逻辑全在文档，这里只搭上下文）
# --------------------------------------------------------------------------


def review_create(sql: str, res: ReviewResult, settings: Settings, env: str | None,
                  database: str | None, cache: dict | None = None) -> None:
    tbl = parse_create_table(sql)
    res.table = tbl
    if database and not tbl.db:
        tbl.db = database
    raw_name = tbl.name
    fact = None
    db_name = tbl.db or database or ""
    candidates = []
    for nm in (raw_name, (raw_name or "").upper(), (raw_name or "").lower()):
        if nm and nm not in candidates:
            candidates.append(nm)
    for nm in candidates:
        fact = _fetch(settings, env, db_name, nm, cache)
        if fact is not None and fact.exists:
            break
    res.facts = fact
    ctx = {
        "table": tbl, "database": db_name, "fact": fact,
        "indexes": [i for i in tbl.indexes if i.kind != "FOREIGN"],
        "coltypes": {c.name: c for c in tbl.columns},
        "index_count": len([i for i in tbl.indexes if i.kind != "FOREIGN"]),
        "touching_rows": False,
    }
    run_scope(res, settings.ddl_ruleset, "create_table", ctx)
    # 外键索引：TB 侧已被过滤，交给 IX-04 单独判定
    for idx in [i for i in tbl.indexes if i.kind == "FOREIGN"]:
        ctx["indexes"] = [idx]
        run_scope(res, settings.ddl_ruleset, "create_index", dict(ctx, fact=None))
    tbl.indexes = [i for i in tbl.indexes if i.kind != "FOREIGN" and i.columns]


def review_alter(sql: str, res: ReviewResult, settings: Settings, env: str | None,
                 database: str | None, cache: dict | None = None) -> None:
    alter = parse_alter_table(sql)
    res.alter = alter
    if database and not alter.db:
        alter.db = database
    rs = settings.ddl_ruleset
    fact = _fetch(settings, env, alter.db or database or "", alter.name, cache)
    res.facts = fact
    added = [op.index for op in alter.ops
             if op.kind in ("ADD_INDEX", "ADD_PRIMARY") and op.index is not None]
    existing_idx = len(fact.indexes) if fact is not None and fact.indexes else None
    heavy = {op.kind for op in alter.ops} & {
        "MODIFY_COLUMN", "CHANGE_COLUMN", "DROP_COLUMN", "RENAME_TABLE", "TABLE_OPTION",
        "OTHER"}
    ctx = {
        "alter": alter, "database": alter.db or database or "", "fact": fact,
        "indexes": added,
        "coltypes": _coltypes_from_fact(fact),
        "index_count": (existing_idx + 1) if (existing_idx is not None and added) else None,
        "touching_rows": bool(heavy),
        "float_types": _types_of(rs, "TB-03"),
        "enum_types": _types_of(rs, "TB-04"),
        "blob_types": _types_of(rs, "TB-05"),
        "drop_index_names": [op.name for op in alter.ops if op.kind == "DROP_INDEX"],
        "target_name": alter.name,
    }
    run_scope(res, rs, "alter_table", ctx)
    alter.ops = [op for op in alter.ops
                 if op.kind != "ADD_INDEX" or op.index is None or op.index.kind != "FOREIGN"]


def review_create_index(sql: str, res: ReviewResult, settings: Settings, env: str | None,
                        database: str | None, cache: dict | None = None) -> None:
    toks = tokenize(sql)
    words = [t.value.upper() for t in toks]
    kind = "INDEX"
    if "UNIQUE" in words[:4]:
        kind = "UNIQUE"
    elif "FULLTEXT" in words[:4]:
        kind = "FULLTEXT"
    elif "SPATIAL" in words[:4]:
        kind = "SPATIAL"
    idx = Index(kind=kind)
    i = 0
    while i < len(toks) and not toks[i].is_kw("INDEX", "KEY"):
        i += 1
    i += 1
    if i < len(toks) and toks[i].kind in ("word", "ident"):
        idx.name = toks[i].value
        i += 1
    while i < len(toks) and not toks[i].is_kw("ON"):
        i += 1
    i += 1
    table_name = ""
    table_db = database or ""
    if i < len(toks):
        table_name = toks[i].value
        i += 1
        if i + 1 < len(toks) and toks[i].is_punct("."):
            table_db = table_name
            table_name = toks[i + 1].value
            i += 2
    while i < len(toks) and not toks[i].is_punct("("):
        i += 1
    if i < len(toks):
        close = find_matching(toks, i)
        for part in split_top(toks[i + 1:close]):
            name = part[0].value
            prefix = None
            if len(part) > 2 and part[1].is_punct("("):
                prefix = part[2].value
            idx.columns.append(IndexCol(name=name, prefix_len=prefix))

    fact = _fetch(settings, env, table_db, table_name, cache)
    res.facts = fact
    ctx = {
        "indexes": [idx], "database": table_db, "fact": fact,
        "coltypes": _coltypes_from_fact(fact),
        "index_count": (len(fact.indexes) + 1) if (fact is not None and fact.indexes) else None,
        "duplicate_index": (idx.name if (fact is not None and fact.exists and idx.name
                                         and fact.has_index(idx.name)) else None),
        "touching_rows": True,
        "table_name": table_name,
    }
    run_scope(res, settings.ddl_ruleset, "create_index", ctx)
    if fact is not None and fact.exists is False:
        ctx["target_name"] = table_name
    res.extra["create_index"] = {"index": idx, "db": table_db, "table": table_name}


def review_drop_index(sql: str, res: ReviewResult, settings: Settings, env: str | None,
                      database: str | None, cache: dict | None = None) -> None:
    toks = tokenize(sql)
    name = ""
    table_db = database or ""
    table = ""
    for i, t in enumerate(toks):
        if t.is_kw("INDEX", "KEY") and i + 1 < len(toks):
            name = toks[i + 1].value
            break
    for i, t in enumerate(toks):
        if t.is_kw("ON") and i + 1 < len(toks):
            table = toks[i + 1].value
            if i + 3 < len(toks) and toks[i + 2].is_punct("."):
                table_db = table
                table = toks[i + 3].value
            break
    fact = _fetch(settings, env, table_db, table, cache)
    res.facts = fact
    ctx = {"index_name": name, "target_name": name, "fact": fact,
           "database": table_db, "table_name": table, "indexes": []}
    run_scope(res, settings.ddl_ruleset, "drop_index", ctx)


def review_destructive(sql: str, res: ReviewResult, kind: str) -> None:
    toks = [t for t in tokenize(sql) if t.kind in ("word", "ident")]
    name = toks[1].value if len(toks) > 1 else ""
    if name.upper() in ("IF", "TABLE", "TEMPORARY"):
        name = toks[2].value if len(toks) > 2 else name
    ctx = {"target_name": name, "table_name": name}
    run_scope(res, res.ruleset, DDL_SCOPE[kind], ctx)


# --------------------------------------------------------------------------
# DML 审核：解析出事实 → 文档表达式判定
# --------------------------------------------------------------------------


def review_dml_stmt(sql: str, res: ReviewResult, settings: Settings, env: str | None,
                    database: str | None, cache: dict | None,
                    rules_warn: str = "") -> None:
    scope = DML_SCOPE[res.statement_type]
    st = parse_dml(sql, scope)
    db = st.db or database or ""
    res.extra["dml"] = {
        "kind": st.kind, "database": db, "table": st.table,
        "multi_table": st.multi_table, "has_where": st.has_where,
        "where_columns": list(st.where_columns),
        "insert_rows": st.insert_rows, "insert_columns": list(st.insert_columns),
        "has_limit": st.has_limit, "limit_value": st.limit_value,
        "has_order_by": st.has_order_by,
    }
    target = f"表 `{db}.{st.table}`" if db else f"表 `{st.table}`"

    if res.ruleset is None:
        res.add("ENGINE-02", ERROR, target,
                f"DML 规则不可用，本条无法审核：{rules_warn or 'standards/dml.md 缺失或校验失败'}",
                "请联系 DBA 修复 standards/dml.md；本结论不可作为提交依据")
        res.extra["missing_facts"] = []
        return

    if st.unsure:
        res.add("PARSE-01", UNKNOWN, target,
                f"该 {scope.upper()} 语句结构复杂，部分事实无法确定：{st.note}",
                "请改用更直白的写法（单表、显式列名、无嵌套子查询）后重提")

    fact = _fetch(settings, env, db, st.table, cache)
    res.facts = fact

    facts = FactSet(stmt=st, fact=fact)
    hits = run_dml_rules(res.ruleset, facts, scope, target)
    for h in hits:
        res.add(h.rule.id, h.level, target, h.message, h.fix)
        if h.rule.p_bool("fatal") and not res.fatal:
            # 文档里标了 fatal（如 DM-12 DELETE/REPLACE）→ 整单驳回，不给"补信息"的模糊结论
            res.fatal = f"{h.rule.id}：{h.rule.title} 本通道不出审核结论"
        for qid, blocking in h.rule.asks:
            res.ask(qid, h.rule.id, blocking=blocking, fallback=h.fix or h.message)

    res.extra["missing_facts"] = dml_missing_facts(res.ruleset, facts, scope)
    res.extra["dml_facts"] = {
        k: v for k, v in facts.snapshot([
            "table_exists", "table_rows", "column_names", "index_first_columns",
            "where_index_hit", "where_has_id", "missing_insert_columns",
            "null_into_notnull", "implicit_conversion_hits",
            "indexed_col_func_calls", "affected_rows_upper",
        ]).items() if v is not None
    }


def _in_transaction(statements: list[str], types: list[str]) -> bool:
    """整段是否被 START TRANSACTION / BEGIN … COMMIT 包裹（只认最朴素的形态）。"""
    tx_idx = [i for i, t in enumerate(types) if t == TYPE_TRANSACTION]
    body_idx = [i for i, t in enumerate(types) if t != TYPE_TRANSACTION]
    if not body_idx or len(tx_idx) < 2:
        return False
    first, last = tx_idx[0], tx_idx[-1]
    if first > min(body_idx) or last < max(body_idx):
        return False
    head = statements[first].strip().upper()
    tail = statements[last].strip().upper()
    opens = head.startswith("START TRANSACTION") or head == "BEGIN"
    closes = tail.startswith("COMMIT") or tail.startswith("ROLLBACK")
    return opens and closes


def _review_batch(results: list[ReviewResult], statements: list[str], dml_rs) -> None:
    """批次级规则（DM-10 事务包裹、DM-16 DDL/DML 混批）——结论记在首条 DML 上。"""
    dml_results = [r for r in results if r.statement_type in DML_TYPES]
    if not dml_results or dml_rs is None:
        return
    types = [r.statement_type for r in results]
    kinds = [DML_SCOPE[r.statement_type] for r in dml_results
             if r.statement_type in (TYPE_INSERT, TYPE_UPDATE)]
    if not kinds:
        return
    facts = FactSet(
        stmt=DmlStmt(),
        batch=batch_facts(kinds,
                          ddl_present=any(t in DDL_TYPES for t in types),
                          in_transaction=_in_transaction(statements, types)))
    host = dml_results[0]
    for h in run_dml_rules(dml_rs, facts, "batch", "整个批次"):
        host.add(h.rule.id, h.level, "整个批次", h.message, h.fix)
        for qid, blocking in h.rule.asks:
            host.ask(qid, h.rule.id, blocking=blocking, fallback=h.fix or h.message)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _load_doc_ruleset(settings: Settings, doc: str):
    """装载一侧规范文档。

    v1.0.0（拆分）：DDL / DML 各自成服务，本包里通常只有自己那一份 md。
    文档不存在时返回 (None, "")——不报"读取失败"、不反复 stat/open，
    响应里对应侧规则集摘要即 available=false（如实披露"本服务不加载该规范"）。
    """
    path = settings.standard_path if doc == "ddl" else settings.dml_standard_path
    if not Path(path).exists():
        return None, ""
    return get_ruleset(settings, doc)


def review_sql(sql: str, settings: Settings, env: str | None = None,
               database: str | None = None,
               allow_types: tuple[str, ...] = ()) -> dict:
    started = time.perf_counter()
    raw = (sql or "").strip()
    if not raw:
        return _err("INVALID_ARGUMENTS", "sql 不能为空", reason="ARG_SQL_EMPTY")
    if env is not None:
        env = env.strip().lower()
        if env and env not in settings.envs:
            return _err("INVALID_ARGUMENTS",
                        f"env 只支持 {', '.join(sorted(settings.envs))}；不支持 {env}",
                        reason="ARG_ENV_UNSUPPORTED",
                        detail={"supported": sorted(settings.envs), "received": env})

    max_bytes = int(getattr(settings, "max_sql_bytes", 20000) or 0)
    if max_bytes and len(raw.encode("utf-8")) > max_bytes:
        return _err("INVALID_ARGUMENTS",
                    f"单条请求的 SQL 超过 {max_bytes} 字节上限，请分批提交",
                    reason="ARG_SQL_TOO_LONG",
                    detail={"max_bytes": max_bytes, "bytes": len(raw.encode("utf-8"))})

    try:
        statements = split_statements(raw)
    except ParseError as exc:
        return _err("INVALID_ARGUMENTS", f"SQL 词法解析失败：{exc}",
                    reason="ARG_SQL_UNPARSABLE")
    if not statements:
        return _err("INVALID_ARGUMENTS", "未识别到任何 SQL 语句", reason="ARG_NO_STATEMENT")
    max_stmts = int(getattr(settings, "max_statements", 20) or 20)
    if len(statements) > max_stmts:
        return _err("INVALID_ARGUMENTS", f"单次最多审核 {max_stmts} 条语句，请分批提交",
                    reason="ARG_TOO_MANY_STATEMENTS",
                    detail={"statements": len(statements), "max_statements": max_stmts})

    # v1.4.0 修订：工具类型不匹配在**取元数据之前**就判掉（避免白跑一次库查询）
    if allow_types:
        detected = sorted({t for t in (classify(s) for s in statements)
                           if t not in (TYPE_UNSUPPORTED, TYPE_TRANSACTION)
                           and t not in allow_types})
        if detected:
            kinds = "、".join(STATEMENT_LABEL.get(t, t) for t in detected)
            return _err("INVALID_ARGUMENTS",
                        f"该工具只接受 {', '.join(allow_types)}；检测到：{kinds}。"
                        "多条 DDL 请用 SQL Review DDL 服务的 review_ddl，"
                        "多条 DML 请用 SQL Review DML 服务的 review_dml；"
                        "DDL 与 DML 不要放在同一批次（DDL 会隐式提交，打断事务）",
                        reason="ARG_TOOL_TYPE_MISMATCH",
                        detail={"allowed": list(allow_types), "detected": detected})

    ddl_rs, ddl_warn = _load_doc_ruleset(settings, "ddl")
    dml_rs, dml_warn = _load_doc_ruleset(settings, "dml")
    th = doc_thresholds(settings, ddl_rs, dml_rs)
    qcatalog = {}
    try:
        from .config import load_questions
        raw_q = load_questions(settings) or {}
        qcatalog = {str(q.get("id")): q for q in (raw_q.get("questions") or [])
                    if q.get("id")}
    except Exception:            # noqa: BLE001  反问目录缺失不影响审核
        qcatalog = {}
    ctx_settings = replace(settings, ddl_ruleset=ddl_rs, dml_ruleset=dml_rs, thresholds=th)

    results: list[ReviewResult] = []
    meta_cache: dict = {}       # 请求级元数据缓存（同一张表只查一次）
    for i, stmt in enumerate(statements, start=1):
        res = ReviewResult(statement_index=i, statement_type=TYPE_UNSUPPORTED,
                           sql_input=stmt, qcatalog=qcatalog, thresholds=th)
        res.extra.setdefault("in_scope", False)
        try:
            stype = classify(stmt)
            res.statement_type = stype
            res.extra["in_scope"] = stype != TYPE_UNSUPPORTED and stype != TYPE_TRANSACTION
            res.ruleset = dml_rs if stype in DML_TYPES else ddl_rs
            if stype == TYPE_CREATE_TABLE:
                review_create(stmt, res, ctx_settings, env, database, meta_cache)
            elif stype == TYPE_ALTER_TABLE:
                review_alter(stmt, res, ctx_settings, env, database, meta_cache)
            elif stype == TYPE_CREATE_INDEX:
                review_create_index(stmt, res, ctx_settings, env, database, meta_cache)
            elif stype == TYPE_DROP_INDEX:
                review_drop_index(stmt, res, ctx_settings, env, database, meta_cache)
            elif stype in (TYPE_DROP_TABLE, TYPE_TRUNCATE, TYPE_DROP_DATABASE):
                review_destructive(stmt, res, stype)
            elif stype in DML_TYPES:
                review_dml_stmt(stmt, res, ctx_settings, env, database, meta_cache, dml_warn)
            elif stype == TYPE_TRANSACTION:
                res.add("SC-00", INFO, "语句",
                        "事务控制语句，不单独判定（是否包裹由批次级规则 DM-10 判定）",
                        "无需处理")
            else:
                res.extra["in_scope"] = False
                res.add("SC-00", INFO, "语句",
                        "本服务只审 DDL；该语句不在审核范围",
                        "SELECT 等查询请走 dba-mcp；DML（INSERT / UPDATE / DELETE / "
                        "REPLACE）请走 SQL Review DML 服务")
        except ParseError as exc:
            res.add("PARSE-01", UNKNOWN, "语句", f"语句解析失败：{exc}",
                    "请确认语法；复杂语法建议改用等价的标准写法")
            res.fatal = res.fatal or f"解析失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            res.add("ENGINE-01", UNKNOWN, "语句", f"审核异常：{type(exc).__name__}: {exc}",
                    "请把这条语句和报错发给 DBA")
            res.fatal = res.fatal or f"审核异常：{exc}"
        results.append(res)

    _review_batch(results, statements, dml_rs)
    _merge_same_table_alters(results)

    for res in results:
        if res.extra.get("merged_into"):
            res.sql_final = ""
            continue
        if res.fatal:
            res.sql_final = res.sql_input
            continue
        fixed = any(f.auto_fixed for f in res.findings)
        if res.table is not None and fixed:
            try:
                res.sql_final = render_table(res.table)
                continue
            except Exception:  # noqa: BLE001
                pass
        if res.alter is not None and fixed:
            try:
                res.sql_final = render_alter(res.alter)
                continue
            except Exception:  # noqa: BLE001
                pass
        if res.extra.get("create_index") and fixed:
            try:
                ci = res.extra["create_index"]
                res.sql_final = render_create_index(ci["index"], ci["db"], ci["table"])
                continue
            except Exception:  # noqa: BLE001
                pass
        res.sql_final = res.sql_input

    return _assemble(raw, results, settings, env, database, started,
                     ddl_rs, ddl_warn, dml_rs, dml_warn)


def _merge_same_table_alters(results: list[ReviewResult]) -> None:
    """AL-02：同一张表的多条 ALTER 合并为一条（级别/文案取自 ddl.md）。"""
    groups: dict[str, list[ReviewResult]] = {}
    for res in results:
        if res.statement_type != TYPE_ALTER_TABLE or res.alter is None or res.fatal:
            continue
        key = f"{(res.alter.db or '').upper()}.{res.alter.name.upper()}"
        groups.setdefault(key, []).append(res)
    for _key, group in groups.items():
        if len(group) < 2:
            continue
        merged = merge_alters([r.alter for r in group])
        first = group[0]
        first.alter = merged
        rule = first.rule("AL-02")
        if rule is not None:
            first.emit(rule, f"表 `{merged.name}`", {"table": merged.name, "count": len(group)},
                       case="merged", auto_fixed=rule.auto_fix)
        for other in group[1:]:
            other.extra["merged_into"] = first.statement_index
            if rule is not None:
                other.emit(rule, f"表 `{merged.name}`",
                           {"table": merged.name, "index": first.statement_index},
                           case="merged_into")


def _assemble(raw: str, results: list[ReviewResult], settings: Settings,
              env: str | None, database: str | None, started: float,
              ddl_rs, ddl_warn: str, dml_rs, dml_warn: str) -> dict:
    findings = [f.as_dict() for r in results for f in r.findings]
    questions: list[dict] = []
    seen = set()
    for r in results:
        for q in r.questions:
            if q.qid in seen:
                continue
            seen.add(q.qid)
            questions.append(q.as_dict())

    verdicts = [r.verdict() for r in results]
    if "rejected" in verdicts:
        verdict = "rejected"
    elif "need_more_info" in verdicts:
        verdict = "need_more_info"
    elif "fix_required" in verdicts:
        verdict = "fix_required"
    else:
        verdict = "pass"

    in_scope_count = sum(1 for r in results if r.extra.get("in_scope", True))
    in_scope_ddl = sum(1 for r in results
                       if r.extra.get("in_scope") and r.statement_type in DDL_TYPES)
    in_scope_dml = sum(1 for r in results
                       if r.extra.get("in_scope") and r.statement_type in DML_TYPES)
    skipped = len(results) - in_scope_count
    if results and in_scope_count == 0:
        verdict = "rejected"

    counts = {
        "error": sum(1 for f in findings if f["level"] == ERROR),
        "warn": sum(1 for f in findings if f["level"] == "warn"),
        "info": sum(1 for f in findings if f["level"] == INFO),
        "unknown": sum(1 for f in findings if f["level"] == UNKNOWN),
        "auto_fixed": sum(1 for f in findings if f["auto_fixed"]),
        "statements": len(results),
    }

    finals = [r.sql_final for r in results if r.sql_final and not r.extra.get("merged_into")]
    big_table = any(f["rule_id"] in ("SC-01", "SC-02") and f["level"] == "warn"
                    for f in findings)

    if results and in_scope_count == 0:
        summary = ("❌ 本次没有可审核的语句（本服务只收 DDL）："
                   "SELECT 类查询请走 dba-mcp；DML 请走 SQL Review DML 服务。不要按本结论提交。")
    else:
        summary = _summary(verdict, counts, questions, big_table)
        if skipped:
            summary += f"（另有 {skipped} 条非本次审核范围的语句已跳过，见 statements[].in_scope）"

    data = {
        "verdict": verdict,
        "summary": summary,
        "in_scope_count": in_scope_count,
        "in_scope_ddl_count": in_scope_ddl,
        "in_scope_dml_count": in_scope_dml,
        "skipped_statements": skipped,
        "submittable": verdict in ("pass", "fix_required"),
        "statement_count": len(results),
        "counts": counts,
        "statements": [
            {
                "index": r.statement_index,
                "type": r.statement_type,
                "type_label": STATEMENT_LABEL.get(r.statement_type, r.statement_type),
                "verdict": r.verdict(),
                "fatal": r.fatal,
                "sql_input": r.sql_input,
                "sql_final": r.sql_final,
                "in_scope": bool(r.extra.get("in_scope", True)),
                "merged_into": r.extra.get("merged_into"),
                "facts": _fact_brief(r.facts),
                "dml": r.extra.get("dml"),
                "dml_facts": r.extra.get("dml_facts"),
                "missing_facts": r.extra.get("missing_facts") or [],
                "findings": [f.as_dict() for f in r.findings],
                "questions": [q.as_dict() for q in r.questions],
            }
            for r in results
        ],
        "findings": findings,
        "questions": questions,
        "sql_final": "\n\n".join(finals),
        "big_table_warning": big_table,
        "env": env or "",
        "database": database or "",
        "standard_version": standard_version(settings),
        "standard": _rules_brief(ddl_rs, ddl_warn, "ddl"),
        "dml_rules": _rules_brief(dml_rs, dml_warn, "dml"),
        "sql_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    return {"ok": True, "data": data}


def _rules_brief(rs, warning: str, doc: str) -> dict:
    brief = spec.summary(rs, warning)
    brief["scope"] = doc
    return brief


def _fact_brief(fact: TableFact | None) -> dict:
    if fact is None:
        return {"source": "none"}
    return {
        "source": fact.source,
        "exists": fact.exists,
        "table_rows": fact.table_rows,
        "size_gb": fact.size_gb(),
        "engine": fact.engine,
        "index_count": len(fact.indexes),
        "column_count": len(fact.columns),
        "note": fact.note,
    }


def _summary(verdict: str, counts: dict, questions: list[dict], big_table: bool) -> str:
    if verdict == "pass":
        extra = "；另有建议项可酌情优化" if counts["warn"] else ""
        return f"✅ 全部符合规范，可以提交（共 {counts['statements']} 条语句）{extra}。"
    if verdict == "fix_required":
        return (f"⚠️ 已自动改写 {counts['auto_fixed']} 处，改写后的语句可直接提交；"
                f"❌{counts['error']} ⚠️{counts['warn']} ⏸{counts['unknown']}。")
    if verdict == "rejected":
        return (f"❌ 存在不可自动修复的违规（{counts['error']} 项），本单不通过；"
                "请按 findings 整改后重新提交。")
    blocking = [q for q in questions if q.get("blocking")]
    parts = [f"已自动改写 {counts['auto_fixed']} 处" if counts["auto_fixed"] else ""]
    if counts["error"]:
        parts.append(f"❌{counts['error']} 项须整改")
    if counts["unknown"]:
        parts.append(f"⏸{counts['unknown']} 项未判定（缺事实信息）")
    if blocking:
        parts.append("需你确认 " + "、".join(q["id"].replace("Q-", "") for q in blocking))
    elif questions:
        parts.append(f"另有 {len(questions)} 条补充建议")
    return "⏸ " + "；".join([p for p in parts if p]) + "。"


def _err(code: str, message: str, reason: str = "", detail: dict | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message,
                                  "reason": reason or code, "detail": detail or {}}}
