"""表结构快照：把 SHOW CREATE TABLE 拆成「列定义 / 索引定义」两本账。

DDL 的反向语句只能靠执行前的快照推出来：MODIFY / DROP COLUMN 之后，
原定义在库里就没了，事后再查只能拿到新定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import parser as P


@dataclass
class IndexDef:
    name: str
    raw: str            # 原样片段，可直接拼进 ALTER TABLE ... ADD <raw>
    primary: bool = False


@dataclass
class TableSnapshot:
    database: str = ""
    table: str = ""
    create_sql: str = ""
    columns: dict[str, str] = field(default_factory=dict)          # UPPER(name) -> 原始列定义
    column_names: dict[str, str] = field(default_factory=dict)     # UPPER(name) -> 实际列名
    indexes: dict[str, IndexDef] = field(default_factory=dict)     # UPPER(name) -> 索引定义

    def column(self, name: str | None) -> str | None:
        return self.columns.get((name or "").upper())

    def column_name(self, name: str | None) -> str | None:
        return self.column_names.get((name or "").upper())

    def index(self, name: str | None) -> IndexDef | None:
        return self.indexes.get((name or "").upper())

    def as_dict(self) -> dict:
        return {
            "database": self.database,
            "table": self.table,
            "columns": [self.column_names[k] for k in self.columns],
            "indexes": list(self.indexes.keys()),
        }


def _index_name(part: str) -> str | None:
    toks = P.tokenize(part)
    if not toks:
        return None
    ups = [t.up for t in toks]
    if ups[:2] == ["PRIMARY", "KEY"]:
        return "PRIMARY"
    i = 0
    while i < len(toks) and toks[i].up in (
            "UNIQUE", "FULLTEXT", "SPATIAL", "KEY", "INDEX", "CONSTRAINT"):
        i += 1
    if i < len(toks) and toks[i].kind in ("word", "btick") and toks[i].value != "(":
        return P._unquote(toks[i])          # noqa: SLF001 同一包内复用词法器
    return None


def from_ddl(ddl: str, database: str = "", table: str = "") -> TableSnapshot:
    """解析 SHOW CREATE TABLE 的输出。取不到结构时返回空快照，调用方按「不可回滚」处理。"""
    snap = TableSnapshot(database=database, table=table, create_sql=(ddl or "").strip())
    if not snap.create_sql:
        return snap

    open_i = snap.create_sql.find("(")
    if open_i < 0:
        return snap
    close_i = P.match_paren(snap.create_sql, open_i)
    if close_i < 0:
        return snap

    for part in P.split_top(snap.create_sql[open_i + 1:close_i]):
        part = part.strip()
        if not part:
            continue
        if P.is_index_def(part):
            name = _index_name(part)
            if name:
                snap.indexes[name.upper()] = IndexDef(
                    name=name, raw=part, primary=(name.upper() == "PRIMARY"))
            continue
        col = P._parse_column_def(part)      # noqa: SLF001
        if col:
            snap.columns[col.name.upper()] = part
            snap.column_names[col.name.upper()] = col.name
    return snap


def show_create(session, database: str, table: str) -> str | None:
    """取 SHOW CREATE TABLE 的 DDL 文本。表不存在或无权时返回 None。"""
    try:
        rows = session.query(f"SHOW CREATE TABLE `{_esc(database)}`.`{_esc(table)}`")
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        for value in row.values():
            if isinstance(value, str) and value.lstrip().upper().startswith("CREATE TABLE"):
                return value
    return None


def _esc(ident: str) -> str:
    """反引号转义。库名/表名来自调用方，不转义会拼坏语句。"""
    return str(ident).replace("`", "``")
