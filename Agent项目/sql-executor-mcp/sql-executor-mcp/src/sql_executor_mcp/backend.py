"""数据源抽象：真实 MySQL 与离线 Stub 两套实现。

P0 的判定全部通过 Provider 取事实，因此 selfcheck 可以完全不连库，
用 StubProvider 注入用例里的事实。
"""

from __future__ import annotations

import re
from typing import Any

try:  # pymysql 只在真实后端需要，导入失败不影响 Stub 与用例
    import pymysql  # type: ignore
    from pymysql.cursors import DictCursor  # type: ignore
except Exception:  # noqa: BLE001
    pymysql = None  # type: ignore
    DictCursor = None  # type: ignore


class Provider:
    """事实来源接口。任何方法返回 None 表示「取不到」，绝不用默认值糊弄。"""

    name = "base"

    def server_vars(self) -> dict[str, str] | None:
        return None

    def database_exists(self, db: str) -> bool | None:
        return None

    def table_info(self, db: str, table: str) -> dict[str, Any] | None:
        """{'exists': bool, 'rows': int|None, 'size_gb': float|None, 'engine': str|None}"""
        return None

    def columns(self, db: str, table: str) -> dict[str, dict[str, Any]] | None:
        """{COL_NAME: {'nullable': bool, 'has_default': bool, 'type': str, 'primary': bool}}"""
        return None

    def indexes(self, db: str, table: str) -> list[dict[str, Any]] | None:
        """[{'name': str, 'columns': [str], 'unique': bool}]"""
        return None

    def triggers(self, db: str, table: str) -> list[str] | None:
        return None

    def privileges(self) -> set[str] | None:
        return None

    def replica_lag(self) -> int | None:
        return None

    def long_trx_count(self, seconds: int) -> int | None:
        return None

    def disk_free_gb(self) -> float | None:
        return None

    def count_where(self, db: str, table: str, where: str, timeout: int) -> int | None:
        return None

    def explain_type(self, db: str, sql: str) -> str | None:
        return None

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- 真实后端

# SHOW GRANTS 的一行：GRANT <权限列表> ON <范围> TO <账号> [WITH GRANT OPTION]
_GRANT_HEAD_RE = re.compile(r"^GRANT\s+(.+?)\s+ON\s+\S+\s+TO\s+", re.I)
_ALL_PRIVS = "ALL PRIVILEGES"


def parse_privileges(lines: list[str]) -> set[str]:
    """把 SHOW GRANTS 的每行文本解析成权限集合（纯函数，便于离线自检）。

    坑：不能用「整行按逗号切」的粗暴做法——第一段是 ``GRANT SELECT``，
    切出来会变成 ``"GRANT SELECT"``，于是 SELECT 永远被判缺失，
    EX-G07 会把真实库上的每一条语句都拦掉（离线自检用 Stub 权限，发现不了）。
    """
    privs: set[str] = set()
    for raw in lines:
        text = (raw or "").strip()
        if not text or text.upper().startswith("GRANT PROXY"):
            continue
        m = _GRANT_HEAD_RE.match(text)
        if not m:
            continue
        part = m.group(1).upper()
        if _ALL_PRIVS in part:
            privs.add(_ALL_PRIVS)
            continue
        for tok in part.split(","):
            # 列级授权 SELECT (col) → SELECT
            tok = re.sub(r"\s*\(.*\)$", "", tok.strip()).strip()
            if tok:
                privs.add(tok)
    return privs


class MySQLProvider(Provider):
    name = "mysql"

    def __init__(self, cfg: dict[str, Any], timeout: int = 10):
        if pymysql is None:
            raise RuntimeError("未安装 pymysql，无法使用真实后端")
        self.cfg = cfg
        self.timeout = timeout
        self._conn = None

    def _connect(self):
        if self._conn is not None:
            # 长连接会被 wait_timeout 杀掉；不 ping 的话所有事实都会静默变成 None，
            # 然后 fail-closed 把所有 SQL 都判成 BLOCKED —— 故障表现是"全拦"，很难查。
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:  # noqa: BLE001
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None
        c = self.cfg
        self._conn = pymysql.connect(
            host=c["host"], port=int(c.get("port", 3306)),
            user=c["user"], password=c["password"],
            charset=c.get("charset", "utf8mb4"),
            connect_timeout=self.timeout, read_timeout=self.timeout,
            cursorclass=DictCursor, autocommit=True,
        )
        return self._conn

    def _q(self, sql: str, args: tuple | None = None) -> list[dict]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return list(cur.fetchall() or [])

    def _one(self, sql: str, args: tuple | None = None) -> dict | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _scalar(self, sql: str, args: tuple | None = None):
        row = self._one(sql, args)
        if not row:
            return None
        return next(iter(row.values()), None)

    def server_vars(self) -> dict[str, str] | None:
        try:
            rows = self._q("SHOW VARIABLES WHERE Variable_name IN "
                           "('default_storage_engine','version','datadir','tmpdir')")
            return {r["Variable_name"]: r["Value"] for r in rows}
        except Exception:  # noqa: BLE001
            return None

    def database_exists(self, db: str) -> bool | None:
        try:
            return bool(self._scalar(
                "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (db,)))
        except Exception:  # noqa: BLE001
            return None

    def table_info(self, db: str, table: str) -> dict[str, Any] | None:
        try:
            row = self._one(
                "SELECT TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, ENGINE "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (db, table))
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return {"exists": False, "rows": None, "size_gb": None, "engine": None}
        size = (row.get("DATA_LENGTH") or 0) + (row.get("INDEX_LENGTH") or 0)
        return {
            "exists": True,
            "rows": row.get("TABLE_ROWS"),
            "size_gb": round(size / 1024 ** 3, 3) if size else 0.0,
            "engine": row.get("ENGINE"),
        }

    def columns(self, db: str, table: str) -> dict[str, dict[str, Any]] | None:
        try:
            rows = self._q(
                "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_TYPE, COLUMN_KEY "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "ORDER BY ORDINAL_POSITION", (db, table))
        except Exception:  # noqa: BLE001
            return None
        if not rows:
            return None
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            name = r["COLUMN_NAME"]
            out[name.upper()] = {
                "name": name,
                "nullable": r["IS_NULLABLE"] == "YES",
                "has_default": r["COLUMN_DEFAULT"] is not None,
                "type": r["COLUMN_TYPE"],
                "primary": r["COLUMN_KEY"] == "PRI",
                "auto_increment": "auto_increment" in (r["COLUMN_TYPE"] or "").lower(),
            }
        return out

    def indexes(self, db: str, table: str) -> list[dict[str, Any]] | None:
        try:
            rows = self._q(
                "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "ORDER BY INDEX_NAME, SEQ_IN_INDEX", (db, table))
        except Exception:  # noqa: BLE001
            return None
        if rows is None:
            return None
        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            name = r["INDEX_NAME"]
            g = grouped.setdefault(name, {"name": name, "columns": [], "unique": r["NON_UNIQUE"] == 0})
            if r["COLUMN_NAME"]:
                g["columns"].append(r["COLUMN_NAME"])
        return list(grouped.values())

    def triggers(self, db: str, table: str) -> list[str] | None:
        try:
            rows = self._q(
                "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                "WHERE EVENT_OBJECT_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s", (db, table))
            return [r["TRIGGER_NAME"] for r in rows]
        except Exception:  # noqa: BLE001
            return None

    def privileges(self) -> set[str] | None:
        try:
            rows = self._q("SHOW GRANTS FOR CURRENT_USER()")
        except Exception:  # noqa: BLE001
            return None
        return parse_privileges([str(next(iter(r.values()), "") or "") for r in rows])

    def replica_lag(self) -> int | None:
        for stmt in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
            try:
                row = self._one(stmt)
            except Exception:  # noqa: BLE001
                continue
            if row:
                for key in ("Seconds_Behind_Source", "Seconds_Behind_Master"):
                    if key in row and row[key] is not None:
                        return int(row[key])
                return None
        return None

    def long_trx_count(self, seconds: int) -> int | None:
        try:
            v = self._scalar(
                "SELECT COUNT(*) AS C FROM information_schema.INNODB_TRX "
                "WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > %s", (seconds,))
            return int(v) if v is not None else None
        except Exception:  # noqa: BLE001
            return None

    def disk_free_gb(self) -> float | None:
        """MySQL 没有内置剩余磁盘查询，返回 None 交由 EX-A07 的 on_missing 处理。"""
        return None

    def count_where(self, db: str, table: str, where: str, timeout: int) -> int | None:
        sql = f"SELECT COUNT(*) AS C FROM `{db}`.`{table}` WHERE {where}"
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute("SET SESSION max_execution_time=%s", (timeout * 1000,))
                cur.execute(sql)
                row = cur.fetchone()
            return int(row["C"]) if row else None
        except Exception:  # noqa: BLE001
            return None

    def explain_type(self, db: str, sql: str) -> str | None:
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(f"USE `{db}`")
                cur.execute("EXPLAIN " + sql)
                row = cur.fetchone()
            return (row or {}).get("type")
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


# ---------------------------------------------------------------- 离线 Stub


class StubProvider(Provider):
    """从用例 YAML 注入事实，用于 selfcheck 与本地调试，完全不连库。"""

    name = "stub"

    def __init__(self, spec: dict[str, Any] | None = None):
        self.spec = spec or {}

    def _get(self, key: str, default=None):
        return self.spec.get(key, default)

    def server_vars(self):
        return self._get("server_vars", {"default_storage_engine": "InnoDB"})

    def database_exists(self, db: str):
        return self._get("database_exists", True)

    def table_info(self, db: str, table: str):
        t = self._get("table")
        if t is None:
            return {"exists": False, "rows": None, "size_gb": None, "engine": None}
        return {
            "exists": t.get("exists", True),
            "rows": t.get("rows"),
            "size_gb": t.get("size_gb"),
            "engine": t.get("engine", "InnoDB"),
        }

    def columns(self, db: str, table: str):
        cols = self._get("columns")
        if cols is None:
            return None
        nul = {k.upper(): v for k, v in (self._get("nullables") or {}).items()}
        dft = {k.upper(): v for k, v in (self._get("has_default") or {}).items()}
        typ = {k.upper(): v for k, v in (self._get("types") or {}).items()}
        ai = {x.upper() for x in (self._get("auto_increment") or [])}
        out = {}
        for c in cols:
            out[c.upper()] = {
                "name": c,
                "nullable": nul.get(c.upper(), True),
                "has_default": dft.get(c.upper(), False),
                "type": typ.get(c.upper(), ""),
                "primary": c.upper() in [x.upper() for x in (self._get("primary_key") or [])],
                "auto_increment": c.upper() in ai,
            }
        return out

    def indexes(self, db: str, table: str):
        idx = self._get("indexes")
        if idx is None:
            pk = self._get("primary_key") or []
            return [{"name": "PRIMARY", "columns": pk, "unique": True}] if pk else []
        return idx

    def triggers(self, db: str, table: str):
        return self._get("triggers", [])

    def privileges(self):
        return set(self._get("privileges", [
            "SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX",
        ]))

    def replica_lag(self):
        return self._get("replica_lag", 0)

    def long_trx_count(self, seconds: int):
        return self._get("long_trx_count", 0)

    def disk_free_gb(self):
        return self._get("disk_free_gb")

    def count_where(self, db: str, table: str, where: str, timeout: int):
        return self._get("count_rows")

    def explain_type(self, db: str, sql: str):
        return self._get("explain_type", "ref")
