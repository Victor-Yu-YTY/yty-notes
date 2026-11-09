"""DML 解析：把 INSERT / UPDATE / DELETE / REPLACE 解析成结构化事实。

设计边界
--------
只做**词法级结构识别**，不构建完整表达式树：
  * 语义判定（WHERE 是否命中索引、列名是否存在）交给 dmfacts 结合表事实做；
  * 抽不准的地方一律置 ``where_complex=True`` / ``unsure=True``，由规则层标 ⏸，绝不猜。

这样才能保证「规则写在 YAML 里」成立——解析器只负责产出事实，不掺任何合规判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import Token, find_matching, split_top, tokenize

# 非确定函数：写库后无法复现，主从/回放结果可能不一致
NONDETERMINISTIC = {
    "NOW", "CURDATE", "CURTIME", "SYSDATE", "RAND", "UUID", "UUID_SHORT",
    "CONNECTION_ID", "LAST_INSERT_ID",
}

# WHERE 里可能出现的比较操作符（用于识别「列 op 值」形态）
_COMPARE_OPS = {
    "=", "<", ">", "<=", ">=", "!=", "<>", "<=>",
    "LIKE", "NOT", "IN", "IS", "BETWEEN", "REGEXP", "RLIKE",
}

# 表引用后可能跟的关键字，用来判断表引用是否结束
_REF_STOP = {
    "SET", "WHERE", "ORDER", "LIMIT", "VALUES", "VALUE", "SELECT", "ON",
    "USING", "JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "STRAIGHT_JOIN",
    "NATURAL", "OUTER", "GROUP", "HAVING", "PARTITION", "AS",
}

_JOIN_WORDS = {"JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "STRAIGHT_JOIN", "NATURAL", "OUTER"}

# 出现在 "(" 前面的这些词不是函数名（否则 IN (SELECT ...) 会被当成 func(SELECT)）
_NOT_FUNCTION = {
    "IN", "NOT", "AND", "OR", "XOR", "EXISTS", "BETWEEN", "LIKE", "IS",
    "SELECT", "FROM", "WHERE", "VALUES", "VALUE", "SET", "ON", "USING",
}

_TX_HEADS = {"BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


@dataclass
class DmlStmt:
    """一条 DML 语句解析后的事实集合（对应 standards/dml.md 里的 S 类事实）。"""

    kind: str = ""                      # insert / update / delete / replace
    raw: str = ""
    db: str = ""
    table: str = ""
    alias: str = ""
    tables: list[str] = field(default_factory=list)   # 全部表引用（含 JOIN 的右表）
    multi_table: bool = False

    has_where: bool = False
    where_columns: list[str] = field(default_factory=list)
    where_complex: bool = False         # OR / 子查询 / 函数包裹 → 谓词抽取不可靠
    where_has_id: bool = False

    has_order_by: bool = False
    has_limit: bool = False
    limit_value: int | None = None

    insert_columns: list[str] = field(default_factory=list)
    insert_rows: int | None = None      # VALUES 元组数；INSERT ... SELECT 为 None
    insert_has_null: bool = False
    insert_col_values: dict[str, list[Token]] = field(default_factory=dict)  # 首行 VALUES：列名(大写) → 值 tokens
    insert_select: bool = False
    on_duplicate_key: bool = False

    ignore_modifier: bool = False
    set_targets: list[str] = field(default_factory=list)
    set_values: dict[str, list[Token]] = field(default_factory=dict)
    has_subquery: bool = False
    subquery_target_self: bool = False
    nondeterministic_calls: list[str] = field(default_factory=list)

    func_wrapped_columns: list[str] = field(default_factory=list)   # 被函数包裹的列
    leading_like_columns: list[str] = field(default_factory=list)   # LIKE '%x'
    where_predicates: list[dict] = field(default_factory=list)      # [{col, op, kind, value}]
    unsure: bool = False                # 解析形态超出预期，事实不可靠
    note: str = ""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _is_name(tok: Token) -> bool:
    return tok.kind in ("word", "ident")


def _read_ref(toks: list[Token], i: int) -> tuple[str, str, int]:
    """读一个表引用 ``[db.]tbl``，返回 (db, table, 下一个下标)。"""
    if i >= len(toks) or not _is_name(toks[i]):
        return "", "", i
    first = toks[i].value
    i += 1
    if i + 1 < len(toks) and toks[i].is_punct(".") and _is_name(toks[i + 1]):
        return first, toks[i + 1].value, i + 2
    return "", first, i


def _last_name(toks: list[Token]) -> str:
    """取最后一个标识符（用于 ``o.TITLE`` 这类带限定符的左值）。"""
    for t in reversed(toks):
        if _is_name(t):
            return t.value
    return ""


def _index_of_kw(toks: list[Token], *words: str, start: int = 0) -> int:
    want = {w.upper() for w in words}
    for i in range(start, len(toks)):
        t = toks[i]
        if t.kind == "word" and t.value.upper() in want:
            return i
    return -1


def _top_level_ranges(toks: list[Token], seps: set[str]) -> list[int]:
    """返回深度 0 上属于分隔符集合的下标。"""
    out: list[int] = []
    depth = 0
    for i, t in enumerate(toks):
        if t.is_punct("("):
            depth += 1
        elif t.is_punct(")"):
            depth -= 1
        elif depth == 0 and t.kind == "word" and t.value.upper() in seps:
            out.append(i)
    return out


def _has_subquery(toks: list[Token]) -> bool:
    return any(t.kind == "word" and t.value.upper() == "SELECT" for t in toks)


def _nondet_calls(toks: list[Token]) -> list[str]:
    out: list[str] = []
    for i, t in enumerate(toks):
        if t.kind == "word" and t.value.upper() in NONDETERMINISTIC:
            name = t.value.upper()
            if name not in out:
                out.append(name)
    return out


def _parse_where(seg: list[Token]) -> tuple[list[str], bool, bool, list[str], list[str], list[dict]]:
    """解析 WHERE 片段。

    返回 (列名列表, 是否复杂, 是否带 ID 条件, 被函数包裹的列, 前置 LIKE 的列, 谓词列表)。
    只认「列 op 值」「func(列) op 值」两类形态；出现 OR / 子查询 / 无法归类即标复杂。
    """
    cols: list[str] = []
    complex_ = False
    has_id = False
    func_cols: list[str] = []
    lead_like: list[str] = []
    preds: list[dict] = []

    if not seg:
        return cols, True, False, func_cols, lead_like, preds

    if _has_subquery(seg):
        complex_ = True
    for t in seg:
        if t.kind == "word" and t.value.upper() in ("OR", "XOR"):
            complex_ = True

    i = 0
    n = len(seg)
    while i < n:
        t = seg[i]
        # 形态一：func( 列 )
        if _is_name(t) and t.value.upper() not in _NOT_FUNCTION \
                and i + 1 < n and seg[i + 1].is_punct("("):
            inner = seg[i + 2:i + 3]
            if len(inner) == 1 and _is_name(inner[0]):
                func_cols.append(inner[0].value)
            i += 1
            continue
        # 形态二：列 op ...（含 alias.col，限定符由下面的 i+=2 跳过）
        if _is_name(t):
            nxt = seg[i + 1] if i + 1 < n else None
            is_kw = t.value.upper() in (_COMPARE_OPS | {"AND", "OR", "NOT", "NULL", "TRUE", "FALSE"})
            if not is_kw and nxt is not None and nxt.is_punct("."):
                i += 2          # alias 或 库名，跳过限定符，下一轮即列名
                continue
            if not is_kw and nxt is not None and (
                    nxt.kind == "punct" and nxt.value in "=<>!"
                    or nxt.kind == "word" and nxt.value.upper() in _COMPARE_OPS):
                name = t.value
                if name not in cols:
                    cols.append(name)
                up = name.upper()
                if up == "ID" or up.endswith("_ID"):
                    has_id = True
                # 记录谓词（隐式转换判定要用字面量类型）
                op_tok = seg[i + 1]
                op = op_tok.value.upper()
                val = seg[i + 2] if i + 2 < n else None
                if op in ("IS", "BETWEEN") or val is None:
                    preds.append({"col": name, "op": op, "kind": "", "value": ""})
                else:
                    preds.append({
                        "col": name, "op": op,
                        "kind": {"number": "number", "string": "string"}.get(val.kind, "ident"),
                        "value": val.value,
                    })
                # LIKE '%...' 前置通配
                if op == "LIKE" and val is not None and val.kind == "string" \
                        and val.value.startswith("%"):
                    lead_like.append(name)
            i += 1
            continue
        i += 1

    if not cols and not func_cols:
        complex_ = True         # 抽不出任何谓词列 → 判定不可靠
    return cols, complex_, has_id, func_cols, lead_like, preds


def _split_by_where(toks: list[Token]) -> tuple[list[Token], list[Token]]:
    """按第一个深度 0 的 WHERE 切成 (before, after)。"""
    idx = -1
    depth = 0
    for i, t in enumerate(toks):
        if t.is_punct("("):
            depth += 1
        elif t.is_punct(")"):
            depth -= 1
        elif depth == 0 and t.kind == "word" and t.value.upper() == "WHERE":
            idx = i
            break
    if idx < 0:
        return toks, []
    return toks[:idx], toks[idx + 1:]


def _tail_flags(toks: list[Token]) -> tuple[bool, bool, int | None, bool]:
    """识别尾部 ORDER BY / LIMIT。返回 (has_order_by, has_limit, limit_value, has_subquery)。"""
    has_order = _index_of_kw(toks, "ORDER") >= 0
    lim = _index_of_kw(toks, "LIMIT")
    has_limit = lim >= 0
    value = None
    if has_limit and lim + 1 < len(toks) and toks[lim + 1].kind == "number":
        try:
            value = int(float(toks[lim + 1].value))
        except ValueError:
            value = None
    return has_order, has_limit, value, _has_subquery(toks)


# --------------------------------------------------------------------------
# 各语句解析
# --------------------------------------------------------------------------


def _parse_insert(toks: list[Token], st: DmlStmt) -> None:
    words = [t.value.upper() for t in toks if t.kind == "word"]
    st.ignore_modifier = any(w in ("IGNORE", "LOW_PRIORITY", "HIGH_PRIORITY", "DELAYED") for w in words[:4])
    st.on_duplicate_key = "DUPLICATE" in words

    i = _index_of_kw(toks, "INTO")
    i = i + 1 if i >= 0 else 1
    db, tbl, i = _read_ref(toks, i)
    st.db, st.table = db, tbl
    st.tables = [tbl] if tbl else []

    # 显式列名
    if i < len(toks) and toks[i].is_punct("("):
        close = find_matching(toks, i)
        inner = toks[i + 1:close]
        # 括号里是 SELECT → INSERT ... SELECT 的子查询列，不是列名
        if not _has_subquery(inner):
            for part in split_top(inner):
                if part and _is_name(part[0]):
                    st.insert_columns.append(part[0].value)
        i = close + 1

    # INSERT ... SET A=1, B=2
    set_i = _index_of_kw(toks, "SET", start=i)
    if set_i >= 0 and _index_of_kw(toks, "VALUES", "VALUE", start=i) < 0:
        seg = toks[set_i + 1:]
        st.has_subquery = _has_subquery(seg)
        for part in split_top(seg):
            eq = next((k for k, t in enumerate(part) if t.is_punct("=")), -1)
            if eq > 0 and _is_name(part[0]):
                st.insert_columns.append(part[0].value)
                st.set_values[part[0].value.upper()] = part[eq + 1:]
        st.insert_rows = 1

    # VALUES (...), (...)
    v_i = _index_of_kw(toks, "VALUES", "VALUE", start=i)
    if v_i >= 0:
        rest = toks[v_i + 1:]
        groups = 0
        j = 0
        while j < len(rest):
            if rest[j].is_punct("("):
                close = find_matching(rest, j)
                groups += 1
                for t in rest[j + 1:close]:
                    if t.kind == "word" and t.value.upper() == "NULL":
                        st.insert_has_null = True
                    if t.kind == "word" and t.value.upper() in NONDETERMINISTIC:
                        if t.value.upper() not in st.nondeterministic_calls:
                            st.nondeterministic_calls.append(t.value.upper())
                # 首行按列名对齐，供「向 NOT NULL 列插 NULL」判定；
                # 未写列名时无法对齐 → 不猜，留空由规则层标 ⏸
                if groups == 1 and st.insert_columns:
                    vals = split_top(rest[j + 1:close])
                    for cname, vtok in zip(st.insert_columns, vals):
                        st.insert_col_values[cname.upper()] = vtok
                j = close + 1
                if j < len(rest) and rest[j].is_punct(","):
                    j += 1
                continue
            j += 1
        st.insert_rows = groups

    if st.insert_rows is None:
        st.insert_select = _has_subquery(toks)
        st.has_subquery = st.has_subquery or st.insert_select

    if st.on_duplicate_key:
        st.has_subquery = st.has_subquery or _has_subquery(toks)

    st.nondeterministic_calls = list(dict.fromkeys(
        st.nondeterministic_calls + _nondet_calls(toks)))


def _parse_update(toks: list[Token], st: DmlStmt) -> None:
    words = [t.value.upper() for t in toks if t.kind == "word"]
    st.ignore_modifier = any(w in ("IGNORE", "LOW_PRIORITY") for w in words[:3])

    set_i = _index_of_kw(toks, "SET")
    if set_i < 0:
        st.unsure = True
        st.note = "UPDATE 未找到 SET 子句"
        return

    refs = toks[1:set_i]
    db, tbl, ni = _read_ref(refs, 0)
    st.db, st.table = db, tbl
    names = [tbl] if tbl else []
    # 别名
    if ni < len(refs) and refs[ni].kind == "word" and refs[ni].value.upper() == "AS":
        if ni + 1 < len(refs):
            st.alias = refs[ni + 1].value
    elif ni < len(refs) and _is_name(refs[ni]) and refs[ni].value.upper() not in _REF_STOP:
        st.alias = refs[ni].value
    # JOIN / 逗号 → 多表
    multi = any(t.is_punct(",") for t in refs) or any(
        t.kind == "word" and t.value.upper() in _JOIN_WORDS for t in refs)
    if multi:
        st.multi_table = True
        j = 0
        while j < len(refs):
            if refs[j].kind == "word" and refs[j].value.upper() in _JOIN_WORDS:
                _d, other, nj = _read_ref(refs, j + 1)
                if other:
                    names.append(other)
                j = max(nj, j + 1)
                continue
            j += 1
    st.tables = [n for n in dict.fromkeys(names) if n]

    set_seg = toks[set_i + 1:]
    before_where, after_where = _split_by_where(set_seg)
    st.has_where = bool(after_where)
    st.has_subquery = _has_subquery(set_seg)

    for part in split_top(before_where):
        eq = next((k for k, t in enumerate(part) if t.is_punct("=")), -1)
        if eq > 0:
            col = _last_name(part[:eq])      # 兼容 o.TITLE / `TITLE`
            if col:
                st.set_targets.append(col)
                st.set_values[col.upper()] = part[eq + 1:]

    if after_where:
        cols, cx, has_id, func_cols, lead_like, preds = _parse_where(after_where)
        st.where_columns = cols
        st.where_complex = cx
        st.where_has_id = has_id
        st.func_wrapped_columns = func_cols
        st.leading_like_columns = lead_like
        st.where_predicates = preds

    # 无 WHERE 时 ORDER BY / LIMIT 仍在 SET 段里，不能因为 after_where 为空就漏判
    st.has_order_by, st.has_limit, st.limit_value, _sub = _tail_flags(
        after_where if after_where else before_where)
    st.subquery_target_self = _self_reference(set_seg, st.table)
    st.nondeterministic_calls = _nondet_calls(set_seg)


def _parse_delete(toks: list[Token], st: DmlStmt) -> None:
    words = [t.value.upper() for t in toks if t.kind == "word"]
    st.ignore_modifier = any(w in ("IGNORE", "LOW_PRIORITY", "QUICK") for w in words[:4])

    # DELETE a FROM t a JOIN ...  /  DELETE FROM t WHERE ...
    from_i = _index_of_kw(toks, "FROM")
    if from_i < 0:
        st.unsure = True
        st.note = "DELETE 未找到 FROM 子句"
        return
    head_refs = toks[1:from_i]
    db, tbl, ni = _read_ref(toks, from_i + 1)
    st.db, st.table = db, tbl
    names = [tbl] if tbl else []

    refs = toks[from_i + 1:]
    multi = any(t.is_punct(",") for t in refs) or any(
        t.kind == "word" and t.value.upper() in _JOIN_WORDS for t in refs)
    if multi:
        st.multi_table = True
        j = 0
        while j < len(refs):
            if refs[j].kind == "word" and refs[j].value.upper() in _JOIN_WORDS:
                _d, other, nj = _read_ref(refs, j + 1)
                if other:
                    names.append(other)
                j = max(nj, j + 1)
                continue
            j += 1
    st.tables = [n for n in dict.fromkeys(names) if n]

    if head_refs:
        st.multi_table = True           # DELETE t1 FROM ... 形式
    if ni < len(refs) and refs[ni].kind == "word" and refs[ni].value.upper() == "AS":
        if ni + 1 < len(refs):
            st.alias = refs[ni + 1].value
    elif ni < len(refs) and _is_name(refs[ni]) and refs[ni].value.upper() not in _REF_STOP:
        st.alias = refs[ni].value

    before_where, after_where = _split_by_where(refs)
    st.has_where = bool(after_where)
    st.has_subquery = _has_subquery(refs)
    if after_where:
        cols, cx, has_id, func_cols, lead_like, preds = _parse_where(after_where)
        st.where_columns = cols
        st.where_complex = cx
        st.where_has_id = has_id
        st.func_wrapped_columns = func_cols
        st.leading_like_columns = lead_like
        st.where_predicates = preds
    st.has_order_by, st.has_limit, st.limit_value, _sub = _tail_flags(
        after_where if after_where else before_where)
    st.subquery_target_self = _self_reference(refs, st.table)


def _self_reference(toks: list[Token], table: str) -> bool:
    """子查询里是否引用了被更新的同一张表（MySQL ERROR 1093 的诱因）。"""
    if not table:
        return False
    seg = [t for t in toks if not t.is_punct("(")]
    for i, t in enumerate(seg):
        if t.kind == "word" and t.value.upper() == "FROM" and i + 1 < len(seg):
            nxt = seg[i + 1]
            if _is_name(nxt) and nxt.value.upper() == table.upper():
                return True
    return False


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def is_transaction_control(sql: str) -> bool:
    """是否为事务控制语句（BEGIN / START TRANSACTION / COMMIT / ROLLBACK）。"""
    words = [t.value.upper() for t in tokenize(sql) if t.kind == "word"]
    if not words:
        return False
    if words[0] in ("COMMIT", "ROLLBACK", "BEGIN"):
        return True
    if words[0] == "START" and len(words) > 1 and words[1] == "TRANSACTION":
        return True
    return False


def parse_dml(sql: str, kind: str) -> DmlStmt:
    """解析一条 DML。``kind`` 为 insert / update / delete / replace。"""
    toks = tokenize(sql)
    st = DmlStmt(kind=kind, raw=sql)
    if not toks:
        st.unsure = True
        st.note = "空语句"
        return st
    try:
        if kind in ("insert", "replace"):
            _parse_insert(toks, st)
            if kind == "replace":
                st.on_duplicate_key = True     # REPLACE 语义等价于「冲突即删后插」
        elif kind == "update":
            _parse_update(toks, st)
        elif kind == "delete":
            _parse_delete(toks, st)
        else:
            st.unsure = True
            st.note = f"未支持的 DML 类型 {kind}"
    except Exception as exc:  # noqa: BLE001  解析异常不致命，标 unsure 交给规则层
        st.unsure = True
        st.note = f"解析异常：{type(exc).__name__}: {exc}"
    return st
