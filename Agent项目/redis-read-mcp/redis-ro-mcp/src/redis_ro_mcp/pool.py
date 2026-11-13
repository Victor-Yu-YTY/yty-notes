"""Redis 连接管理：一环境一客户端，进程内复用。

为什么自己管而不是每个工具现建：redis-py 的 `Redis` 自带连接池，同一环境重复新建
只会反复握手（Predixy 侧的连接开销不便宜），还容易把连接数堆上去。这里按
**实际环境名**缓存，`dev`/`test`/`mirror` 各一个客户端。

只读服务固定住的四个连接参数（缺一个就连不上，见 README §3）：

  · `protocol=2`      —— redis-py 8.x 默认走 RESP3 的 `HELLO` 握手，Redis 5.0.6 不认；
  · `lib_name=""`     —— redis-py 8.x 连接时发 `CLIENT SETINFO`，Predixy 白名单拒 `CLIENT`；
  · `socket_timeout`  —— 取自配置（默认 5s），拒绝无限等；
  · `decode_responses=True` —— 统一返回 str，避免 bytes 混进 JSON 响应。

本模块**只读**：不提供 pipeline/事务/发布订阅等任何写路径，也不暴露底层连接给
工具之外的调用方。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import redis as redis_lib

from .config import RedisEnv, Settings
from .redis_guard import mask_secret

log = logging.getLogger("redis_ro_mcp.pool")


def make_client(conf: RedisEnv) -> redis_lib.Redis:
    """按环境配置建客户端；老版本 redis-py 不认某些关键字时降级重试。"""
    common: dict[str, Any] = dict(
        host=conf.host,
        port=conf.port,
        db=conf.db,
        socket_timeout=max(conf.socket_timeout_ms, 100) / 1000.0,
        socket_connect_timeout=max(conf.connect_timeout_ms, 100) / 1000.0,
        decode_responses=True,
        retry_on_timeout=False,
        health_check_interval=0,
    )
    if conf.username:
        common["username"] = conf.username
    if conf.password:
        common["password"] = conf.password

    # protocol / lib_name 是 redis-py 5.0+ 的参数；Predixy + Redis 5 必须这么传。
    try:
        return redis_lib.Redis(protocol=conf.protocol, lib_name=conf.lib_name,
                               lib_version="", **common)
    except TypeError:                                  # 老版本 redis-py
        log.warning("当前 redis-py 不识别 protocol/lib_name 关键字，降级建连（可能与 Redis 5 不兼容）")
        return redis_lib.Redis(**common)


class RedisManager:
    def __init__(self, settings: Settings, app_name: str = "redis-ro-mcp") -> None:
        self._settings = settings
        self._app_name = app_name
        self._clients: dict[str, redis_lib.Redis] = {}

    # -- 客户端 --------------------------------------------------------
    def client(self, env: str) -> redis_lib.Redis:
        """按请求名/别名取客户端（`stage` → `mirror` 在这个函数里落地一次）。"""
        conf: RedisEnv = self._settings.env(env)
        key = conf.actual
        if key not in self._clients:
            self._clients[key] = make_client(conf)
            log.info("已建立 Redis 客户端：env=%s（请求名 %s）→ %s auth=%s",
                     conf.actual, env, conf.endpoint,
                     "yes" if conf.has_auth else "no")
        return self._clients[key]

    def env(self, env: str) -> RedisEnv:
        return self._settings.env(env)

    # -- 探测 ----------------------------------------------------------
    def probe(self, env: str, deep: bool = False) -> dict[str, Any]:
        """连通性摘要（`list_environments` / `/healthz` / precheck 共用）。

        连不上不抛错，返回 `reachable=false` + 原因，让调用方看到全景。
        `deep=True` 时会额外探一次 Predixy 白名单（`DBSIZE` 预期被拒），
        用于预检/验收；日常 `list_environments` 走浅探测，不制造预期内的失败命令。
        """
        conf: RedisEnv = self._settings.env(env)
        info: dict[str, Any] = {
            "env": conf.name,
            "env_actual": conf.actual,
            "display_name": conf.display_name,
            "endpoint": conf.endpoint,
            "auth": bool(conf.has_auth),
        }
        started = time.perf_counter()
        try:
            client = self.client(env)
            info["ping"] = bool(client.ping())
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self._fill_info(client, info)
            # SCAN 抽样：证明键枚举这条主路径可用
            cursor, keys = client.scan(cursor=0, count=5)
            info["scan_sample"] = list(keys)[:3]
            info["scan_next_cursor"] = int(cursor or 0)
            info["reachable"] = True
            if deep:
                info["whitelist"] = self._whitelist_probe(client)
        except redis_lib.AuthenticationError as exc:
            info.update(reachable=False, ping=False,
                        error="认证失败: %s" % str(exc).split("\n")[0][:200])
        except Exception as exc:                       # noqa: BLE001 探测失败不中断
            info.update(reachable=False, ping=False,
                        error="%s: %s" % (type(exc).__name__,
                                          str(exc).split("\n")[0][:200]))
        finally:
            if conf.password:
                # 口令绝不出现在返回体/日志里（当前实例无口令，留着防未来翻车）
                info["password"] = mask_secret(conf.password)
        return info

    @staticmethod
    def _fill_info(client: redis_lib.Redis, info: dict[str, Any]) -> None:
        """INFO 经 Predixy 返回的是**代理自身**信息（Name/Version/RedisMode）。

        后端 Redis 版本经代理不可见——这一点必须在响应里说清楚，否则会被误读成
        "线上跑的是 Predixy 1.0.5 版本"。
        """
        try:
            raw = client.info() or {}
        except Exception as exc:                       # noqa: BLE001
            info["proxy"] = {"error": str(exc).split("\n")[0][:120]}
            return
        if not isinstance(raw, dict):
            info["proxy"] = {"raw": str(raw)[:200]}
            return
        info["proxy"] = {
            "name": raw.get("Name") or raw.get("name"),
            "version": raw.get("Version") or raw.get("version"),
            "mode": raw.get("RedisMode") or raw.get("redis_mode"),
        }
        if raw.get("uptime_in_seconds") is not None:
            info["proxy"]["uptime_hours"] = round(
                float(raw.get("uptime_in_seconds") or 0) / 3600.0, 1)
        info["version_note"] = "以上版本信息为 Predixy 代理层；后端 Redis 版本经 proxy 不可见"

    @staticmethod
    def _whitelist_probe(client: redis_lib.Redis) -> dict[str, Any]:
        """Predixy 白名单实测：允许的应成功，拒绝的应报错（结果只作事实记录）。"""
        result: dict[str, Any] = {"allowed": {}, "rejected": {}}
        allowed = {
            "ping": lambda: client.ping(),
            "scan": lambda: len(client.scan(cursor=0, count=1)[1]),
            "randomkey": lambda: client.randomkey(),
            "config_get": lambda: client.config_get("maxmemory"),
        }
        for label, fn in allowed.items():
            try:
                fn()
                result["allowed"][label] = "ok"
            except Exception as exc:                   # noqa: BLE001
                result["allowed"][label] = "denied: %s" % str(exc).split("\n")[0][:80]
        # 只调用 redis-py 的具名方法，**不用 execute_command**：
        # 本服务全代码库没有通用命令执行入口（verify.sh 会静态断言这一点）。
        rejected = {
            "dbsize": lambda: client.dbsize(),
            "slowlog": lambda: client.slowlog_get(1),
            "client_info": lambda: client.client_info(),
        }
        for label, fn in rejected.items():
            try:
                fn()
                result["rejected"][label] = "意外可用（Predixy 可能已升级/放开）"
            except Exception as exc:                   # noqa: BLE001
                result["rejected"][label] = "denied（预期）: %s" % str(exc).split("\n")[0][:60]
        return result

    # -- 关闭 ----------------------------------------------------------
    def close_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                client.close()
            except Exception:                        # noqa: BLE001
                log.warning("关闭客户端失败：%s", name, exc_info=True)
        self._clients.clear()
