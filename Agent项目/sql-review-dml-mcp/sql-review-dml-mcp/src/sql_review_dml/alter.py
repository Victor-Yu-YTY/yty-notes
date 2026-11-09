"""ALTER TABLE 的模型、解析与渲染（变更表能力的核心）。

支持的子句（覆盖 dev/test/stage 常见 DDL）：
  ADD [COLUMN] coldef [FIRST|AFTER col]
  ADD {INDEX|KEY|UNIQUE|PRIMARY KEY|FULLTEXT|SPATIAL} ...
  ADD CONSTRAINT ... FOREIGN KEY ...
  MODIFY [COLUMN] coldef [FIRST|AFTER col]
  CHANGE [COLUMN] old new coldef [FIRST|AFTER col]
  ALTER [COLUMN] col SET DEFAULT x | DROP DEFAULT
  DROP [COLUMN] col | DROP INDEX name | DROP PRIMARY KEY | DROP FOREIGN KEY name
  RENAME [TO|AS] newtable | RENAME COLUMN a TO b | RENAME INDEX a TO b
  ENGINE=... / DEFAULT CHARSET=... / COMMENT='...' / CONVERT TO CHARACTER SET ...
  ALGORITHM=INPLACE / LOCK=NONE
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Column, Index, parse_column, parse_index, q, render_column, render_index
from .tokens import ParseError, Token, join_raw, split_top, tokenize

_INDEX_HEADS = {"INDEX", "KEY", "UNIQUE", "PRIMARY", "FULLTEXT", "SPATIAL", "CONSTRAINT", "FOREIGN"}
_TABLE_OPTS = {"ENGINE", "CHARSET", "CHARACTER", "COLLATE", "COMMENT", "DEFAULT", "CONVERT", "FORCE", "AUTO_INCREMENT", "ROW_FORMAT"}


@dataclass
class AlterOp:
    kind: str                      # ADD_COLUMN / MODIFY_COLUMN / CHANGE_COLUMN / ADD_INDEX /
                                   # DROP_COLUMN / DROP_INDEX / DROP_PRIMARY / ADD_PRIMARY /
                                   # RENAME_TABLE / RENAME_COLUMN / RENAME_INDEX /
                                   # ALTER_COLUMN / TABLE_OPTION / OTHER
    raw: str = ""
    column: Column | None = None
    index: Index | None = None
    name: str = ""                 # 列名/索引名/新表名
    old_name: str = ""             # CHANGE / RENAME 的旧名
    first_or_after: str = ""       # "" / FIRST / AFTER xxx（违规信息，供 AL-01 使用）
    tail: str = ""

    def where(self) -> str:
        if self.column is not None:
            return f"字段 `{self.column.name}`"
        if self.index is not None:
            return f"索引 {self.index.key_name() or ''}".strip()
        if self.name:
            return f"{self.name}"
        return self.kind


@dataclass
class AlterTable:
    name: str = ""
    db: str = ""
    ops: list[AlterOp] = field(default_factory=list)
    algorithm: str = ""
    lock: str = ""
    lock_hint: str = ""            # 原文里的 ALGORITHM/LOCK 片段（用于 AL-08）

    def table_label(self) -> str:
        return (f"{self.db}.{self.name}" if self.db else self.name)


def parse_alter_table(sql: str) -> AlterTable:
    tokens = tokenize(sql)
    if len(tokens) < 3:
        raise ParseError("语句过短，无法识别为 ALTER TABLE")
    if not tokens[0].is_kw("ALTER") or not tokens[1].is_kw("TABLE"):
        raise ParseError("不是 ALTER TABLE 语句")
    i = 2
    first = tokens[i]
    if first.kind not in ("word", "ident"):
        raise ParseError(f"表名不合法: {first.raw!r}")
    alter = AlterTable(name=first.value)
    i += 1
    if i + 1 < len(tokens) and tokens[i].is_punct("."):
        alter.db = alter.name
        alter.name = tokens[i + 1].value
        i += 2

    parts = split_top(tokens[i:])
    for part in parts:
        part = [t for t in part if not t.is_punct(";")]
        if not part:
            continue
        op = _parse_op(part, alter)
        if op is not None:
            alter.ops.append(op)
    if not alter.ops:
        raise ParseError("没有解析到任何变更子句")
    return alter


def _parse_op(tokens: list[Token], alter: AlterTable) -> AlterOp | None:
    head = tokens[0]
    raw = join_raw(tokens)

    if head.is_kw("ALGORITHM"):
        alter.algorithm = join_raw(tokens)
        alter.lock_hint = (alter.lock_hint + " " + raw).strip()
        return None
    if head.is_kw("LOCK"):
        alter.lock = join_raw(tokens)
        alter.lock_hint = (alter.lock_hint + " " + raw).strip()
        return None

    if head.is_kw("ADD"):
        rest = tokens[1:]
        if not rest:
            raise ParseError("ADD 子句缺少内容")
        if rest[0].is_kw(*_INDEX_HEADS):
            return _parse_add_index(rest, raw)
        if rest[0].is_kw("COLUMN"):
            rest = rest[1:]
        col, where = _parse_coldef(rest)
        op = AlterOp(kind="ADD_COLUMN", raw=raw, column=col, first_or_after=where)
        return op

    if head.is_kw("MODIFY"):
        rest = tokens[1:]
        if rest and rest[0].is_kw("COLUMN"):
            rest = rest[1:]
        col, where = _parse_coldef(rest)
        return AlterOp(kind="MODIFY_COLUMN", raw=raw, column=col, first_or_after=where)

    if head.is_kw("CHANGE"):
        rest = tokens[1:]
        if rest and rest[0].is_kw("COLUMN"):
            rest = rest[1:]
        if len(rest) < 2:
            raise ParseError("CHANGE 子句缺少字段名")
        old = rest[0].value
        col, where = _parse_coldef(rest[1:])
        op = AlterOp(kind="CHANGE_COLUMN", raw=raw, column=col, old_name=old, first_or_after=where)
        return op

    if head.is_kw("DROP"):
        rest = tokens[1:]
        if not rest:
            raise ParseError("DROP 子句缺少内容")
        if rest[0].is_kw("INDEX", "KEY"):
            name = rest[1].value if len(rest) > 1 else ""
            return AlterOp(kind="DROP_INDEX", raw=raw, name=name)
        if rest[0].is_kw("PRIMARY"):
            return AlterOp(kind="DROP_PRIMARY", raw=raw, name="PRIMARY")
        if rest[0].is_kw("FOREIGN"):
            name = rest[2].value if len(rest) > 2 else ""
            return AlterOp(kind="DROP_INDEX", raw=raw, name=name, tail="FOREIGN KEY")
        if rest[0].is_kw("COLUMN"):
            name = rest[1].value if len(rest) > 1 else ""
            return AlterOp(kind="DROP_COLUMN", raw=raw, name=name)
        return AlterOp(kind="DROP_COLUMN", raw=raw, name=rest[0].value)

    if head.is_kw("ALTER"):
        rest = tokens[1:]
        if rest and rest[0].is_kw("COLUMN"):
            rest = rest[1:]
        name = rest[0].value if rest else ""
        return AlterOp(kind="ALTER_COLUMN", raw=raw, name=name, tail=join_raw(rest[1:]))

    if head.is_kw("RENAME"):
        rest = tokens[1:]
        if rest and rest[0].is_kw("COLUMN"):
            old = rest[1].value if len(rest) > 1 else ""
            new = rest[3].value if len(rest) > 3 else ""
            return AlterOp(kind="RENAME_COLUMN", raw=raw, old_name=old, name=new)
        if rest and rest[0].is_kw("INDEX", "KEY"):
            old = rest[1].value if len(rest) > 1 else ""
            new = rest[3].value if len(rest) > 3 else ""
            return AlterOp(kind="RENAME_INDEX", raw=raw, old_name=old, name=new)
        if rest and rest[0].is_kw("TO", "AS"):
            rest = rest[1:]
        new = rest[0].value if rest else ""
        return AlterOp(kind="RENAME_TABLE", raw=raw, name=new)

    if head.value.upper() in _TABLE_OPTS:
        return AlterOp(kind="TABLE_OPTION", raw=raw, tail=raw)

    return AlterOp(kind="OTHER", raw=raw, tail=raw)


def _parse_add_index(rest: list[Token], raw: str) -> AlterOp:
    head = rest[0]
    if head.is_kw("CONSTRAINT", "FOREIGN"):
        idx = Index(kind="FOREIGN", raw_tail=join_raw(rest))
        return AlterOp(kind="ADD_INDEX", raw=raw, index=idx)
    if head.is_kw("PRIMARY"):
        idx = parse_index(rest, "PRIMARY")
        return AlterOp(kind="ADD_PRIMARY", raw=raw, index=idx)
    if head.is_kw("UNIQUE"):
        idx = parse_index(rest, "UNIQUE")
        return AlterOp(kind="ADD_INDEX", raw=raw, index=idx)
    if head.is_kw("FULLTEXT"):
        return AlterOp(kind="ADD_INDEX", raw=raw, index=parse_index(rest, "FULLTEXT"))
    if head.is_kw("SPATIAL"):
        return AlterOp(kind="ADD_INDEX", raw=raw, index=parse_index(rest, "SPATIAL"))
    return AlterOp(kind="ADD_INDEX", raw=raw, index=parse_index(rest, "INDEX"))


def _parse_coldef(tokens: list[Token]) -> tuple[Column, str]:
    """解析列定义，并把 FIRST / AFTER xxx 摘出来（AL-01 要报违规）。"""
    where = ""
    kept: list[Token] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.is_kw("FIRST"):
            where = "FIRST"
            i += 1
            continue
        if t.is_kw("AFTER"):
            if i + 1 < len(tokens):
                where = f"AFTER {tokens[i + 1].value}"
                i += 2
                continue
            i += 1
            continue
        kept.append(t)
        i += 1
    col = parse_column(kept)
    if col is None:
        raise ParseError("无法解析字段定义")
    return col, where


# ------------------------------- 渲染 -------------------------------


def render_op(op: AlterOp) -> str:
    if op.kind == "ADD_COLUMN":
        return "ADD COLUMN " + render_column(op.column)
    if op.kind == "MODIFY_COLUMN":
        return "MODIFY COLUMN " + render_column(op.column)
    if op.kind == "CHANGE_COLUMN":
        return f"CHANGE COLUMN {q(op.old_name)} " + render_column(op.column)
    if op.kind == "ADD_INDEX":
        return "ADD " + render_index(op.index)
    if op.kind == "ADD_PRIMARY":
        return "ADD " + render_index(op.index)
    if op.kind == "DROP_INDEX":
        if op.tail == "FOREIGN KEY":
            return f"DROP FOREIGN KEY {q(op.name)}"
        return f"DROP INDEX {q(op.name)}"
    if op.kind == "DROP_PRIMARY":
        return "DROP PRIMARY KEY"
    if op.kind == "DROP_COLUMN":
        return f"DROP COLUMN {q(op.name)}"
    if op.kind == "ALTER_COLUMN":
        return f"ALTER COLUMN {q(op.name)} {op.tail}".strip()
    if op.kind == "RENAME_TABLE":
        return f"RENAME TO {q(op.name)}"
    if op.kind == "RENAME_COLUMN":
        return f"RENAME COLUMN {q(op.old_name)} TO {q(op.name)}"
    if op.kind == "RENAME_INDEX":
        return f"RENAME INDEX {q(op.old_name)} TO {q(op.name)}"
    if op.kind == "TABLE_OPTION":
        return op.tail
    return op.raw


def render_alter(alter: AlterTable, extra_opts: list[str] | None = None) -> str:
    target = (q(alter.db) + "." if alter.db else "") + q(alter.name)
    lines = ["  " + render_op(op) for op in alter.ops if not op.kind == "OTHER"]
    for opt in (extra_opts or []):
        lines.append("  " + opt)
    if alter.algorithm:
        lines.append("  " + alter.algorithm)
    if alter.lock:
        lines.append("  " + alter.lock)
    if not lines:
        lines = ["  " + op.raw for op in alter.ops]
    return f"ALTER TABLE {target}\n" + ",\n".join(lines) + ";"


def merge_alters(alters: list[AlterTable]) -> AlterTable:
    """把同一张表的多条 ALTER 合并成一条（AL-02 自动改写）。"""
    base = alters[0]
    merged = AlterTable(name=base.name, db=base.db, algorithm=base.algorithm, lock=base.lock,
                        lock_hint=base.lock_hint)
    for a in alters:
        merged.ops.extend(a.ops)
        if not merged.algorithm and a.algorithm:
            merged.algorithm = a.algorithm
        if not merged.lock and a.lock:
            merged.lock = a.lock
    return merged
