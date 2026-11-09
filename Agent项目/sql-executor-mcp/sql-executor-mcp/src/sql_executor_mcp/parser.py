"""SQL 解析：把 5 类语句拆成结构化对象。

只覆盖 executor 开放的类型（CREATE TABLE / ALTER TABLE / CREATE INDEX / INSERT / UPDATE），
不做完整 SQL 语法树，够用即止。纯标准库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 语句类型

K_CREATE_TABLE = "create_table"
K_ALTER_TABLE = "alter_table"
K_CREATE_INDEX = "create_index"
K_INSERT = "insert"
K_UPDATE = "update"
K_OTHER = "other"

DDL_KINDS = {K_CREATE_TABLE, K_ALTER_TABLE, K_CREATE_INDEX}
DML_KINDS = {K_INSERT, K_UPDATE}

# ALTER 的 op 类型
OP_ADD_COLUMN = "ADD_COLUMN"
OP_MODIFY_COLUMN = "MODIFY_COLUMN"
OP_CHANGE_COLUMN = "CHANGE_COLUMN"
OP_DROP_COLUMN = "DROP_COLUMN"
OP_ADD_INDEX = "ADD_INDEX"
OP_ADD_PRIMARY = "ADD_PRIMARY"
OP_DROP_INDEX = "DROP_INDEX"
OP_RENAME_TABLE = "RENAME_TABLE"
OP_TABLE_OPTION = "TABLE_OPTION"
OP_OTHER = "OTHER"


@dataclass
class ColumnDef:
    name: str
    type_sql: str = ""
    nullable: bool = True
    has_default: bool = False
    auto_increment: bool = False
    inline_primary: bool = False
    raw: str = ""


@dataclass
class AlterOp:
    kind: str
    column: str | None = None
    new_name: str | None = None
    index_name: str | None = None
    columns: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class Statement:
    index: int
    raw: str
    kind: str = K_OTHER
    database: str | None = None
    table: str | None = None
    # CREATE TABLE
    columns: list[ColumnDef] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    engine: str | None = None
    # ALTER TABLE
    ops: list[AlterOp] = field(default_factory=list)
    # CREATE INDEX
    index_name: str | None = None
    index_columns: list[str] = field(default_factory=list)
    # INSERT
    insert_columns: list[str] = field(default_factory=list)
    insert_rows: int | None = None
    insert_is_select: bool = False
    insert_ignore: bool = False
    on_duplicate_key: bool = False
    # UPDATE
    set_columns: list[str] = field(default_factory=list)
    has_where: bool = False
    where_sql: str | None = None
    where_full: str | None = None      # WHERE 之后到语句末尾（含 ORDER BY / LIMIT），供 before-image 复用
    limit_value: int | None = None
    multi_table: bool = False

    @property
    def qualified(self) -> str:
        if self.database and self.table:
            return f"{self.database}.{self.table}"
        return self.table or ""


@dataclass
class ParseResult:
    statements: list[Statement] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<btick>`(?:[^`]|``)*`)
    | (?P<str>'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*")
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<word>[A-Za-z_$\u4e00-\u9fa5][A-Za-z0-9_$\u4e00-\u9fa5]*)
    | (?P<punct>[(),;.=<>!+\-*/%~^&|])
    """,
    re.X | re.S,
)


@dataclass
class Token:
    kind: str
    value: str
    pos: int

    @property
    def up(self) -> str:
        return self.value.upper()


def strip_comments(sql: str) -> str:
    """去掉注释，保留字符串内容。用空格替代，避免把两个词粘在一起。"""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:  # 双写转义
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """按顶层分号拆语句，跳过字符串与反引号内部。"""
    sql = strip_comments(sql)
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
        elif ch == ";":
            text = "".join(buf).strip()
            if text:
                stmts.append(text)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def tokenize(sql: str) -> list[Token]:
    toks: list[Token] = []
    for m in _TOKEN_RE.finditer(sql):
        kind = m.lastgroup or ""
        if kind == "ws":
            continue
        toks.append(Token(kind, m.group(), m.start()))
    return toks


def _unquote(tok: Token) -> str:
    v = tok.value
    if tok.kind == "btick":
        return v[1:-1].replace("``", "`")
    if tok.kind == "str":
        return v[1:-1]
    return v


def _ident(tok: Token) -> str:
    """标识符取值，保留原始大小写（MySQL 列名不区分大小写，比较时统一 upper）。"""
    return _unquote(tok)


def split_top(text: str, sep: str = ",") -> list[str]:
    """按顶层分隔符切分，跳过括号与引号内部。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    if j + 1 < n and text[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def match_paren(text: str, start: int) -> int:
    """text[start] 是 '('，返回匹配的 ')' 下标；找不到返回 -1。"""
    depth = 0
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    if j + 1 < n and text[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ---------------------------------------------------------------- 辅助


def _read_qualified(toks: list[Token], i: int) -> tuple[str | None, str | None, int]:
    """从 i 起读 [db.]tbl，返回 (db, tbl, next_i)。"""
    if i >= len(toks) or toks[i].kind not in ("word", "btick"):
        return None, None, i
    first = _ident(toks[i])
    # 跳过 MySQL 的 schema 分隔符 .
    if i + 2 < len(toks) and toks[i + 1].value == "." and toks[i + 2].kind in ("word", "btick"):
        return first, _ident(toks[i + 2]), i + 3
    return None, first, i + 1


def _find_keyword(toks: list[Token], word: str, start: int = 0) -> int:
    w = word.upper()
    for i in range(start, len(toks)):
        if toks[i].kind == "word" and toks[i].up == w:
            return i
    return -1


def _rest_text(sql: str, toks: list[Token], i: int) -> str:
    if i >= len(toks):
        return ""
    return sql[toks[i].pos:].strip()


# ---------------------------------------------------------------- 各类型解析


def _parse_column_def(text: str) -> ColumnDef | None:
    toks = tokenize(text)
    if not toks or toks[0].kind not in ("word", "btick"):
        return None
    name = _ident(toks[0])
    # 类型：从第二个 token 起到第一个属性关键字前
    attr_words = {
        "NOT", "NULL", "DEFAULT", "COMMENT", "AUTO_INCREMENT", "PRIMARY",
        "UNIQUE", "KEY", "COLLATE", "CHARACTER", "CHARSET", "ON", "GENERATED",
        "AS", "VIRTUAL", "STORED", "REFERENCES", "CHECK", "INVISIBLE", "VISIBLE",
    }
    type_parts: list[str] = []
    j = 1
    while j < len(toks) and not (toks[j].kind == "word" and toks[j].up in attr_words):
        type_parts.append(toks[j].value)
        j += 1
    type_sql = "".join(type_parts)
    upper = text.upper()
    col = ColumnDef(name=name, type_sql=type_sql, raw=text.strip())
    col.nullable = not re.search(r"\bNOT\s+NULL\b", upper)
    col.has_default = bool(re.search(r"\bDEFAULT\b", upper))
    col.auto_increment = "AUTO_INCREMENT" in upper
    col.inline_primary = bool(re.search(r"\bPRIMARY\s+KEY\b", upper))
    return col


def _is_index_def(text: str) -> bool:
    up = text.upper().lstrip()
    for prefix in ("PRIMARY KEY", "UNIQUE KEY", "UNIQUE INDEX", "KEY ", "INDEX ",
                   "FULLTEXT", "SPATIAL", "CONSTRAINT", "FOREIGN KEY"):
        if up.startswith(prefix) or up.startswith(prefix.strip() + " "):
            return True
    return False


# snapshot 模块解析 SHOW CREATE TABLE 时要判断一段是列定义还是索引定义，对外暴露
is_index_def = _is_index_def


def _index_columns(text: str) -> list[str]:
    """从索引定义里取列名，支持 (A, B(10)) 前缀索引。"""
    i = text.find("(")
    if i < 0:
        return []
    end = match_paren(text, i)
    if end < 0:
        return []
    cols: list[str] = []
    for part in split_top(text[i + 1:end]):
        toks = tokenize(part)
        if toks and toks[0].kind in ("word", "btick"):
            cols.append(_ident(toks[0]))
    return cols


def _parse_create_table(st: Statement, sql: str, toks: list[Token]) -> None:
    i = 1
    if i < len(toks) and toks[i].up == "TEMPORARY":
        i += 1
    if i < len(toks) and toks[i].up == "TABLE":
        i += 1
    # IF NOT EXISTS
    if i + 2 < len(toks) and toks[i].up == "IF" and toks[i + 1].up == "NOT" and toks[i + 2].up == "EXISTS":
        i += 3
    db, tbl, i = _read_qualified(toks, i)
    st.database, st.table = db, tbl

    if i >= len(toks) or toks[i].value != "(":
        return
    close = match_paren(sql, toks[i].pos)
    if close < 0:
        return
    body = sql[toks[i].pos + 1:close]
    for part in split_top(body):
        if not part:
            continue
        if _is_index_def(part):
            up = part.lstrip().upper()
            cols = _index_columns(part)
            if up.startswith("PRIMARY KEY"):
                st.primary_key = cols
            continue
        col = _parse_column_def(part)
        if col:
            st.columns.append(col)
            if col.inline_primary and not st.primary_key:
                st.primary_key = [col.name]

    # 表选项
    tail = sql[close + 1:]
    m = re.search(r"\bENGINE\s*=\s*([A-Za-z0-9_]+)", tail, re.I)
    if m:
        st.engine = m.group(1)


def _parse_alter_op(text: str) -> AlterOp:
    toks = tokenize(text)
    if not toks:
        return AlterOp(kind=OP_OTHER, raw=text.strip())
    head = toks[0].up

    if head == "ADD":
        i = 1
        # ADD COLUMN / ADD [UNIQUE] INDEX / ADD PRIMARY KEY / ADD KEY
        if i < len(toks) and toks[i].up in ("COLUMN",):
            i += 1
            db, name, _ = _read_qualified(toks, i)
            return AlterOp(kind=OP_ADD_COLUMN, column=name, raw=text.strip())
        if i + 1 < len(toks) and toks[i].up == "PRIMARY" and toks[i + 1].up == "KEY":
            return AlterOp(kind=OP_ADD_PRIMARY, columns=_index_columns(text), raw=text.strip())
        if i < len(toks) and toks[i].up in ("INDEX", "KEY", "UNIQUE", "FULLTEXT", "SPATIAL"):
            j = i
            while j < len(toks) and toks[j].up in ("UNIQUE", "FULLTEXT", "SPATIAL", "INDEX", "KEY"):
                j += 1
            idx_name = _ident(toks[j]) if j < len(toks) and toks[j].kind in ("word", "btick") else None
            # 省略索引名的写法：ADD INDEX (A,B)
            if j < len(toks) and toks[j].value == "(":
                idx_name = None
            return AlterOp(kind=OP_ADD_INDEX, index_name=idx_name,
                           columns=_index_columns(text), raw=text.strip())
        # ADD (col1, col2) 少见，按列处理
        if i < len(toks) and toks[i].value == "(":
            return AlterOp(kind=OP_ADD_COLUMN, raw=text.strip())
        db, name, _ = _read_qualified(toks, i)
        return AlterOp(kind=OP_ADD_COLUMN, column=name, raw=text.strip())

    if head == "MODIFY":
        i = 2 if len(toks) > 1 and toks[1].up == "COLUMN" else 1
        _, name, _ = _read_qualified(toks, i)
        return AlterOp(kind=OP_MODIFY_COLUMN, column=name, raw=text.strip())

    if head == "CHANGE":
        i = 2 if len(toks) > 1 and toks[1].up == "COLUMN" else 1
        db, old, i = _read_qualified(toks, i)
        db2, new, _ = _read_qualified(toks, i)
        return AlterOp(kind=OP_CHANGE_COLUMN, column=old, new_name=new, raw=text.strip())

    if head == "DROP":
        i = 1
        if i < len(toks) and toks[i].up == "COLUMN":
            db, name, _ = _read_qualified(toks, i + 1)
            return AlterOp(kind=OP_DROP_COLUMN, column=name, raw=text.strip())
        if i < len(toks) and toks[i].up in ("INDEX", "KEY"):
            db, name, _ = _read_qualified(toks, i + 1)
            return AlterOp(kind=OP_DROP_INDEX, index_name=name, raw=text.strip())
        if i < len(toks) and toks[i].up == "PRIMARY":
            return AlterOp(kind=OP_DROP_INDEX, index_name="PRIMARY", raw=text.strip())
        return AlterOp(kind=OP_OTHER, raw=text.strip())

    if head == "RENAME":
        return AlterOp(kind=OP_RENAME_TABLE, raw=text.strip())

    if head in ("ENGINE", "CONVERT", "DEFAULT", "CHARACTER", "CHARSET", "COLLATE",
                "AUTO_INCREMENT", "COMMENT", "ROW_FORMAT"):
        return AlterOp(kind=OP_TABLE_OPTION, raw=text.strip())

    return AlterOp(kind=OP_OTHER, raw=text.strip())


def _parse_alter_table(st: Statement, sql: str, toks: list[Token]) -> None:
    i = 2  # ALTER TABLE
    db, tbl, i = _read_qualified(toks, i)
    st.database, st.table = db, tbl
    body = _rest_text(sql, toks, i)
    for part in split_top(body):
        if part:
            st.ops.append(_parse_alter_op(part))


def _parse_create_index(st: Statement, sql: str, toks: list[Token]) -> None:
    i = 1
    while i < len(toks) and toks[i].up in ("UNIQUE", "FULLTEXT", "SPATIAL"):
        i += 1
    if i < len(toks) and toks[i].up in ("INDEX", "KEY"):
        i += 1
    if i < len(toks) and toks[i].kind in ("word", "btick"):
        st.index_name = _ident(toks[i])
        i += 1
    if i < len(toks) and toks[i].up == "ON":
        i += 1
    db, tbl, i = _read_qualified(toks, i)
    st.database, st.table = db, tbl
    st.index_columns = _index_columns(_rest_text(sql, toks, i))


def _parse_insert(st: Statement, sql: str, toks: list[Token]) -> None:
    i = 1
    if i < len(toks) and toks[i].up == "IGNORE":
        st.insert_ignore = True
        i += 1
    if i < len(toks) and toks[i].up == "INTO":
        i += 1
    db, tbl, i = _read_qualified(toks, i)
    st.database, st.table = db, tbl

    # 列名
    if i < len(toks) and toks[i].value == "(":
        close = match_paren(sql, toks[i].pos)
        if close > 0:
            st.insert_columns = [
                _ident(tokenize(p)[0])
                for p in split_top(sql[toks[i].pos + 1:close])
                if tokenize(p) and tokenize(p)[0].kind in ("word", "btick")
            ]
            i = next((k for k, t in enumerate(toks) if t.pos > close), len(toks))

    # VALUES / VALUE / SELECT / SET
    if i < len(toks) and toks[i].up in ("VALUES", "VALUE"):
        body = _rest_text(sql, toks, i + 1)
        st.insert_rows = len(split_top(body)) if body.strip() else 0
    elif i < len(toks) and toks[i].up == "SELECT":
        st.insert_is_select = True
    elif i < len(toks) and toks[i].up == "SET":
        st.insert_is_select = True  # INSERT ... SET 无法静态确定行数

    if re.search(r"\bON\s+DUPLICATE\s+KEY\b", sql, re.I):
        st.on_duplicate_key = True


def _parse_update(st: Statement, sql: str, toks: list[Token]) -> None:
    # 多表：UPDATE 之后、SET 之前出现逗号或 JOIN
    set_i = _find_keyword(toks, "SET")
    head_end = set_i if set_i > 0 else len(toks)
    if any(t.value == "," for t in toks[1:head_end]) or \
            any(t.up in ("JOIN", "INNER", "LEFT", "RIGHT") for t in toks[1:head_end]):
        st.multi_table = True

    db, tbl, i = _read_qualified(toks, 1)
    st.database, st.table = db, tbl

    if set_i < 0:
        return
    # SET 之后到 WHERE / ORDER / LIMIT 之前
    stop = len(toks)
    for kw in ("WHERE", "ORDER", "LIMIT"):
        k = _find_keyword(toks, kw, set_i + 1)
        if k > 0:
            stop = min(stop, k)
    body = sql[toks[set_i].pos + 3:toks[stop].pos] if stop < len(toks) else sql[toks[set_i].pos + 3:]
    for part in split_top(body):
        tk = tokenize(part)
        if tk and tk[0].kind in ("word", "btick"):
            st.set_columns.append(_ident(tk[0]))

    where_i = _find_keyword(toks, "WHERE")
    if where_i > 0:
        st.has_where = True
        limit_i = _find_keyword(toks, "LIMIT", where_i)
        order_i = _find_keyword(toks, "ORDER", where_i)
        end = min([x for x in (limit_i, order_i, len(toks)) if x > 0])
        st.where_sql = sql[toks[where_i].pos + 5:toks[end].pos].strip() if end < len(toks) \
            else sql[toks[where_i].pos + 5:].strip()
        # 完整尾巴：before-image 的 SELECT 必须与 UPDATE 用同一套 WHERE/ORDER/LIMIT，
        # 否则采集到的行集与真正被改的行集不一致，回滚 SQL 会漏行。
        st.where_full = sql[toks[where_i].pos + 5:].strip()

    limit_i = _find_keyword(toks, "LIMIT")
    if limit_i > 0 and limit_i + 1 < len(toks):
        try:
            st.limit_value = int(toks[limit_i + 1].value)
        except ValueError:
            st.limit_value = None


# ---------------------------------------------------------------- 入口


def classify(sql: str) -> str:
    toks = tokenize(sql)
    if not toks:
        return K_OTHER
    head = toks[0].up
    if head == "CREATE":
        # CREATE [UNIQUE|FULLTEXT|SPATIAL] INDEX / CREATE TABLE
        i = 1
        while i < len(toks) and toks[i].up in ("UNIQUE", "FULLTEXT", "SPATIAL", "TEMPORARY"):
            i += 1
        if i < len(toks):
            nxt = toks[i].up
            if nxt == "TABLE":
                return K_CREATE_TABLE
            if nxt in ("INDEX",):
                return K_CREATE_INDEX
        return K_OTHER
    if head == "ALTER":
        if len(toks) > 1 and toks[1].up == "TABLE":
            return K_ALTER_TABLE
        return K_OTHER
    if head == "INSERT":
        return K_INSERT
    if head == "UPDATE":
        return K_UPDATE
    # 已知但不开放的类型，返回真实名字，让 EX-G01 的提示可读
    if head == "DELETE":
        return "delete"
    if head == "REPLACE":
        return "replace"
    if head == "TRUNCATE":
        return "truncate"
    if head == "DROP":
        nxt = toks[1].up if len(toks) > 1 else ""
        return {"TABLE": "drop_table", "DATABASE": "drop_database",
                "INDEX": "drop_index"}.get(nxt, "drop")
    return K_OTHER


def parse_statement(sql: str, index: int, default_db: str | None = None) -> Statement:
    st = Statement(index=index, raw=sql.strip(), kind=classify(sql))
    toks = tokenize(sql)
    if st.kind == K_CREATE_TABLE:
        _parse_create_table(st, sql, toks)
    elif st.kind == K_ALTER_TABLE:
        _parse_alter_table(st, sql, toks)
    elif st.kind == K_CREATE_INDEX:
        _parse_create_index(st, sql, toks)
    elif st.kind == K_INSERT:
        _parse_insert(st, sql, toks)
    elif st.kind == K_UPDATE:
        _parse_update(st, sql, toks)
    if not st.database:
        st.database = default_db
    return st


def parse(sql: str, default_db: str | None = None) -> ParseResult:
    res = ParseResult()
    try:
        raw_stmts = split_statements(sql)
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"语句切分失败：{exc}")
        return res
    if not raw_stmts:
        res.errors.append("未识别到任何 SQL 语句")
        return res
    for i, text in enumerate(raw_stmts, start=1):
        try:
            st = parse_statement(text, i, default_db)
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"第 {i} 条解析失败：{type(exc).__name__}: {exc}")
            continue
        # 不支持的类型不在这里报错，交给 EX-G01 统一拦（提示更完整）
        res.statements.append(st)
    return res
