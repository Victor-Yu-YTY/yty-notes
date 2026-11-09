"""执行会话：真实 pymysql 会话 + 可脚本化的假会话。

为什么单独抽一层：executor 是唯一会写库的模块，必须能在完全不连库的情况下
把「采 before-image → 写 undo → 执行 → 校验行数 → 提交/回滚」整条路径跑一遍。
selfcheck 用 ScriptedSession 跑，生产用 MySQLSession。
"""

from __future__ import annotations

from typing import Any

try:  # pymysql 只在真实后端需要
    import pymysql  # type: ignore
    from pymysql.cursors import DictCursor  # type: ignore
except Exception:  # noqa: BLE001
    pymysql = None  # type: ignore
    DictCursor = None  # type: ignore


class Session:
    """执行会话接口。query 返回字典列表，execute 返回受影响行数。"""

    name = "base"

    def query(self, sql: str, args: tuple | None = None) -> list[dict]:
        raise NotImplementedError

    def execute(self, sql: str, args: tuple | None = None) -> int:
        raise NotImplementedError

    def scalar(self, sql: str, args: tuple | None = None):
        rows = self.query(sql, args)
        if not rows:
            return None
        return next(iter(rows[0].values()), None)

    def begin(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- 真实会话


class MySQLSession(Session):
    """执行账号的会话。autocommit 关闭，事务由 executor 显式控制。"""

    name = "mysql"

    def __init__(self, cfg: dict[str, Any], connect_timeout: int = 5):
        if pymysql is None:
            raise RuntimeError("未安装 pymysql，无法执行")
        self.cfg = cfg
        self._conn = pymysql.connect(
            host=cfg["host"], port=int(cfg.get("port", 3306)),
            user=cfg["user"], password=cfg["password"],
            charset=cfg.get("charset", "utf8mb4"),
            connect_timeout=connect_timeout,
            read_timeout=int(cfg.get("read_timeout", 60)),
            write_timeout=int(cfg.get("write_timeout", 60)),
            cursorclass=DictCursor, autocommit=False,
        )

    def query(self, sql: str, args: tuple | None = None) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(sql, args or ())
            return list(cur.fetchall() or [])

    def execute(self, sql: str, args: tuple | None = None) -> int:
        with self._conn.cursor() as cur:
            cur.execute(sql, args or ())
            return int(cur.rowcount if cur.rowcount is not None else 0)

    def begin(self) -> None:
        self._conn.begin()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 假会话


class ScriptedSession(Session):
    """按正则匹配返回预置结果的假会话，用于 selfcheck。

    queries   : [(正则, 行列表)]      —— query 命中即返回
    rowcounts : [(正则, 行数)]        —— execute 命中即返回
    未命中：query 返回 []，execute 返回 1，并记入 unmatched 便于排查用例。
    """

    name = "scripted"

    def __init__(self, queries: list | None = None, rowcounts: list | None = None,
                 fail_on: list | None = None):
        self.queries: list = queries or []
        self.rowcounts: list = rowcounts or []
        self.fail_on: list = fail_on or []
        self.log: list[tuple[str, str]] = []
        self.unmatched: list[str] = []
        self.committed = 0
        self.rolled_back = 0
        self.began = 0

    # -- 内部
    @staticmethod
    def _match(table: list, sql: str):
        import re
        for pat, val in table:
            if re.search(pat, sql, re.I | re.S):
                return True, val
        return False, None

    def _check_fail(self, sql: str) -> None:
        import re
        for pat in self.fail_on:
            if re.search(pat, sql, re.I | re.S):
                raise RuntimeError(f"模拟数据库报错：{sql[:80]}")

    # -- 接口
    def query(self, sql: str, args: tuple | None = None) -> list[dict]:
        self.log.append(("query", sql.strip()))
        self._check_fail(sql)
        hit, val = self._match(self.queries, sql)
        if not hit:
            self.unmatched.append(sql.strip())
            return []
        return [dict(r) for r in (val or [])]

    def execute(self, sql: str, args: tuple | None = None) -> int:
        self.log.append(("execute", sql.strip()))
        self._check_fail(sql)
        hit, val = self._match(self.rowcounts, sql)
        if not hit:
            self.unmatched.append(sql.strip())
            return 1
        return int(val)

    def begin(self) -> None:
        self.began += 1
        self.log.append(("begin", ""))

    def commit(self) -> None:
        self.committed += 1
        self.log.append(("commit", ""))

    def rollback(self) -> None:
        self.rolled_back += 1
        self.log.append(("rollback", ""))

    # -- 断言辅助
    def executed(self) -> list[str]:
        return [s for k, s in self.log if k == "execute"]

    def find(self, pattern: str) -> list[str]:
        import re
        return [s for _, s in self.log if re.search(pattern, s, re.I | re.S)]
