"""MongoDB 连接管理：一环境一客户端，进程内复用。

为什么自己管而不是每个工具现建：MongoClient 本身就是连接池（默认 100 连接），
每次工具调用新建客户端会带来重复握手（而且 4.0 副本集选主握手不便宜）。
这里按「实际环境名」缓存，`dev`/`test`/`mirror` 各一个池子。

只读服务的连接参数固定三条：
  · `serverSelectionTimeoutMS` 取自配置（默认 8s），拒绝无限等；
  · `appname` 标识来源，DBA 在 currentOp / 慢日志里一眼能认出来是谁；
  · 写重试关闭（`retryWrites=false`，连接串里已带）——本服务不写库。
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from .config import MongoEnv, Settings

log = logging.getLogger("mongodb_ro_mcp.pool")


class MongoManager:
    def __init__(self, settings: Settings, app_name: str = "mongodb-ro-mcp") -> None:
        self._settings = settings
        self._app_name = app_name
        self._clients: dict[str, MongoClient] = {}

    # -- 客户端 --------------------------------------------------------
    def client(self, env: str) -> MongoClient:
        """按请求名/别名取客户端（`stage` → `mirror` 在这个函数里落地一次）。"""
        conf: MongoEnv = self._settings.env(env)
        key = conf.actual
        if key not in self._clients:
            self._clients[key] = MongoClient(
                conf.uri,
                serverSelectionTimeoutMS=conf.connect_timeout_ms,
                appname=self._app_name,
            )
            log.info("已建立 MongoDB 客户端：env=%s（请求名 %s）", conf.actual, env)
        return self._clients[key]

    # -- 便捷取用 ------------------------------------------------------
    def db(self, env: str, database: str) -> Database:
        return self.client(env)[database]

    def coll(self, env: str, database: str, collection: str) -> Collection:
        return self.db(env, database)[collection]

    def admin(self, env: str) -> Database:
        return self.client(env)["admin"]

    # -- 探测 ----------------------------------------------------------
    def probe(self, env: str) -> dict[str, Any]:
        """连通性摘要（`list_environments` / `/healthz` / precheck 共用）。

        MongoDB 4.0 没有 `hello` 命令，统一用 `isMaster`。
        连不上不抛错，返回 `reachable=false` + 原因，让调用方看到全景。
        """
        conf: MongoEnv = self._settings.env(env)
        info: dict[str, Any] = {
            "env": env,
            "env_actual": conf.actual,
            "display_name": conf.display_name,
        }
        try:
            client = self.client(env)
            hello = client.admin.command("isMaster")
            info.update(
                reachable=True,
                setName=hello.get("setName"),
                primary=hello.get("primary"),
                maxWireVersion=hello.get("maxWireVersion"),
                server_version=client.server_info().get("version"),
            )
        except Exception as exc:                     # noqa: BLE001 探测失败不中断
            info.update(reachable=False, error=str(exc).split("\n")[0][:200])
        return info

    # -- 关闭 ----------------------------------------------------------
    def close_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                client.close()
            except Exception:                        # noqa: BLE001
                log.warning("关闭客户端失败：%s", name, exc_info=True)
        self._clients.clear()
