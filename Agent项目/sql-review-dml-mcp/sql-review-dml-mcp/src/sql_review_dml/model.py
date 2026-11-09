"""CREATE TABLE 的模型、解析与渲染（建表能力的核心）。

只覆盖建表语法，不追求通用 SQL 解析：
  CREATE [TEMPORARY] TABLE [IF NOT EXISTS] [db.]name ( <col|index>, ... ) [options];
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import Token, ParseError, find_matching, join_raw, split_top, tokenize

_ATTR_KEYWORDS = {
    "NOT", "NULL", "DEFAULT", "AUTO_INCREMENT", "COMMENT", "CHARACTER", "CHARSET",
    "COLLATE", "ON", "PRIMARY", "UNIQUE", "KEY", "REFERENCES", "GENERATED", "AS",
    "VISIBLE", "INVISIBLE", "SRID", "CHECK", "CONSTRAINT", "COLUMN_FORMAT", "STORAGE",
    "FIRST", "AFTER",
}


@dataclass
class Column:
    name: str
    type: str = ""
    args: list[str] = field(default_factory=list)
    signed: str = ""
    charset: str = ""
    collate: str = ""
    nullable: bool = True
    default: str | None = None
    auto_increment: bool = False
    comment: str | None = None
    on_update: str | None = None
    inline_primary: bool = False
    inline_unique: bool = False

    def type_sql(self) -> str:
        t = self.type
        if self.args:
            t += "(" + ",".join(self.args) + ")"
        if self.signed:
            t += " " + self.signed
        return t


@dataclass
class IndexCol:
    name: str
    prefix_len: str | None = None
    order: str = ""

    def sql(self) -> str:
        s = f"`{self.name}`"
        if self.prefix_len:
            s += f"({self.prefix_len})"
        if self.order:
            s += " " + self.order
        return s


@dataclass
class Index:
    kind: str                     # PRIMARY / UNIQUE / INDEX / FULLTEXT / SPATIAL / FOREIGN
    name: str = ""
    columns: list[IndexCol] = field(default_factory=list)
    raw_tail: str = ""

    def key_name(self) -> str:
        return self.name or "PRIMARY"


@dataclass
class Table:
    name: str = ""
    db: str = ""
    temporary: bool = False
    if_not_exists: bool = False
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    engine: str = ""
    charset: str = ""
    collate: str = ""
    comment: str | None = None
    create_select: bool = False

    def col(self, name: str) -> Column | None:
        up = name.upper()
        for c in self.columns:
            if c.name.upper() == up:
                return c
        return None

    def idx(self, name: str) -> Index | None:
        up = name.upper()
        for i in self.indexes:
            if i.key_name().upper() == up:
                return i
        return None


def parse_create_table(sql: str) -> Table:
    tokens = tokenize(sql)
    if not tokens:
        raise ParseError("语句为空")
    tbl = Table()
    i = 0

    def expect(words, i):
        if i >= len(tokens) or not tokens[i].is_kw(*words):
            got = tokens[i].value if i < len(tokens) else "<结束>"
            raise ParseError(f"期望 {words[0]}，实际 {got!r}")
        return i + 1

    i = expect(["CREATE"], i)
    if i < len(tokens) and tokens[i].is_kw("TEMPORARY"):
        tbl.temporary = True
        i += 1
    i = expect(["TABLE"], i)
    if i + 2 < len(tokens) and tokens[i].is_kw("IF") and tokens[i + 1].is_kw("NOT") and tokens[i + 2].is_kw("EXISTS"):
        tbl.if_not_exists = True
        i += 3
    if i >= len(tokens):
        raise ParseError("缺少表名")
    first = tokens[i]
    if first.kind not in ("word", "ident"):
        raise ParseError(f"表名不合法: {first.raw!r}")
    tbl.name = first.value
    i += 1
    if i + 1 < len(tokens) and tokens[i].is_punct("."):
        second = tokens[i + 1]
        tbl.db = tbl.name
        tbl.name = second.value
        i += 2

    if i < len(tokens) and tokens[i].is_punct("("):
        close = find_matching(tokens, i)
        for element in split_top(tokens[i + 1:close]):
            _parse_element(element, tbl)
        i = close + 1
    else:
        rest = " ".join(t.raw for t in tokens[i:]).upper()
        if "SELECT" in rest or rest.strip().startswith("AS"):
            tbl.create_select = True
        else:
            raise ParseError("缺少列定义括号（若是 CREATE ... LIKE，本服务不支持审核）")

    tail = tokens[i:]
    tbl.create_select = tbl.create_select or any(t.is_kw("SELECT") for t in tail)
    _parse_options(tail, tbl)
    if not tbl.columns and not tbl.create_select:
        raise ParseError("没有解析到任何字段定义")
    return tbl


def _parse_options(tokens: list[Token], tbl: Table) -> None:
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.is_punct(";"):
            i += 1
            continue
        if t.is_kw("ENGINE"):
            i += 1
            if i < len(tokens) and tokens[i].is_punct("="):
                i += 1
            if i < len(tokens):
                tbl.engine = tokens[i].value
                i += 1
            continue
        if t.is_kw("DEFAULT"):
            i += 1
            continue
        if t.is_kw("CHARSET", "CHARACTER"):
            while i < len(tokens) and not tokens[i].is_punct("="):
                i += 1
            if i < len(tokens):
                i += 1
            if i < len(tokens):
                tbl.charset = tokens[i].value
                i += 1
            continue
        if t.is_kw("COLLATE"):
            i += 1
            if i < len(tokens) and tokens[i].is_punct("="):
                i += 1
            if i < len(tokens):
                tbl.collate = tokens[i].value
                i += 1
            continue
        if t.is_kw("COMMENT"):
            i += 1
            if i < len(tokens) and tokens[i].is_punct("="):
                i += 1
            if i < len(tokens):
                tbl.comment = tokens[i].value
                i += 1
            continue
        i += 1


def _parse_element(tokens: list[Token], tbl: Table) -> None:
    if not tokens:
        return
    head = tokens[0]
    if head.is_kw("PRIMARY"):
        tbl.indexes.append(parse_index(tokens, "PRIMARY"))
        return
    if head.is_kw("UNIQUE"):
        tbl.indexes.append(parse_index(tokens, "UNIQUE"))
        return
    if head.is_kw("KEY", "INDEX"):
        tbl.indexes.append(parse_index(tokens, "INDEX"))
        return
    if head.is_kw("FULLTEXT"):
        tbl.indexes.append(parse_index(tokens, "FULLTEXT"))
        return
    if head.is_kw("SPATIAL"):
        tbl.indexes.append(parse_index(tokens, "SPATIAL"))
        return
    if head.is_kw("FOREIGN", "CONSTRAINT", "CHECK"):
        idx = Index(kind="FOREIGN")
        j = 0
        while j < len(tokens) and tokens[j].is_kw("CONSTRAINT"):
            if j + 1 < len(tokens):
                idx.name = tokens[j + 1].value
                j += 2
            else:
                j += 1
        idx.raw_tail = join_raw(tokens[j:])
        tbl.indexes.append(idx)
        return
    col = parse_column(tokens)
    if col:
        tbl.columns.append(col)


def parse_index(tokens: list[Token], kind: str) -> Index:
    idx = Index(kind=kind)
    i = 0
    if kind == "PRIMARY":
        i = 2 if len(tokens) > 1 and tokens[1].is_kw("KEY") else 1
    elif kind in ("UNIQUE", "FULLTEXT", "SPATIAL"):
        i = 1
        if i < len(tokens) and tokens[i].is_kw("KEY", "INDEX"):
            i += 1
    else:
        i = 1
    if i < len(tokens) and tokens[i].kind in ("word", "ident"):
        idx.name = tokens[i].value
        i += 1
    while i < len(tokens) and not tokens[i].is_punct("("):
        i += 1
    if i < len(tokens):
        close = find_matching(tokens, i)
        for part in split_top(tokens[i + 1:close]):
            idx.columns.append(parse_index_col(part))
        i = close + 1
    idx.raw_tail = join_raw(tokens[i:]) if i < len(tokens) else ""
    return idx


def parse_index_col(tokens: list[Token]) -> IndexCol:
    if not tokens:
        return IndexCol(name="")
    col = IndexCol(name=tokens[0].value)
    i = 1
    if i < len(tokens) and tokens[i].is_punct("("):
        if i + 1 < len(tokens):
            col.prefix_len = tokens[i + 1].value
        i += 3
    while i < len(tokens):
        if tokens[i].is_kw("ASC", "DESC"):
            col.order = tokens[i].value.upper()
        i += 1
    return col


def parse_column(tokens: list[Token]) -> Column | None:
    if not tokens or tokens[0].kind not in ("word", "ident"):
        return None
    col = Column(name=tokens[0].value)
    i = 1
    if i >= len(tokens):
        return None
    typ = tokens[i].value.upper()
    if typ in ("DOUBLE", "REAL") and i + 1 < len(tokens) and tokens[i + 1].is_kw("PRECISION"):
        typ = typ + " PRECISION"
        i += 1
    col.type = typ
    i += 1
    if i < len(tokens) and tokens[i].is_punct("("):
        close = find_matching(tokens, i)
        args, cur = [], []
        for t in tokens[i + 1:close]:
            if t.is_punct(","):
                args.append("".join(x.raw for x in cur))
                cur = []
            else:
                cur.append(t)
        if cur:
            args.append("".join(x.raw for x in cur))
        col.args = args
        i = close + 1
    while i < len(tokens) and tokens[i].is_kw("UNSIGNED", "ZEROFILL", "SIGNED"):
        if tokens[i].value.upper() != "SIGNED":
            col.signed = tokens[i].value.upper()
        i += 1
    return parse_column_attrs(col, tokens, i)


def parse_column_attrs(col: Column, tokens: list[Token], i: int) -> Column:
    while i < len(tokens):
        t = tokens[i]
        if t.is_kw("NOT"):
            if i + 1 < len(tokens) and tokens[i + 1].is_kw("NULL"):
                col.nullable = False
                i += 2
            else:
                i += 1
            continue
        if t.is_kw("NULL"):
            col.nullable = True
            i += 1
            continue
        if t.is_kw("DEFAULT"):
            i += 1
            if i < len(tokens) and tokens[i].is_kw("NULL"):
                col.default = "NULL"
                i += 1
                continue
            start = i
            depth = 0
            while i < len(tokens):
                tt = tokens[i]
                if tt.is_punct("("):
                    depth += 1
                elif tt.is_punct(")"):
                    depth -= 1
                if depth == 0 and tt.kind == "word" and tt.value.upper() in _ATTR_KEYWORDS:
                    break
                if depth == 0 and tt.is_punct(","):
                    break
                i += 1
            col.default = join_raw(tokens[start:i]).strip() or None
            continue
        if t.is_kw("AUTO_INCREMENT"):
            col.auto_increment = True
            i += 1
            continue
        if t.is_kw("COMMENT"):
            i += 1
            if i < len(tokens) and tokens[i].kind == "string":
                col.comment = tokens[i].value
                i += 1
            continue
        if t.is_kw("CHARACTER"):
            i += 2
            if i < len(tokens):
                col.charset = tokens[i].value
                i += 1
            continue
        if t.is_kw("CHARSET"):
            i += 1
            if i < len(tokens) and tokens[i].is_punct("="):
                i += 1
            if i < len(tokens):
                col.charset = tokens[i].value
                i += 1
            continue
        if t.is_kw("COLLATE"):
            i += 1
            if i < len(tokens) and tokens[i].is_punct("="):
                i += 1
            if i < len(tokens):
                col.collate = tokens[i].value
                i += 1
            continue
        if t.is_kw("ON") and i + 1 < len(tokens) and tokens[i + 1].is_kw("UPDATE"):
            i += 2
            start = i
            while i < len(tokens) and not (tokens[i].kind == "word" and tokens[i].value.upper() in _ATTR_KEYWORDS):
                i += 1
            col.on_update = join_raw(tokens[start:i]).strip()
            continue
        if t.is_kw("PRIMARY"):
            col.inline_primary = True
            i += 1
            if i < len(tokens) and tokens[i].is_kw("KEY"):
                i += 1
            continue
        if t.is_kw("UNIQUE"):
            col.inline_unique = True
            i += 1
            if i < len(tokens) and tokens[i].is_kw("KEY"):
                i += 1
            continue
        i += 1
    return col


# ------------------------------- 渲染 -------------------------------


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def render_column(col: Column) -> str:
    parts = [q(col.name), col.type_sql()]
    if col.charset:
        parts.append(f"CHARACTER SET {col.charset}")
    if col.collate:
        parts.append(f"COLLATE {col.collate}")
    parts.append("NULL" if col.nullable else "NOT NULL")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    if col.on_update:
        parts.append(f"ON UPDATE {col.on_update}")
    if col.auto_increment:
        parts.append("AUTO_INCREMENT")
    if col.comment is not None:
        parts.append("COMMENT '" + col.comment.replace("'", "''") + "'")
    return " ".join(parts)


def render_index(idx: Index) -> str:
    cols = ",".join(c.sql() for c in idx.columns)
    if idx.kind == "PRIMARY":
        return f"PRIMARY KEY ({cols})"
    if idx.kind == "UNIQUE":
        return f"UNIQUE KEY {q(idx.name)} ({cols})"
    if idx.kind == "INDEX":
        return f"KEY {q(idx.name)} ({cols})"
    if idx.kind in ("FULLTEXT", "SPATIAL"):
        return f"{idx.kind} KEY {q(idx.name)} ({cols})"
    tail = (" " + idx.raw_tail) if idx.raw_tail else ""
    prefix = "CONSTRAINT " + q(idx.name) + " " if idx.name else ""
    return prefix + tail.strip()


def render_table(tbl: Table) -> str:
    lines = ["  " + render_column(c) for c in tbl.columns]
    lines += ["  " + render_index(i) for i in tbl.indexes]
    head = "CREATE TABLE "
    if tbl.if_not_exists:
        head += "IF NOT EXISTS "
    head += (q(tbl.db) + "." if tbl.db else "") + q(tbl.name)
    opts = [f"ENGINE={tbl.engine or 'InnoDB'}", f"DEFAULT CHARSET={tbl.charset or 'utf8mb4'}"]
    if tbl.collate:
        opts.append(f"COLLATE={tbl.collate}")
    if tbl.comment is not None:
        opts.append("COMMENT='" + tbl.comment.replace("'", "''") + "'")
    return f"{head} (\n" + ",\n".join(lines) + "\n) " + " ".join(opts) + ";"


def render_create_index(idx: Index, db: str = "", table: str = "") -> str:
    """把（可能被改写的）索引对象渲染回 CREATE INDEX 语句。

    用于 v1.2.1 起 review_index_change 的 sql_final：
    索引名被规范化成 IDX_/UNIQ_、长 VARCHAR 加了前缀长度之后，这里给出可直接提交的语句。
    """
    cols = ",".join(c.sql() for c in idx.columns)
    prefix = {"UNIQUE": "UNIQUE ", "FULLTEXT": "FULLTEXT ", "SPATIAL": "SPATIAL "}.get(idx.kind, "")
    target = (q(db) + "." if db else "") + q(table)
    return f"CREATE {prefix}INDEX {q(idx.name)} ON {target} ({cols});"
