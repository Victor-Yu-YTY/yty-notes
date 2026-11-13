"""redis-ro-mcp 服务入口：11 个只读工具 + 准入令牌闸门 + 身份审计 + /healthz。

## 这个服务是什么

一个进程覆盖线下三套 Redis（`dev` 开发 / `test` 测试 / `stage` 模拟，
**`stage` 的实际实例是内部 `mirror`，`mirror`/`sim` 作别名**），工具第一个参数一律是
`env`。三套环境都是 **Redis 5.0.6（无密码、无 ACL）+ 前置 Predixy 1.0.5 代理**
（端口 25900、db0）。因此：

* `protocol=2` —— Redis 5 不认 RESP3 的 `HELLO` 握手；
* `lib_name=""` —— Predixy 白名单拒绝 `CLIENT SETINFO`；
* 不用 `DBSIZE`/`OBJECT`/`MEMORY`/`SLOWLOG`/`CLIENT`/`CLUSTER`（代理实测拒绝）；
* 键枚举一律 `SCAN`，**绝不 `KEYS`**；大 string 用 `GETRANGE` 分段读。

## 只读三层

| 层 | 措施 |
| --- | --- |
| 接口层 | 工具面只登记读操作，**没有** `execute` / `eval` / 通用命令执行入口 |
| 语义层 | 只调用白名单读命令；写命令黑名单在 `redis_guard.py`，并作为静态自检的断言源 |
| 资源层 | SCAN COUNT、成员数、采样数、Top N、单字段字符、响应字节、socket 超时全部有上限 |

> ⚠️ **实例无密码无 ACL，数据库层没有任何写拒绝能力**——只读边界的最终保障是
> 网络位置（只绑内网）+ 网关准入令牌 + 本服务的代码白名单，三者缺一不可。
> 详见 README §6「已知限制」。

## 鉴权与审计（与 db-read-mcp v1.0.0 / sql-review-* v1.0.1 / mongodb-ro-mcp 同口径）

* **只有一层鉴权**：准入令牌 `X-REDIS-RO-MCP-TOKEN`，缺失/错误 → HTTP 403，
  且早于 MCP 协议层；令牌为空时**拒绝启动**；
* **调用方身份只用于审计**：网关透传的 `X-Agent-Id` / `X-Forwarded-For`
  由 identity.py 解析后落进每行审计（`agent_id` / `client_ip` / `socket_ip` /
  `*_source` / `identity_trusted`），**不参与任何放行判定**；
* 每个工具出口、鉴权拒绝、协议层拒绝、启动各写一行 JSONL，未直接放行的结论
  都带 `reject{code,stage,text,evidence}`。

启动：
    python -m redis_ro_mcp.server --transport stdio             # 本机调试
    python -m redis_ro_mcp.server --transport http --port 8050  # 218 共享部署
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import json
import logging
import os
import time
from typing import Any, Callable

import redis as redis_lib
from fastmcp import FastMCP

from . import __version__ as _VERSION
from . import audit as A
from . import identity as I
from .auth import pick_header, token_ok
from .config import ConfigError, RedisEnv, Settings, UnknownEnvironment, load_settings
from .pool import RedisManager
from .redis_guard import (
    GuardError,
    cap_items,
    check_key,
    check_pattern,
    check_range,
    check_score,
    clamp_bytes,
    clamp_int,
    dumps,
)
from .redis_guard import guard_command as _guard_command

log = logging.getLogger("redis_ro_mcp.server")

_STATE: dict[str, Any] = {}
mcp = FastMCP("redis-ro-mcp")

# 单次请求上下文（审计用）
_CTX_IDENT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "redis_ctx_ident", default={}
)
_CTX_AUDITED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "redis_ctx_audited", default=False
)
_CTX_STARTED: contextvars.ContextVar[float] = contextvars.ContextVar(
    "redis_ctx_started", default=0.0
)


# --------------------------------------------------------------------------
# 状态初始化
# --------------------------------------------------------------------------
def init_state(config_path: str | None = None) -> Settings:
    if "settings" in _STATE:
        return _STATE["settings"]
    settings = load_settings(config_path or os.environ.get("REDIS_MCP_CONFIG"))
    _STATE["settings"] = settings
    _STATE["conns"] = RedisManager(settings)
    _STATE["policy"] = A.AuditPolicy.from_config(settings.audit)
    return settings


def _settings() -> Settings:
    return _STATE.get("settings") or init_state()


def _conns() -> RedisManager:
    _settings()
    return _STATE["conns"]


def _policy() -> A.AuditPolicy:
    _settings()
    return _STATE["policy"]


# --------------------------------------------------------------------------
# 统一返回信封
# --------------------------------------------------------------------------
def _elapsed_ms() -> float:
    start = _CTX_STARTED.get()
    return round((time.perf_counter() - start) * 1000, 1) if start else 0.0


def _ok(payload: dict[str, Any], *, source: str = "redis", truncated: bool = False,
        warning: str | None = None) -> str:
    """统一成功信封：{ok, source, elapsed_ms, truncated, data}。

    两层截断：`dumps` 先按字段字符数缩水（保留结构和提示），
    `clamp_bytes` 再按 **字节** 兜底（CJK 按字符切会算错，这里先编码再切）。
    """
    guard = _settings().guard
    body: dict[str, Any] = {
        "ok": True,
        "source": source,
        "elapsed_ms": _elapsed_ms(),
        "truncated": truncated,
        "data": payload,
    }
    if warning:
        body["warning"] = warning
    return clamp_bytes(dumps(body, guard.max_field_chars), guard.max_bytes)


def _err(code: str, message: str, **extra: Any) -> str:
    body = {"ok": False, "error": {"code": code, "message": message, **extra}}
    return clamp_bytes(
        dumps(body, _settings().guard.max_field_chars), _settings().guard.max_bytes
    )


def _env_of(name: str) -> RedisEnv:
    return _settings().env(name)


def _env_brief(name: str) -> dict[str, Any]:
    """响应里的环境回显：env 恒为**规范名**（dev/test/stage），
    传了别名时另附 env_alias，方便调用方与审计对齐。"""
    conf = _env_of(name)
    brief = {"env": conf.name, "env_actual": conf.actual,
             "display_name": conf.display_name, "endpoint": conf.endpoint}
    if name and name != conf.name:
        brief["env_alias"] = name
    return brief


def _db_label(conf: RedisEnv) -> str:
    """审计里的 database 字段：Redis 侧用库序号，形如 `db0`。"""
    return "db%d" % conf.db


def _assert_cmd(*names: str) -> None:
    """运行时断言：即将发出的命令必须在只读白名单内、且不在写命令黑名单内。

    为什么在工具里再拦一次：白名单是配置项（`environments.yaml` 可改）。若有人
    手滑把某条命令从白名单删掉、或误把写命令加进白名单，这里会直接抛 GuardError
    并落一条 `GUARD_COMMAND_DENIED` 审计，而不是默默把命令发到实例上。
    这是"改规则改声明、不改代码"的安全阀。
    """
    guard = _settings().guard
    for name in names:
        _guard_command(name, guard.allowed_commands, guard.forbidden_commands)


def _len_of(r: redis_lib.Redis, key: str, ktype: str) -> int | None:
    """按类型取长度：string=字节，其余=元素数。代理拒绝时返回 None。

    ★ `MEMORY USAGE` 被 Predixy 拒绝，所以这里只做"类型化长度近似"，
      响应里用 `len_unit` 明确单位，避免被当成精确内存占用。
    """
    try:
        if ktype == "string":
            return int(r.strlen(key))
        if ktype == "list":
            return int(r.llen(key))
        if ktype == "hash":
            return int(r.hlen(key))
        if ktype == "set":
            return int(r.scard(key))
        if ktype == "zset":
            return int(r.zcard(key))
    except Exception:                                   # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
def _ident_kw() -> dict[str, Any]:
    ident = _CTX_IDENT.get() or {}
    if not ident or not getattr(_settings().identity, "record", True):
        return {}
    return {
        "client_ip": ident.get("client_ip", ""),
        "agent_id": ident.get("agent_id", ""),
        "socket_ip": ident.get("socket_ip", ""),
        "client_ip_source": ident.get("client_ip_source", ""),
        "agent_id_source": ident.get("agent_id_source", ""),
        "identity_trusted": ident.get("identity_trusted"),
    }


def _audit(*, event: str = "tool_call", tool: str = "", outcome: str = "ok",
           reject: dict | None = None, env: str | None = None,
           env_actual: str | None = None, key: str | None = None,
           pattern: str | None = None, query: Any = None,
           detail: dict | None = None) -> None:
    settings = _settings()
    policy = _policy()

    database = None
    # 审计里的 env 恒为规范名（dev/test/stage）；调用方传别名时把原名塞进 detail
    if env:
        try:
            conf = settings.env(env)
            if env != conf.name:
                detail = {**(detail or {}), "env_alias": env}
            env, env_actual = conf.name, env_actual or conf.actual
            database = _db_label(conf)
        except Exception:                               # noqa: BLE001 未知环境保留原值
            pass
    if pattern:
        detail = {**(detail or {}), "pattern": pattern}

    sha, length = A.fingerprint(query)
    text = A.preview(query) if getattr(settings.audit, "record_query_text", False) else ""
    try:
        A.record(
            settings.audit.dir,
            policy,
            event=event,
            tool=tool,
            outcome=outcome,
            reject=reject,
            env=env,
            env_actual=env_actual,
            database=database,
            collection=key,          # Redis 侧：被访问的 key
            query_sha256=sha,
            query_len=length,
            query_text=text,
            detail=detail,
            **_ident_kw(),
        )
    except Exception:                                   # noqa: BLE001 审计失败不影响业务
        log.warning("审计写入失败", exc_info=True)


# --------------------------------------------------------------------------
# 工具装饰器：统一计时 + 异常兜底 + 审计
# --------------------------------------------------------------------------
# Predixy 拒绝命令时的提示（README §2 的白名单是这份提示的唯一来源）
_PROXY_HINT = (
    "可用命令以 README §2 的 Predixy 白名单实测为准：PING/INFO/SCAN/RANDOMKEY/TYPE/"
    "TTL/STRLEN/GETRANGE/LRANGE/LLEN/HGETALL/HGET/HLEN/HKEYS/SMEMBERS/SCARD/ZRANGE/"
    "ZCARD/ZRANGEBYSCORE/EXISTS/CONFIG GET(单参)。"
    "DBSIZE/OBJECT/MEMORY/SLOWLOG/CLIENT/CLUSTER 被代理拒绝。"
)


def tool(tool_name: str) -> Callable:
    """统一出口处理。

    注意：被装饰函数签名里**不要**出现带默认值的内部参数 —— MCP SDK 会把默认值
    一起当成调用参数传进来（db-read-mcp 踩过的坑），计时改用 contextvar。
    """

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            _CTX_STARTED.set(time.perf_counter())
            settings = _settings()
            env_arg = kwargs.get("env")
            key_arg = kwargs.get("key")
            pattern_arg = kwargs.get("pattern")
            raw_query = key_arg or pattern_arg or ""
            try:
                out = fn(*args, **kwargs)
                reject = None
                outcome = "ok"
                try:
                    parsed = json.loads(out)
                    if isinstance(parsed, dict) and parsed.get("ok") is False:
                        outcome = "invalid_request"
                        err = parsed.get("error") or {}
                        reject = A.make_reject(str(err.get("code") or "INTERNAL_EXCEPTION"),
                                               str(err.get("message") or ""))
                except (ValueError, TypeError):
                    outcome = "info"
                _audit(tool=tool_name, outcome=outcome, reject=reject,
                       env=env_arg, key=key_arg, pattern=pattern_arg, query=raw_query)
                return out
            except UnknownEnvironment as exc:
                reject = A.make_reject("ARG_ENV_UNKNOWN", str(exc))
                _audit(tool=tool_name, outcome="invalid_request", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("ENV_NOT_FOUND", str(exc), available=settings.names())
            except GuardError as exc:
                reject = A.make_reject(exc.code, exc.message, exc.evidence)
                _audit(tool=tool_name, outcome="invalid_request", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                extra = {"evidence": exc.evidence} if exc.evidence else {}
                return _err(exc.code, exc.message, **extra)
            except redis_lib.AuthenticationError as exc:
                reject = A.make_reject("DEP_AUTH_FAILED", str(exc).split("\n")[0][:200])
                _audit(tool=tool_name, outcome="denied", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("AUTH_FAILED", reject["text"],
                            hint="当前实例应为无密码；若已启用 ACL，请把口令/用户注入 "
                                 "/etc/redis-ro-mcp/redis-ro-mcp.env（见部署说明）")
            except redis_lib.ResponseError as exc:
                text = str(exc).split("\n")[0][:300]
                if "unknown command" in text.lower() or "not supported" in text.lower() \
                        or "denied" in text.lower():
                    reject = A.make_reject("DEP_PROXY_DENIED", text)
                    _audit(tool=tool_name, outcome="denied", reject=reject,
                           env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                    return _err("PROXY_DENIED", text, hint=_PROXY_HINT)
                reject = A.make_reject("INTERNAL_EXCEPTION", text)
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("TOOL_ERROR", text)
            except redis_lib.TimeoutError as exc:
                reject = A.make_reject("DEP_TIMEOUT", "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("TIMEOUT", reject["text"],
                            hint="命令超时（socket_timeout 到点）；缩小 SCAN count / 采样数，"
                                 "或确认 Predixy 与后端是否正常")
            except redis_lib.ConnectionError as exc:
                reject = A.make_reject("DEP_REDIS_UNREACHABLE",
                                       "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("REDIS_UNREACHABLE", reject["text"],
                            hint="确认 Predixy 可达、端口 25900 放通（见 scripts/checks/precheck.py）")
            except redis_lib.RedisError as exc:
                reject = A.make_reject("INTERNAL_EXCEPTION",
                                       "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("TOOL_ERROR", "%s: %s" % (type(exc).__name__, exc))
            except Exception as exc:                    # noqa: BLE001 最后兜底
                log.exception("tool=%s 执行失败", tool_name)
                reject = A.make_reject("INTERNAL_EXCEPTION",
                                       "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""), key=key_arg, pattern=pattern_arg)
                return _err("TOOL_ERROR", "%s: %s" % (type(exc).__name__, exc))

        return wrapper

    return deco


# --------------------------------------------------------------------------
# 环境发现
# --------------------------------------------------------------------------
@mcp.tool()
@tool("list_environments")
def list_environments() -> str:
    """列出本 MCP 覆盖的全部 Redis 环境（连通性、Predixy 版本、SCAN 抽样）。

    其余工具的 env 参数从这里取值：规范名是 dev / test / stage，
    stage 的实际实例是内部 mirror（mirror / sim 也可直接传）。
    任何一步失败都不会中断，连不上的环境标 reachable=false 并附原因。
    注意：返回的版本信息是 **Predixy 代理层**；后端 Redis 版本经代理不可见。
    """
    _assert_cmd("ping", "info", "scan")
    rows = [_conns().probe(name) for name in _settings().names()]
    payload = {
        "environments": rows,
        "count": len(rows),
        "aliases": {name: _env_of(name).aliases for name in _settings().names()
                    if _env_of(name).aliases},
        "note": ("版本信息为 Predixy 代理层；DBSIZE/SLOWLOG/MEMORY 等被代理拒绝，"
                 "键总数与精确内存不可得（bigkey_scan 用类型化长度近似）"),
    }
    return _ok(payload, source="config")


# --------------------------------------------------------------------------
# 键发现
# --------------------------------------------------------------------------
@mcp.tool()
@tool("scan_keys")
def scan_keys(env: str, pattern: str = "*", cursor: int = 0, count: int = 100) -> str:
    """用 SCAN 游标翻页枚举键（**绝不使用 KEYS**，不会阻塞实例）。

    pattern 为 Redis 通配（如 'www:SellCarProgress:*'）；count 是单轮建议返回量
    （代理不保证精确）。返回 keys 与 next_cursor，next_cursor=0 表示本轮枚举结束；
    继续翻页把 next_cursor 原样传回来即可。
    """
    conf = _env_of(env)
    guard = _settings().guard
    pat = check_pattern(pattern, guard.max_pattern_len)
    cur = clamp_int("cursor", cursor, 0, 2 ** 63 - 1)
    cnt = clamp_int("count", count or guard.default_count, 1, guard.max_count)
    _assert_cmd("scan")
    r = _conns().client(env)
    next_cursor, keys = r.scan(cursor=cur, match=pat, count=cnt)
    keys = list(keys)
    return _ok({
        **_env_brief(env),
        "pattern": pat,
        "cursor": cur,
        "count": cnt,
        "keys": keys,
        "returned": len(keys),
        "next_cursor": int(next_cursor or 0),
        "note": "SCAN 是渐进式迭代，next_cursor=0 表示本轮枚举结束"
                if int(next_cursor or 0) == 0
                else "还有后续批次，用 next_cursor=%d 继续" % int(next_cursor or 0),
    }, source="redis:%s" % conf.actual)


@mcp.tool()
@tool("find_keys")
def find_keys(env: str, pattern: str = "*", limit: int = 50,
              with_detail: bool = True) -> str:
    """按 pattern 收集一批键（内部循环 SCAN），可附带类型 / TTL / 类型化长度。

    找业务键用它（如 pattern='www:SellCarProgress:*'）。limit 上限见 guard.max_limit，
    扫描轮数也有上限（guard.max_scan_rounds），收不满时会明确告知还剩多少没扫。
    """
    conf = _env_of(env)
    guard = _settings().guard
    pat = check_pattern(pattern, guard.max_pattern_len)
    want = clamp_int("limit", limit, 1, guard.max_limit)

    _assert_cmd("scan", "type", "ttl")
    r = _conns().client(env)
    cursor = 0
    rounds = 0
    found: list[dict[str, Any]] = []
    while len(found) < want and rounds < guard.max_scan_rounds:
        cursor, keys = r.scan(cursor=cursor, match=pat, count=guard.default_count)
        for key in keys:
            if len(found) >= want:
                break
            if not with_detail:
                found.append({"key": key})
                continue
            try:
                ktype = str(r.type(key))
                ttl = int(r.ttl(key))
                found.append({"key": key, "type": ktype, "ttl": ttl,
                              "len": _len_of(r, key, ktype),
                              "len_unit": "bytes" if ktype == "string" else "elements"})
            except Exception:                           # noqa: BLE001 单键失败不影响整体
                found.append({"key": key})
        if int(cursor or 0) == 0:
            break
        rounds += 1

    done = int(cursor or 0) == 0
    note = ("扫描 %d 轮，枚举已结束" % rounds if done
            else "已达收集上限 %d 个（未扫完；用 scan_keys 从 cursor=%d 继续）"
                 % (want, int(cursor or 0)))
    return _ok({
        **_env_brief(env),
        "pattern": pat,
        "limit": want,
        "keys": found,
        "returned": len(found),
        "scan_rounds": rounds,
        "next_cursor": int(cursor or 0),
        "note": note,
    }, source="redis:%s" % conf.actual, truncated=not done, warning=None if done else note)


@mcp.tool()
@tool("key_detail")
def key_detail(env: str, key: str) -> str:
    """单个键的详情：存在性、类型、TTL、类型化长度。

    ⚠️ MEMORY USAGE 被 Predixy 拒绝，所以长度是"类型化近似"：
    string=字节数（STRLEN）、list/hash/set/zset=元素数。精确内存占用不可得。
    """
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    _assert_cmd("exists", "type", "ttl")
    r = _conns().client(env)
    if not r.exists(key):
        return _ok({**_env_brief(env), "key": key, "exists": False,
                    "note": "键不存在（注意 env 是否选对，以及是否已被 TTL 清理）"},
                   source="redis:%s" % conf.actual)
    ktype = str(r.type(key))
    ttl = int(r.ttl(key))
    return _ok({
        **_env_brief(env),
        "key": key,
        "exists": True,
        "type": ktype,
        "ttl_seconds": ttl,
        "ttl_display": "永不过期" if ttl == -1 else ("键不存在" if ttl == -2 else "%d 秒" % ttl),
        "len": _len_of(r, key, ktype),
        "len_unit": "bytes" if ktype == "string" else "elements",
        "note": "长度是类型化近似（MEMORY USAGE 被代理拒绝）",
    }, source="redis:%s" % conf.actual)


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------
@mcp.tool()
@tool("read_string")
def read_string(env: str, key: str, offset: int = 0, length: int = 2000) -> str:
    """读 string 键（GETRANGE 分段读，大 value 不会一次拉爆）。

    先用 key_detail 看 STRLEN 再定分段：本工具最多返回 guard.max_field_chars 个字符；
    没读完时用更大的 offset 继续读（返回里的 note 会说明是否到末尾）。
    """
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    off = clamp_int("offset", offset, 0, 2 ** 31 - 1)
    size = clamp_int("length", length, 1, guard.max_field_chars)
    _assert_cmd("strlen", "getrange")
    r = _conns().client(env)
    total = int(r.strlen(key))
    chunk = r.getrange(key, off, off + size - 1)
    text = chunk if isinstance(chunk, str) else str(chunk)
    returned = len(text.encode("utf-8", "replace"))
    reached_end = off + size >= total
    return _ok({
        **_env_brief(env),
        "key": key,
        "type": "string",
        "total_bytes": total,
        "offset": off,
        "requested_chars": size,
        "returned_bytes": returned,
        "value": text,
        "note": "已到末尾" if reached_end else "未读完，用 offset=%d 继续" % (off + size),
    }, source="redis:%s" % conf.actual, truncated=not reached_end)


@mcp.tool()
@tool("read_hash")
def read_hash(env: str, key: str, field: str = "") -> str:
    """读 hash 键：不传 field 返回前 N 个字段，传 field 只取该字段。

    实现用的是 **HLEN + HKEYS + 逐字段 HGET**（都不是写命令，且都在 Predixy 白名单内）：
    先看总字段数，再只对前 guard.max_members 个字段取值，避免一次把整张大 hash 拉出来。
    ⚠️ `HSCAN` 不在 Predixy 白名单内，**不支持按游标翻字段**；字段很多时请用 field 精确取。
    """
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    _assert_cmd("hlen", "hkeys", "hget")
    r = _conns().client(env)
    hlen = int(r.hlen(key))
    if field:
        return _ok({**_env_brief(env), "key": key, "type": "hash", "field": field,
                    "value": r.hget(key, field), "total_fields": hlen},
                   source="redis:%s" % conf.actual)
    names = list(r.hkeys(key))[:guard.max_members]
    pairs = [{"field": n, "value": r.hget(key, n)} for n in names]
    pairs, note, truncated = cap_items(pairs, guard.max_members, "个字段")
    return _ok({
        **_env_brief(env),
        "key": key,
        "type": "hash",
        "total_fields": hlen,
        "fields": pairs,
        "returned": len(pairs),
        "note": note,
    }, source="redis:%s" % conf.actual, truncated=truncated, warning=note if truncated else None)


@mcp.tool()
@tool("read_list")
def read_list(env: str, key: str, start: int = 0, stop: int = 9) -> str:
    """读 list 键的一段元素（LRANGE，含两端）。

    先 key_detail 看 LLEN 再定区间；支持负数下标（-1 为末尾）。
    单次最多返回 guard.max_members 个元素。
    """
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    s, e = check_range("read_list", start, stop, guard.max_members)
    _assert_cmd("llen", "lrange")
    r = _conns().client(env)
    llen = int(r.llen(key))
    items = list(r.lrange(key, s, e))
    return _ok({
        **_env_brief(env),
        "key": key,
        "type": "list",
        "total_items": llen,
        "range": [s, e],
        "items": items,
        "returned": len(items),
        "note": "空列表" if llen == 0 else "支持负数下标（-1 为末尾）",
    }, source="redis:%s" % conf.actual)


@mcp.tool()
@tool("read_set")
def read_set(env: str, key: str) -> str:
    """读 set 键的成员（排序后返回，最多 guard.max_members 个）与基数。"""
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    _assert_cmd("scard", "smembers")
    r = _conns().client(env)
    scard = int(r.scard(key))
    members, note, truncated = cap_items(sorted(r.smembers(key)), guard.max_members, "个成员")
    return _ok({
        **_env_brief(env),
        "key": key,
        "type": "set",
        "cardinality": scard,
        "members": members,
        "returned": len(members),
        "note": note,
    }, source="redis:%s" % conf.actual, truncated=truncated, warning=note if truncated else None)


@mcp.tool()
@tool("read_zset")
def read_zset(env: str, key: str, start: int = 0, stop: int = 9,
              with_scores: bool = True) -> str:
    """读 zset 键按**排名**区间的一段成员（ZRANGE，可带分数）。

    start/stop 是排名（0 为第一名，支持负数）。按**分数区间**读请用 read_zset_by_score。
    """
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    s, e = check_range("read_zset", start, stop, guard.max_members)
    _assert_cmd("zcard", "zrange")
    r = _conns().client(env)
    zcard = int(r.zcard(key))
    raw = list(r.zrange(key, s, e, withscores=bool(with_scores)))
    rows: list[dict[str, Any]] = []
    for item in raw:
        if bool(with_scores) and isinstance(item, (tuple, list)) and len(item) == 2:
            rows.append({"member": item[0], "score": item[1]})
        else:
            rows.append({"member": item})
    return _ok({
        **_env_brief(env),
        "key": key,
        "type": "zset",
        "total_members": zcard,
        "rank_range": [s, e],
        "with_scores": bool(with_scores),
        "members": rows,
        "returned": len(rows),
    }, source="redis:%s" % conf.actual)


@mcp.tool()
@tool("read_zset_by_score")
def read_zset_by_score(env: str, key: str, min_score: str = "-inf",
                       max_score: str = "+inf", limit: int = 50) -> str:
    """按**分数区间**读 zset 成员（ZRANGEBYSCORE）。

    min_score / max_score 接受数字、`-inf` / `+inf`、以及 `(数字` 表示开区间；
    limit 上限为 guard.max_members。
    """
    conf = _env_of(env)
    guard = _settings().guard
    key = check_key(key, guard.max_key_len)
    lo = check_score("min_score", min_score)
    hi = check_score("max_score", max_score)
    n = clamp_int("limit", limit, 1, guard.max_members)
    _assert_cmd("zrangebyscore")
    r = _conns().client(env)
    raw = list(r.zrangebyscore(key, lo, hi, start=0, num=n, withscores=True))
    rows = [{"member": m, "score": s} for m, s in
            [((i[0], i[1]) if isinstance(i, (tuple, list)) else (i, None)) for i in raw]]
    return _ok({
        **_env_brief(env),
        "key": key,
        "type": "zset",
        "score_range": [lo, hi],
        "limit": n,
        "members": rows,
        "returned": len(rows),
        "note": "仅返回分数区间内前 %d 个" % n,
    }, source="redis:%s" % conf.actual, truncated=len(rows) >= n,
       warning=None if len(rows) < n else "已达 limit=%d，可能还有更多成员" % n)


# --------------------------------------------------------------------------
# 排查
# --------------------------------------------------------------------------
@mcp.tool()
@tool("bigkey_scan")
def bigkey_scan(env: str, pattern: str = "*", sample: int = 500, top: int = 20) -> str:
    """大键采样扫描：SCAN 采样 + 类型化长度排序取 Top N。

    ⚠️ MEMORY USAGE 被代理拒绝，**内存级精确大小不可得**：这里 string 按字节数、
    集合类按元素数近似排序。经验阈值参考：string > 10KB、集合类 > 5000 元素。
    只采样不扫全库，sample 上限见 guard.max_sample。
    """
    conf = _env_of(env)
    guard = _settings().guard
    pat = check_pattern(pattern, guard.max_pattern_len)
    want = clamp_int("sample", sample, 10, guard.max_sample)
    top_n = clamp_int("top", top, 1, guard.max_top)

    _assert_cmd("scan", "type")
    r = _conns().client(env)
    cursor = 0
    sampled = 0
    rounds = 0
    stats: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    while sampled < want and rounds < guard.max_scan_rounds:
        cursor, keys = r.scan(cursor=cursor, match=pat, count=guard.default_count)
        for key in keys:
            if sampled >= want:
                break
            try:
                ktype = str(r.type(key))
            except Exception:                           # noqa: BLE001
                continue
            stats[ktype] = stats.get(ktype, 0) + 1
            rows.append({"key": key, "type": ktype, "len": _len_of(r, key, ktype),
                         "len_unit": "bytes" if ktype == "string" else "elements"})
            sampled += 1
        if int(cursor or 0) == 0:
            break
        rounds += 1

    measured = [x for x in rows if x["len"] is not None]
    measured.sort(key=lambda x: x["len"], reverse=True)
    return _ok({
        **_env_brief(env),
        "pattern": pat,
        "sampled": sampled,
        "scan_rounds": rounds,
        "type_distribution": stats,
        "top": measured[:top_n],
        "unmeasured": len(rows) - len(measured),
        "note": "基于 %d 个采样键；len 单位：string=bytes，其余=elements（非精确内存）" % sampled,
    }, source="redis:%s" % conf.actual)


# --------------------------------------------------------------------------
# HTTP：准入令牌闸门 + /healthz
# --------------------------------------------------------------------------
async def _send_json(send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class TokenMiddleware:
    """唯一一层鉴权：X-REDIS-RO-MCP-TOKEN 缺失/错误 → 403，且早于 MCP 协议处理。

    同时兼任三件事：
      1. 解析并绑定调用方身份（identity.py）到 contextvar，供审计使用；
      2. 鉴权拒绝写一行 `auth_denied` 审计（带 client_ip / agent_id）；
      3. 对 POST 且 HTTP >= 400 的响应做兜底留痕（协议层拒绝，工具根本没被调用）。
    """

    def __init__(self, app, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        auth = settings.auth
        self._enabled = auth.enabled
        self._headers = auth.accepted_headers()
        self._token = (auth.token or "").strip()
        self._header_hint = auth.header or (self._headers[0] if self._headers
                                           else "X-REDIS-RO-MCP-TOKEN")
        self._path = settings.server.path or "/mcp"

    def _observe(self, send, ident: dict[str, Any], path: str, method: str):
        """观测响应状态码；只读不改写，任何异常都不影响请求。"""

        async def wrapped(message):
            try:
                if message.get("type") == "http.response.start":
                    status = int(message.get("status") or 0)
                    if (method == "POST" and status >= 400
                            and not _CTX_AUDITED.get()
                            and getattr(self._settings.audit, "log_http_errors", True)):
                        _CTX_AUDITED.set(True)
                        _audit(
                            event="http_error", tool="", outcome=
                            "error" if status >= 500 else "invalid_request",
                            reject=A.make_reject(
                                "PROTO_REJECTED",
                                "MCP 协议层/路由层拒绝了请求（HTTP %d），工具未被调用；"
                                "多为参数不符合工具签名或路由不存在" % status,
                                {"http_status": status, "method": method, "path": path}),
                            detail={"http_status": status, "path": path, "method": method},
                        )
            except Exception:                            # noqa: BLE001
                log.warning("http_error 审计失败", exc_info=True)
            await send(message)

        return wrapped

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            I.bind({})
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        ident = I.resolve(scope, headers, self._settings.identity)
        _CTX_IDENT.set(ident)
        _CTX_AUDITED.set(False)
        I.bind(ident)

        # /healthz 免鉴权（只给版本与配置回显，不含任何密钥）
        if method in ("GET", "HEAD") and path.rstrip("/") in ("/healthz", "/health"):
            conf = self._settings
            await _send_json(send, 200, {
                "ok": True,
                "service": conf.server.name,
                "display_name": conf.server.display_name,
                "version": _VERSION,
                "transport": "http",
                # Redis 侧：只回显地址与库序号，**不回显口令**（含是否已配口令的布尔）
                "environments": [
                    {"env": name, "env_actual": env.actual,
                     "display_name": env.display_name, "endpoint": env.endpoint,
                     "auth": bool(env.has_auth), "aliases": env.aliases,
                     "protocol": env.protocol}
                    for name, env in sorted(conf.environments.items())
                ],
                "auth": {"header": conf.auth.header, "enabled": conf.auth.enabled},
                # 网关透传身份：只回显配置的头名与可信代理，便于现场核对是否生效
                "identity": {
                    "enabled": bool(conf.identity.record),
                    "agent_id_headers": list(conf.identity.agent_id_headers or []),
                    "client_ip_headers": list(conf.identity.client_ip_headers or []),
                    "trusted_proxies": list(conf.identity.trusted_proxies or []),
                },
                "guard": {
                    "max_count": conf.guard.max_count,
                    "max_members": conf.guard.max_members,
                    "max_limit": conf.guard.max_limit,
                    "max_sample": conf.guard.max_sample,
                    "max_field_chars": conf.guard.max_field_chars,
                    "max_bytes": conf.guard.max_bytes,
                    "socket_timeout_ms": conf.guard.socket_timeout_ms,
                    "allowed_commands": sorted(conf.guard.allowed_commands),
                    "proxy_rejected_commands": sorted(conf.guard.proxy_rejected_commands),
                },
                "audit": {"dir": conf.audit.dir, "prefix": conf.audit.filename_prefix},
            })
            return

        if not self._enabled:
            await self._app(scope, receive, self._observe(send, ident, path, method))
            return

        if path == self._path or path == self._path + "/" or path.startswith(self._path + "/"):
            _hdr, provided = pick_header(headers, self._headers)
            if not token_ok(provided, self._token):
                _CTX_AUDITED.set(True)
                code = "TOKEN_MISSING" if not provided else "TOKEN_INVALID"
                text = ("缺少服务准入令牌（请求头 %s）" % self._header_hint
                        if not provided else "服务准入令牌无效")
                _audit(
                    event="auth_denied", tool="gateway_token", outcome="denied",
                    reject=A.make_reject(code, text, {
                        "path": path, "method": method,
                        "via": "middleware", "header": _hdr or self._header_hint,
                    }),
                    detail={"reason": "missing" if not provided else "invalid", "path": path},
                )
                await _send_json(send, 403, {"ok": False, "error": {
                    "code": "FORBIDDEN",
                    "message": "缺少或无效的服务令牌（请求头 %s）" % self._header_hint,
                }})
                return

        await self._app(scope, receive, self._observe(send, ident, path, method))


def build_http_app(settings: Settings):
    """构建 ASGI app：TokenMiddleware(最外) → IdentityCapture(兜底) → FastMCP。

    实测 fastmcp 2.11.2：只有 `FastMCP.http_app(path=...)`，没有 sse_app；
    不要用 Starlette 的 `middleware=[...]` 参数（它要求 (cls, args, kwargs) 元组，
    传裸类会在请求阶段抛 TypeError），所以这里手写 ASGI 类层层包。
    """
    factory = getattr(mcp, "http_app", None) or getattr(mcp, "streamable_http_app", None)
    if factory is None:
        return None
    for kwargs in ({"path": settings.server.path}, {}):
        try:
            # 注意：这里传的是 settings.identity（IdentityConfig），不是 settings。
            # 传错会让 trusted_proxies 取不到，把外层已绑定的正确身份降级成"非可信"。
            inner = I.IdentityCaptureMiddleware(factory(**kwargs), settings.identity)
            return TokenMiddleware(inner, settings)
        except TypeError:
            continue
    return None


# --------------------------------------------------------------------------
# 入口（唯一入口：python -m redis_ro_mcp.server）
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redis-ro-mcp",
        description="DBA Redis 只读 MCP（单服务多环境：dev / test / stage=mirror）",
    )
    parser.add_argument("--config", default=None, help="config/environments.yaml 路径")
    parser.add_argument("--transport", default=os.environ.get("REDIS_MCP_TRANSPORT", "stdio"),
                        choices=["stdio", "http"], help="stdio=本机调试；http=共享部署")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--version", action="version", version="redis-ro-mcp %s" % _VERSION)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("REDIS_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = init_state(args.config)
    except ConfigError as exc:
        log.error("配置加载失败: %s", exc)
        return 2

    log.info(
        "redis-ro-mcp %s 启动: envs=%s auth=gateway header=%s audit_dir=%s "
        "identity=%s trusted_proxies=%s protocol=%s",
        _VERSION, settings.names(), settings.auth.header, settings.audit.dir,
        bool(settings.identity.record), settings.identity.trusted_proxies,
        settings.environments[settings.names()[0]].protocol if settings.environments else "?",
    )
    for name in settings.names():
        conf = settings.environments[name]
        log.info("  环境 %s → 实际 %s（%s）%s，别名 %s，auth=%s", name, conf.actual,
                 conf.display_name, conf.endpoint, conf.aliases or "无",
                 "yes" if conf.has_auth else "no")

    # 启动即写一行审计：谁、哪台机器、什么时候起的服务
    _audit(event="startup", tool="server", outcome="ok",
           detail={"version": _VERSION, "transport": args.transport,
                   "envs": settings.names()})

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    host = args.host or settings.server.host
    port = args.port or settings.server.port
    app = build_http_app(settings)
    if app is None:
        log.warning("未取到 ASGI app，回退到 SDK 内置 http 服务（身份抓取走 SDK 通道）")
        mcp.run(transport="http", host=host, port=port, path=settings.server.path)
        return 0

    import uvicorn

    # proxy_headers=False 是**刻意**的：uvicorn 默认会按 X-Forwarded-For 改写
    # scope["client"]（默认只信 127.0.0.1）。一旦网关与本服务同机或经本地 nginx
    # 转发，scope["client"] 就会变成真实调用方 IP，而 identity.py 用它判定
    # "直连方是否可信代理"，于是所有请求都会被判成 identity_trusted=false、
    # agent_id 被清空——透传白做。关掉它，TCP 对端永远是网关的真实 IP，
    # 可信判定只由 identity.trusted_proxies 一处决定。
    uvicorn.run(app, host=host, port=port, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
