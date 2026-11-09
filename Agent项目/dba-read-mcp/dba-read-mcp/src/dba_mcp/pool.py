"""按环境隔离的数据库连接池。

每个环境一个 PooledDB；取连接时强制执行只读会话与语句超时。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import pymysql
from dbutils.pooled_db import PooledDB

from .config import DbEnv, Settings, UnknownEnvironment

log = logging.getLogger("dba_mcp.pool")


__all__ = ["ConnectionManager", "UnknownEnvironment"]


class ConnectionManager:
    """懒建池：导入模块时不建连接，第一次用到某环境才创建，避免启动失败。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pools: dict[str, PooledDB] = {}
        self._met_supported: dict[str, bool] = {}
        self._lock = threading.Lock()

    def _pool(self, env_name: str) -> PooledDB:
        pool = self._pools.get(env_name)
        if pool is not None:
            return pool
        db_env = self._settings.environments.get(env_name)
        if db_env is None:
            raise UnknownEnvironment(f"未知环境: {env_name}，可选: {sorted(self._settings.environments)}")
        with self._lock:
            pool = self._pools.get(env_name)
            if pool is None:
                pool = self._build_pool(db_env)
                self._pools[env_name] = pool
            return pool

    @staticmethod
    def _build_pool(env: DbEnv) -> PooledDB:
        return PooledDB(
            creator=pymysql,
            maxconnections=max(1, env.pool_size),
            mincached=1,
            maxcached=max(1, env.pool_size),
            maxusage=None,
            blocking=True,
            ping=1,
            host=env.host,
            port=env.port,
            user=env.user,
            password=env.password,
            database=env.database,
            charset=env.charset,
            connect_timeout=env.connect_timeout,
            read_timeout=env.read_timeout,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

    @contextmanager
    def connection(self, env_name: str) -> Iterator[pymysql.connections.Connection]:
        pool = self._pool(env_name)
        conn = pool.connection()
        try:
            self._harden(conn, env_name)
            yield conn
        finally:
            conn.close()

    def _harden(self, conn: pymysql.connections.Connection, env_name: str) -> None:
        """会话级只读兜底：即使 SQL 校验被绕过，MySQL 层也会拒绝写操作。"""
        guard = self._settings.guard
        with conn.cursor() as cur:
            try:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            except Exception as exc:
                log.warning("env=%s 无法设置只读会话: %s", env_name, exc)

            supports_met = self._met_supported.get(env_name)
            if supports_met is None:
                try:
                    cur.execute("SHOW VARIABLES LIKE 'max_execution_time'")
                    supports_met = cur.fetchone() is not None
                except Exception:
                    supports_met = False
                self._met_supported[env_name] = supports_met

            if supports_met:
                try:
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={int(guard.statement_timeout_ms)}")
                except Exception as exc:
                    log.warning("env=%s 无法设置语句超时: %s", env_name, exc)

    def shutdown(self) -> None:
        for pool in self._pools.values():
            try:
                pool.close()
            except Exception:
                pass
