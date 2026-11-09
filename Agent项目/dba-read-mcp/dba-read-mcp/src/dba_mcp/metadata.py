"""information_schema 元数据查询。

所有查询均为参数化，库名 / 表名 / 列名由调用方先过 IDENT_RE 白名单校验。
"""

from __future__ import annotations

from typing import Any

SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}

_SQL_DATABASES = """
SELECT SCHEMA_NAME AS `database`,
       DEFAULT_CHARACTER_SET_NAME AS charset,
       DEFAULT_COLLATION_NAME AS collation
FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY SCHEMA_NAME
"""

_SQL_TABLES = """
SELECT TABLE_NAME AS `table`,
       ENGINE AS engine,
       TABLE_COLLATION AS collation,
       TABLE_COMMENT AS `comment`
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_NAME
"""

_SQL_COLUMNS = """
SELECT ORDINAL_POSITION AS pos,
       COLUMN_NAME      AS name,
       COLUMN_TYPE      AS type,
       IS_NULLABLE      AS nullable,
       COLUMN_DEFAULT   AS `default`,
       COLUMN_KEY       AS `key`,
       EXTRA            AS extra,
       COLUMN_COMMENT   AS `comment`,
       COLLATION_NAME   AS collation
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION
"""

_SQL_INDEXES = """
SELECT INDEX_NAME AS index_name,
       SEQ_IN_INDEX AS seq_in_index,
       COLUMN_NAME AS column_name,
       NON_UNIQUE AS non_unique,
       INDEX_TYPE AS index_type
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""

_SQL_SEARCH_COLUMN = """
SELECT TABLE_SCHEMA AS `database`,
       TABLE_NAME   AS `table`,
       COLUMN_NAME  AS name,
       COLUMN_TYPE  AS type,
       COLUMN_COMMENT AS `comment`
FROM information_schema.COLUMNS
WHERE COLUMN_NAME LIKE %s AND TABLE_SCHEMA NOT IN ('mysql', 'performance_schema', 'sys')
ORDER BY TABLE_SCHEMA, TABLE_NAME
LIMIT 500
"""


def _run(conn: Any, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def fetch_databases(conn: Any) -> list[dict]:
    return _run(conn, _SQL_DATABASES)


def fetch_tables(conn: Any, database: str) -> list[dict]:
    return _run(conn, _SQL_TABLES, (database,))


def fetch_columns(conn: Any, database: str, table: str) -> list[dict]:
    return _run(conn, _SQL_COLUMNS, (database, table))


def fetch_indexes(conn: Any, database: str, table: str) -> list[dict]:
    return _run(conn, _SQL_INDEXES, (database, table))


def fetch_create_table(conn: Any, database: str, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SHOW CREATE TABLE `{database}`.`{table}`")
        row = cur.fetchone()
    if not row:
        return ""
    values = list(row.values())
    return values[1] if len(values) > 1 else ""


def search_columns(conn: Any, column_name: str) -> list[dict]:
    return _run(conn, _SQL_SEARCH_COLUMN, (column_name,))
