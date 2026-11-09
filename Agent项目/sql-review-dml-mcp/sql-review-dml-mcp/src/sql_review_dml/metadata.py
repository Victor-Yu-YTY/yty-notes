"""dev/test/stage 只读元数据：表结构、行数/容量、同名/相似表（纯只读）。

只读账号与 dba-mcp 同一套（ro_user），仅执行 information_schema / SHOW 类查询。
任何异常都降级为"事实未知"（TableFact.exists=None），绝不臆断"表不存在"。
"""

from __future__ import annotations

import difflib

from .common import TableFact
from .config import Settings


def _connect(settings: Settings, env: str):
    import pymysql  # 延迟导入：引擎自检不需要驱动

    cfg = settings.envs[env]
    return pymysql.connect(
        host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
        charset=cfg.charset, connect_timeout=cfg.connect_timeout, read_timeout=cfg.read_timeout,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_fact(settings: Settings, env: str | None, database: str,
               table: str, cache: dict | None = None) -> TableFact:
    """取目标表事实：是否存在、列定义、索引、行数与容量。

    cache：同一次审核内的请求级缓存（key=env+库+表），多语句操作同一张表时只查一次库，
           避免大 DDL 多语句把网关 30s 超时撑爆（v1.2.1）。
    """
    key = None
    if cache is not None:
        # 表名/库名保持原样：MySQL 在 Linux 上表名大小写敏感，
        # 大写化会让 user 与 USER 命中同一条缓存（TB-16 回退查询会因此失效）
        key = ("fact", (env or "").lower(), database or "", table or "")
        hit = cache.get(key)
        if hit is not None:
            return hit
    fact = _fetch_fact(settings, env, database, table)
    if cache is not None and key is not None:
        cache[key] = fact
    return fact


def _fetch_fact(settings: Settings, env: str | None, database: str, table: str) -> TableFact:
    fact = TableFact(database=database, table=table)
    if settings.metadata_backend != "direct" or not env:
        fact.note = "未启用元数据查询（或未传 env）"
        return fact
    env = env.lower()
    if env not in settings.envs:
        fact.note = f"不支持的环境 {env}（仅 dev/test/stage）"
        return fact
    try:
        conn = _connect(settings, env)
    except Exception as exc:  # noqa: BLE001
        fact.note = f"连接 {env} 只读库失败：{type(exc).__name__}"
        return fact
    try:
        fact.source = f"mysql:{env}"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (database, table),
            )
            row = cur.fetchone()
            fact.exists = row is not None
            if not row:
                return fact
            fact.engine = row.get("ENGINE") or ""
            fact.table_rows = int(row.get("TABLE_ROWS") or 0)
            fact.data_bytes = int((row.get("DATA_LENGTH") or 0) + (row.get("INDEX_LENGTH") or 0))

            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "ORDER BY ORDINAL_POSITION",
                (database, table),
            )
            for c in cur.fetchall():
                name = c["COLUMN_NAME"].upper()
                fact.columns[name] = c["COLUMN_TYPE"]
                fact.nullable[name] = (c.get("IS_NULLABLE") == "YES")

            cur.execute(
                "SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, SUB_PART "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
                (database, table),
            )
            for r in cur.fetchall():
                fact.indexes.setdefault(r["INDEX_NAME"], []).append(r["COLUMN_NAME"])
        return fact
    except Exception as exc:  # noqa: BLE001
        fact.note = f"查询 {env} 元数据失败：{type(exc).__name__}"
        fact.exists = None
        return fact
    finally:
        conn.close()


def fetch_create_table_text(settings: Settings, env: str, database: str, table: str) -> str:
    """取 SHOW CREATE TABLE 原文（供研发/agent 参考，不参与规则判定）。"""
    if settings.metadata_backend != "direct":
        return ""
    env = env.lower()
    if env not in settings.envs:
        return ""
    try:
        conn = _connect(settings, env)
    except Exception:  # noqa: BLE001
        return ""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE `{database}`.`{table}`")
            row = cur.fetchone()
            if not row:
                return ""
            for key in ("Create Table", "Create View"):
                if key in row:
                    return row[key]
            return list(row.values())[1] if len(row) > 1 else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        conn.close()


def similar_tables(settings: Settings, env: str, database: str, table: str, limit: int = 10) -> tuple[list[str], str]:
    """返回相似表清单与来源标记。"""
    if settings.metadata_backend != "direct":
        return [], "none"
    env = env.lower()
    if env not in settings.envs:
        return [], "none"
    try:
        conn = _connect(settings, env)
    except Exception:  # noqa: BLE001
        return [], "unknown"
    try:
        tokens = [t for t in table.upper().split("_") if len(t) >= 3]
        core = max(tokens, key=len) if tokens else table.upper()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE %s LIMIT 200",
                (database, "%" + core + "%"),
            )
            cand = [r["TABLE_NAME"] for r in cur.fetchall() if r["TABLE_NAME"] != table]
        scored = sorted(((difflib.SequenceMatcher(None, table.upper(), c.upper()).ratio(), c)
                         for c in cand), reverse=True)
        return [c for ratio, c in scored if ratio >= 0.6][:limit], f"mysql:{env}"
    except Exception:  # noqa: BLE001
        return [], "unknown"
    finally:
        conn.close()


def table_exists(settings: Settings, env: str, database: str, table: str) -> bool | None:
    if settings.metadata_backend != "direct" or env.lower() not in settings.envs:
        return None
    try:
        conn = _connect(settings, env.lower())
    except Exception:  # noqa: BLE001
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (database, table),
            )
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()
