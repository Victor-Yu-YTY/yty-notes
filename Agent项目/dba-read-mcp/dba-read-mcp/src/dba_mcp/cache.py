"""元数据与查询结果缓存（SQLite，标准库，零外部依赖）。

策略：
  - 元数据（库/表/列/索引）：命中且未过期直接返回；否则回源 information_schema 并回写。
  - 查询结果：默认不缓存，调用方显式开启时才写，避免研发看到过期数据。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_database (
  env            TEXT NOT NULL,
  schema_name    TEXT NOT NULL,
  charset        TEXT,
  collation      TEXT,
  source_instance TEXT,
  collected_at   TEXT NOT NULL,
  PRIMARY KEY (env, schema_name)
);
CREATE TABLE IF NOT EXISTS meta_table (
  env            TEXT NOT NULL,
  schema_name    TEXT NOT NULL,
  table_name     TEXT NOT NULL,
  table_comment  TEXT,
  engine         TEXT,
  table_collation TEXT,
  collected_at   TEXT NOT NULL,
  PRIMARY KEY (env, schema_name, table_name)
);
CREATE TABLE IF NOT EXISTS meta_column (
  env             TEXT NOT NULL,
  schema_name     TEXT NOT NULL,
  table_name      TEXT NOT NULL,
  column_name     TEXT NOT NULL,
  ordinal_position INTEGER,
  column_type     TEXT,
  is_nullable     TEXT,
  column_default  TEXT,
  column_key      TEXT,
  extra           TEXT,
  column_comment  TEXT,
  collation_name  TEXT,
  source_instance TEXT,
  collected_at    TEXT NOT NULL,
  PRIMARY KEY (env, schema_name, table_name, column_name)
);
CREATE TABLE IF NOT EXISTS meta_index (
  env          TEXT NOT NULL,
  schema_name  TEXT NOT NULL,
  table_name   TEXT NOT NULL,
  index_name   TEXT NOT NULL,
  seq_in_index INTEGER,
  column_name  TEXT,
  non_unique   INTEGER,
  index_type   TEXT,
  collected_at TEXT NOT NULL,
  PRIMARY KEY (env, schema_name, table_name, index_name, seq_in_index)
);
CREATE TABLE IF NOT EXISTS query_cache (
  cache_key   TEXT PRIMARY KEY,
  env         TEXT NOT NULL,
  sql_text    TEXT NOT NULL,
  payload     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_snapshot (
  env          TEXT NOT NULL,
  schema_name  TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  table_count  INTEGER,
  elapsed_ms   INTEGER,
  PRIMARY KEY (env, schema_name)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


class MetadataCache:
    def __init__(self, path: str, metadata_ttl: int = 3600, query_ttl: int = 300) -> None:
        self._path = path
        self._metadata_ttl = metadata_ttl
        self._query_ttl = query_ttl
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- 新鲜度 ----------
    def _fresh(self, collected_at: str) -> bool:
        dt = _parse_dt(collected_at or "")
        if dt is None:
            return False
        return datetime.now(timezone.utc) - dt < timedelta(seconds=self._metadata_ttl)

    # ---------- databases ----------
    def get_databases(self, env: str) -> list[dict] | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT schema_name, charset, collation, source_instance, collected_at "
                "FROM meta_database WHERE env = ? ORDER BY schema_name",
                (env,),
            ).fetchall()
        if not rows:
            return None
        if not all(self._fresh(r["collected_at"]) for r in rows):
            return None
        return [
            {
                "database": r["schema_name"],
                "charset": r["charset"],
                "collation": r["collation"],
                "source_instance": r["source_instance"],
            }
            for r in rows
        ]

    def put_databases(self, env: str, rows: Iterable[dict], source_instance_map: dict[str, str]) -> None:
        ts = _now()
        payload = [
            (
                env,
                r["database"],
                r.get("charset"),
                r.get("collation"),
                source_instance_map.get(r["database"], ""),
                ts,
            )
            for r in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta_database "
                "(env, schema_name, charset, collation, source_instance, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )

    # ---------- tables ----------
    def get_tables(self, env: str, database: str) -> list[dict] | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_name, engine, table_collation, table_comment, collected_at "
                "FROM meta_table WHERE env = ? AND schema_name = ? ORDER BY table_name",
                (env, database),
            ).fetchall()
        if not rows:
            return None
        if not all(self._fresh(r["collected_at"]) for r in rows):
            return None
        return [
            {
                "table": r["table_name"],
                "engine": r["engine"],
                "collation": r["table_collation"],
                "comment": r["table_comment"],
            }
            for r in rows
        ]

    def put_tables(self, env: str, database: str, rows: Iterable[dict]) -> None:
        ts = _now()
        payload = [
            (env, database, r["table"], r.get("engine"), r.get("collation"), r.get("comment"), ts)
            for r in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta_table "
                "(env, schema_name, table_name, table_comment, engine, table_collation, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )

    # ---------- columns ----------
    def get_columns(self, env: str, database: str, table: str) -> list[dict] | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT column_name, ordinal_position, column_type, is_nullable, column_default, "
                "column_key, extra, column_comment, collation_name, source_instance, collected_at "
                "FROM meta_column WHERE env = ? AND schema_name = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                (env, database, table),
            ).fetchall()
        if not rows:
            return None
        if not all(self._fresh(r["collected_at"]) for r in rows):
            return None
        # 对外列名与 metadata.fetch_columns 保持一致
        return [
            {
                "pos": r["ordinal_position"],
                "name": r["column_name"],
                "type": r["column_type"],
                "nullable": r["is_nullable"],
                "default": r["column_default"],
                "key": r["column_key"],
                "extra": r["extra"],
                "comment": r["column_comment"],
                "collation": r["collation_name"],
            }
            for r in rows
        ]

    def put_columns(
        self, env: str, database: str, table: str, rows: Iterable[dict], source_instance: str = ""
    ) -> None:
        ts = _now()
        payload = [
            (
                env,
                database,
                table,
                r["name"],
                r.get("pos"),
                r.get("type"),
                r.get("nullable"),
                r.get("default"),
                r.get("key"),
                r.get("extra"),
                r.get("comment"),
                r.get("collation"),
                source_instance,
                ts,
            )
            for r in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta_column "
                "(env, schema_name, table_name, column_name, ordinal_position, column_type, "
                "is_nullable, column_default, column_key, extra, column_comment, collation_name, "
                "source_instance, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )

    # ---------- indexes ----------
    def get_indexes(self, env: str, database: str, table: str) -> list[dict] | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT index_name, seq_in_index, column_name, non_unique, index_type, collected_at "
                "FROM meta_index WHERE env = ? AND schema_name = ? AND table_name = ? "
                "ORDER BY index_name, seq_in_index",
                (env, database, table),
            ).fetchall()
        if not rows:
            return None
        if not all(self._fresh(r["collected_at"]) for r in rows):
            return None
        return [
            {
                "index_name": r["index_name"],
                "seq_in_index": r["seq_in_index"],
                "column_name": r["column_name"],
                "non_unique": r["non_unique"],
                "index_type": r["index_type"],
            }
            for r in rows
        ]

    def put_indexes(self, env: str, database: str, table: str, rows: Iterable[dict]) -> None:
        ts = _now()
        payload = [
            (
                env,
                database,
                table,
                r["index_name"],
                r.get("seq_in_index"),
                r.get("column_name"),
                r.get("non_unique"),
                r.get("index_type"),
                ts,
            )
            for r in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta_index "
                "(env, schema_name, table_name, index_name, seq_in_index, column_name, "
                "non_unique, index_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )

    # ---------- 查询结果缓存 ----------
    def get_query(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM query_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        dt = _parse_dt(row["created_at"] or "")
        if dt is None or datetime.now(timezone.utc) - dt >= timedelta(seconds=self._query_ttl):
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return None

    def put_query(self, key: str, env: str, sql: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO query_cache (cache_key, env, sql_text, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, env, sql, json.dumps(payload, ensure_ascii=False, default=str), _now()),
            )

    # ---------- 维护 ----------
    def purge(self, env: str | None = None, database: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            if env is None:
                for table in ("meta_database", "meta_table", "meta_column", "meta_index", "query_cache"):
                    conn.execute(f"DELETE FROM {table}")
                return
            if database is None:
                for table in ("meta_database", "meta_table", "meta_column", "meta_index"):
                    conn.execute(f"DELETE FROM {table} WHERE env = ?", (env,))
                conn.execute("DELETE FROM query_cache WHERE env = ?", (env,))
                return
            for table in ("meta_table", "meta_column", "meta_index"):
                conn.execute(f"DELETE FROM {table} WHERE env = ? AND schema_name = ?", (env, database))
            conn.execute("DELETE FROM meta_database WHERE env = ? AND schema_name = ?", (env, database))

    def record_snapshot(self, env: str, database: str, table_count: int, elapsed_ms: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta_snapshot "
                "(env, schema_name, collected_at, table_count, elapsed_ms) VALUES (?, ?, ?, ?, ?)",
                (env, database, _now(), table_count, elapsed_ms),
            )
