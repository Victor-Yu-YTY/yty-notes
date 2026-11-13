"""mongodb-ro-mcp 服务入口：13 个只读工具 + 准入令牌闸门 + 身份审计 + /healthz。

## 这个服务是什么

一个进程覆盖线下三套 MongoDB（`dev` 开发 / `test` 测试 / `stage` 模拟，
**`stage` 的实际实例是内部 `mirror` 副本集，`mirror`/`sim` 作别名**），工具第一个参数
一律是 `env`。目标实例是 **MongoDB 4.0.20（wire version 7）**，所以：

* 不用 `hello` 命令（4.2+ 才有），一律 `isMaster`；
* 不用 `$$NOW` 等 4.2+ 聚合变量；
* `pymongo` 锁 4.6.x（4.9+ 要求 wire 8、4.19+ 要求 wire 9，装上直接连不上）。

## 只读三层

| 层 | 措施 |
| --- | --- |
| 接口层 | 工具面只登记读操作，**没有** `execute_command` / 通用脚本执行入口 |
| 语义层 | 管道/过滤条件递归检查 `$out`/`$merge`/`$where`/`$function`/`$accumulator`（mongo_guard.py） |
| 资源层 | `maxTimeMS`、单次条数、单字段字符、响应字节上限、聚合禁落盘 |

> 数据库侧的只读账号（`read` / `readAnyDatabase` + 巡检用的 `clusterMonitor`）
> 才是最终边界，MCP 层只是第一道闸。

## 鉴权与审计（与 db-read-mcp v1.0.0 / sql-review-* v1.0.1 同口径）

* **只有一层鉴权**：准入令牌 `X-MONGODB-RO-MCP-TOKEN`，缺失/错误 → HTTP 403，
  且早于 MCP 协议层；令牌为空时**拒绝启动**；
* **调用方身份只用于审计**：网关透传的 `X-Agent-Id` / `X-Forwarded-For`
  由 identity.py 解析后落进每行审计（`agent_id` / `client_ip` / `socket_ip` /
  `*_source` / `identity_trusted`），**不参与任何放行判定**；
* 每个工具出口、鉴权拒绝、协议层拒绝、启动各写一行 JSONL，未直接放行的结论
  都带 `reject{code,stage,text,evidence}`。

启动：
    python -m mongodb_ro_mcp.server --transport stdio            # 本机调试
    python -m mongodb_ro_mcp.server --transport http --port 8040  # 218 共享部署
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

from fastmcp import FastMCP
from pymongo.errors import OperationFailure, PyMongoError

from . import __version__ as _VERSION
from . import audit as A
from . import identity as I
from .auth import pick_header, token_ok
from .config import ConfigError, MongoEnv, Settings, UnknownEnvironment, load_settings
from .mongo_guard import (
    GuardError,
    check_db_allowed,
    check_ident,
    clamp_bytes,
    dumps,
    guard_filter,
    guard_pipeline,
    load_json,
    summarize_docs,
)
from .pool import MongoManager

log = logging.getLogger("mongodb_ro_mcp.server")

_STATE: dict[str, Any] = {}
mcp = FastMCP("mongodb-ro-mcp")

# 单次请求上下文（审计用）
_CTX_IDENT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "mongo_ctx_ident", default={}
)
_CTX_AUDITED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mongo_ctx_audited", default=False
)
_STARTED: contextvars.ContextVar[float] = contextvars.ContextVar(
    "mongo_ctx_started", default=0.0
)


# --------------------------------------------------------------------------
# 状态初始化
# --------------------------------------------------------------------------
def init_state(config_path: str | None = None) -> Settings:
    if "settings" in _STATE:
        return _STATE["settings"]
    settings = load_settings(config_path or os.environ.get("MONGO_MCP_CONFIG"))
    _STATE["settings"] = settings
    _STATE["conns"] = MongoManager(settings)
    _STATE["policy"] = A.AuditPolicy.from_config(settings.audit)
    return settings


def _settings() -> Settings:
    return _STATE.get("settings") or init_state()


def _conns() -> MongoManager:
    _settings()
    return _STATE["conns"]


def _policy() -> A.AuditPolicy:
    _settings()
    return _STATE["policy"]


# --------------------------------------------------------------------------
# 统一返回信封
# --------------------------------------------------------------------------
def _elapsed_ms() -> float:
    start = _STARTED.get()
    return round((time.perf_counter() - start) * 1000, 1) if start else 0.0


def _ok(payload: dict[str, Any], *, source: str = "mongo", truncated: bool = False,
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


def _env_of(name: str) -> MongoEnv:
    return _settings().env(name)


def _env_brief(name: str) -> dict[str, Any]:
    """响应里的环境回显：env 恒为**规范名**（dev/test/stage），
    传了别名时另附 env_alias，方便调用方与审计对齐。"""
    conf = _env_of(name)
    brief = {"env": conf.name, "env_actual": conf.actual,
             "display_name": conf.display_name}
    if name and name != conf.name:
        brief["env_alias"] = name
    return brief


def _clamp_limit(limit: int) -> int:
    guard = _settings().guard
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise GuardError("ARG_LIMIT_INVALID", "limit 必须是整数，收到 %r" % (limit,)) from None
    return max(1, min(value, guard.max_limit))


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
           env_actual: str | None = None, database: str | None = None,
           collection: str | None = None, query: Any = None,
           detail: dict | None = None) -> None:
    settings = _settings()
    policy = _policy()

    # 审计里的 env 恒为规范名（dev/test/stage）；调用方传别名时把原名塞进 detail
    if env:
        try:
            conf = settings.env(env)
            if env != conf.name:
                detail = {**(detail or {}), "env_alias": env}
            env, env_actual = conf.name, env_actual or conf.actual
        except Exception:                                  # noqa: BLE001 未知环境保留原值
            pass

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
            collection=collection,
            query_sha256=sha,
            query_len=length,
            query_text=text,
            detail=detail,
            **_ident_kw(),
        )
    except Exception:                                  # noqa: BLE001 审计失败不影响业务
        log.warning("审计写入失败", exc_info=True)


# --------------------------------------------------------------------------
# 工具装饰器：统一计时 + 异常兜底 + 审计
# --------------------------------------------------------------------------
def tool(tool_name: str) -> Callable:
    """统一出口处理。

    注意：被装饰函数签名里**不要**出现带默认值的内部参数 —— MCP SDK 会把默认值
    一起当成调用参数传进来（db-read-mcp 踩过的坑），计时改用 contextvar。
    """

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            _STARTED.set(time.perf_counter())
            settings = _settings()
            env_arg = kwargs.get("env")
            database = kwargs.get("database")
            collection = kwargs.get("collection")
            raw_query = kwargs.get("pipeline") or kwargs.get("filter") or ""
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
                       env=env_arg, database=database, collection=collection,
                       query=raw_query)
                return out
            except UnknownEnvironment as exc:
                reject = A.make_reject("ARG_ENV_UNKNOWN", str(exc))
                _audit(tool=tool_name, outcome="invalid_request", reject=reject,
                       env=str(env_arg or ""), database=database, collection=collection)
                return _err("ENV_NOT_FOUND", str(exc), available=settings.names())
            except GuardError as exc:
                reject = A.make_reject(exc.code, exc.message, exc.evidence)
                _audit(tool=tool_name, outcome="invalid_request", reject=reject,
                       env=str(env_arg or ""), database=database, collection=collection)
                extra = {"evidence": exc.evidence} if exc.evidence else {}
                return _err(exc.code, exc.message, **extra)
            except OperationFailure as exc:
                code = int(getattr(exc, "code", 0) or 0)
                if code in (13, 18):
                    reject = A.make_reject("DEP_PERMISSION_DENIED",
                                           str(exc).split(", full error")[0])
                    _audit(tool=tool_name, outcome="denied", reject=reject,
                           env=str(env_arg or ""), detail={"mongo_code": code})
                    return _err("PERMISSION_DENIED", reject["text"],
                                hint='在 admin 库执行 db.grantRolesToUser("<账号>", '
                                     '[{role:"clusterMonitor", db:"admin"}]) 后重试')
                reject = A.make_reject("INTERNAL_EXCEPTION",
                                       "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""))
                return _err("TOOL_ERROR", "%s: %s" % (type(exc).__name__, exc))
            except PyMongoError as exc:
                reject = A.make_reject("DEP_MONGO_UNREACHABLE",
                                       "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""))
                return _err("MONGO_UNREACHABLE", "%s: %s" % (type(exc).__name__, exc),
                            hint="确认目标副本集可达、账号口令正确（见 scripts/checks/precheck.py）")
            except Exception as exc:                    # noqa: BLE001 最后兜底
                log.exception("tool=%s 执行失败", tool_name)
                reject = A.make_reject("INTERNAL_EXCEPTION",
                                       "%s: %s" % (type(exc).__name__, exc))
                _audit(tool=tool_name, outcome="error", reject=reject,
                       env=str(env_arg or ""))
                return _err("TOOL_ERROR", "%s: %s" % (type(exc).__name__, exc))

        return wrapper

    return deco


# --------------------------------------------------------------------------
# 环境发现
# --------------------------------------------------------------------------
@mcp.tool()
@tool("list_environments")
def list_environments() -> str:
    """列出本 MCP 覆盖的全部 MongoDB 环境（含连通性、副本集名、主节点、服务端版本）。

    其余工具的 env 参数从这里取值：规范名是 dev / test / stage，
    stage 的实际实例是内部 mirror 副本集（mirror / sim 也可直接传）。
    任何一步失败都不会中断，连不上的环境标 reachable=false 并附原因。
    """
    rows = [_conns().probe(name) for name in _settings().names()]
    payload = {
        "environments": rows,
        "count": len(rows),
        "aliases": {name: _env_of(name).aliases for name in _settings().names()
                    if _env_of(name).aliases},
    }
    return _ok(payload, source="config")


# --------------------------------------------------------------------------
# 元数据工具
# --------------------------------------------------------------------------
@mcp.tool()
@tool("list_databases")
def list_databases(env: str) -> str:
    """列出指定环境当前账号可见的数据库及其占用空间。

    env 取 dev / test / stage（stage 落到内部 mirror 实例）。只返回库清单与容量，不含表数据。
    """
    conf = _env_of(env)
    rows = []
    for item in _conns().client(env).list_databases():
        rows.append({
            "name": item.get("name"),
            "size_mb": round(item.get("sizeOnDisk", 0) / 1048576, 2),
            "empty": item.get("empty"),
        })
    return _ok({**_env_brief(env), "databases": rows, "count": len(rows)},
               source="mongo:%s" % conf.actual)


@mcp.tool()
@tool("list_collections")
def list_collections(env: str, database: str, include_system: bool = False) -> str:
    """列出库内集合，附带文档数与磁盘占用。

    include_system=True 时才包含 system.* 集合。单个集合取统计失败不影响整个列表。
    """
    _env_of(env)
    check_ident("database", database)
    check_db_allowed(database, _settings().guard.forbidden_dbs)
    rows = []
    for info in _conns().db(env, database).list_collections():
        name = info.get("name", "")
        if not include_system and name.startswith("system."):
            continue
        row: dict[str, Any] = {"name": name, "type": info.get("type", "collection")}
        if info.get("type", "collection") == "collection":
            try:
                stats = _conns().db(env, database).command("collStats", name)
                row["count"] = stats.get("count")
                row["size_mb"] = round(stats.get("size", 0) / 1048576, 2)
                row["storage_mb"] = round(stats.get("storageSize", 0) / 1048576, 2)
                row["indexes"] = stats.get("nindexes")
            except Exception as exc:                # noqa: BLE001 权限不足时降级
                row["error"] = str(exc)[:200]
        rows.append(row)
    return _ok({**_env_brief(env), "database": database, "collections": rows,
                "count": len(rows)}, source="mongo")


@mcp.tool()
@tool("collection_schema")
def collection_schema(env: str, database: str, collection: str, sample_size: int = 100) -> str:
    """抽样推断集合字段结构：字段名、出现率、类型分布。排查脏数据与字段漂移用。

    sample_size 最大 1000，仅抽样不扫全表；集合为空时直接说明。
    """
    _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)
    size = max(1, min(int(sample_size), 1000))
    cursor = _conns().coll(env, database, collection).find({}, limit=size)
    ms = _settings().guard.timeout_ms
    try:
        cursor = cursor.max_time_ms(ms)
    except AttributeError:                          # 驱动版本差异兜底
        pass
    docs = list(cursor)
    if not docs:
        return _ok({**_env_brief(env), "database": database, "collection": collection,
                    "sampled": 0, "fields": []}, warning="集合为空或当前账号不可见")

    fields: dict[str, dict] = {}
    for doc in docs:
        for key, value in doc.items():
            entry = fields.setdefault(key, {"field": key, "present": 0, "types": {}})
            entry["present"] += 1
            tname = type(value).__name__
            entry["types"][tname] = entry["types"].get(tname, 0) + 1
    rows = [
        {
            "field": entry["field"],
            "present_pct": "%d%%" % round(entry["present"] * 100 / len(docs)),
            "types": entry["types"],
        }
        for entry in sorted(fields.values(), key=lambda x: -x["present"])
    ]
    return _ok({**_env_brief(env), "database": database, "collection": collection,
                "sampled": len(docs), "fields": rows}, source="mongo")


@mcp.tool()
@tool("list_indexes")
def list_indexes(env: str, database: str, collection: str) -> str:
    """列出集合索引定义及使用次数（$indexStats），用于识别无用索引。

    ops_since_start 为实例启动以来的访问次数；取不到时不影响索引定义本身。
    """
    _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)
    coll = _conns().coll(env, database, collection)
    indexes = [
        {
            "name": item.get("name"),
            "key": item.get("key"),
            "unique": item.get("unique", False),
            "sparse": item.get("sparse", False),
        }
        for item in coll.list_indexes()
    ]
    usage: dict[str, Any] = {}
    try:
        for stat in coll.aggregate([{"$indexStats": {}}],
                                   maxTimeMS=_settings().guard.timeout_ms):
            usage[stat.get("name")] = stat.get("accesses", {}).get("ops", 0)
    except Exception as exc:                        # noqa: BLE001
        usage = {"_error": str(exc)[:200]}
    for item in indexes:
        item["ops_since_start"] = usage.get(item["name"], "n/a")
    return _ok({**_env_brief(env), "database": database, "collection": collection,
                "indexes": indexes, "count": len(indexes),
                "note": "ops_since_start 为实例启动以来的访问次数"},
               source="mongo")


@mcp.tool()
@tool("collection_stats")
def collection_stats(env: str, database: str, collection: str) -> str:
    """集合存储统计：文档数、逻辑大小、物理大小、压缩率、索引大小。"""
    _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)
    stats = _conns().db(env, database).command("collStats", collection)
    size = stats.get("size", 0)
    storage = stats.get("storageSize", 0)
    return _ok({
        **_env_brief(env),
        "database": database,
        "collection": collection,
        "ns": stats.get("ns"),
        "count": stats.get("count"),
        "size_mb": round(size / 1048576, 2),
        "storage_mb": round(storage / 1048576, 2),
        "compression_ratio": round(size / storage, 2) if storage else None,
        "index_size_mb": round(stats.get("totalIndexSize", 0) / 1048576, 2),
        "nindexes": stats.get("nindexes"),
        "avg_obj_bytes": stats.get("avgObjSize"),
    }, source="mongo")


# --------------------------------------------------------------------------
# 数据查询工具
# --------------------------------------------------------------------------
@mcp.tool()
@tool("find_documents")
def find_documents(env: str, database: str, collection: str, filter: str = "{}",
                   projection: str = "", sort: str = "", limit: int = 20,
                   skip: int = 0) -> str:
    """按条件查询文档（只读）。

    filter / projection / sort 传 **JSON 字符串**，例如
    filter='{"status":"fail"}'、sort='{"_id":-1}'、projection='{"_id":0,"order_no":1}'。
    默认最多 20 条（上限见 guard.max_limit）；被禁止的算子（$where 等）会被拒绝。
    """
    conf = _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)

    flt = load_json(filter, field="filter", default={}) or {}
    guard_filter(flt, _settings().guard.forbidden_operators)
    proj = load_json(projection, field="projection", default=None)
    srt = load_json(sort, field="sort", default=None)
    if srt is not None and not isinstance(srt, dict):
        raise GuardError("ARG_JSON_INVALID", "sort 必须是 JSON 对象，如 '{\"_id\":-1}'")
    if proj is not None and not isinstance(proj, (dict, list)):
        raise GuardError("ARG_JSON_INVALID", "projection 必须是 JSON 对象/数组")

    guard = _settings().guard
    n = _clamp_limit(limit)
    cursor = _conns().coll(env, database, collection).find(
        flt, proj, skip=max(0, int(skip)), limit=n + 1
    )
    try:
        cursor = cursor.max_time_ms(guard.timeout_ms)
    except AttributeError:
        pass
    if srt:
        cursor = cursor.sort(list(srt.items()))
    docs, note, truncated = summarize_docs(list(cursor), n)
    return _ok({**_env_brief(env), "database": database, "collection": collection,
                "docs": docs, "returned": len(docs), "limit": n, "skip": max(0, int(skip)),
                "note": note}, source="mongo:%s" % conf.actual, truncated=truncated,
               warning=note if truncated else None)


@mcp.tool()
@tool("aggregate")
def aggregate(env: str, database: str, collection: str, pipeline: str, limit: int = 20) -> str:
    """执行只读聚合管道（pipeline 传 JSON 数组字符串）。

    禁止 $out / $merge（落盘写库）与 $where / $function / $accumulator（服务端 JS），
    递归检查到任意深度；服务端默认禁止落盘（allowDiskUse=false）。
    末尾会自动补一条 $limit 限制返回条数，不影响管道本身的语义。
    """
    _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)

    stages = load_json(pipeline, field="pipeline", default=None)
    if not isinstance(stages, list):
        raise GuardError("ARG_PIPELINE_NOT_LIST",
                         "pipeline 必须是 JSON 数组，例如 '[{\"$match\":{}}]'")
    guard = _settings().guard
    guard_pipeline(stages, guard.forbidden_operators)

    n = _clamp_limit(limit)
    cursor = _conns().coll(env, database, collection).aggregate(
        stages + [{"$limit": n + 1}],
        maxTimeMS=guard.timeout_ms,
        allowDiskUse=bool(guard.allow_disk_use),
    )
    docs, note, truncated = summarize_docs(list(cursor), n)
    return _ok({**_env_brief(env), "database": database, "collection": collection,
                "docs": docs, "returned": len(docs), "limit": n, "note": note},
               source="mongo", truncated=truncated,
               warning=note if truncated else None)


@mcp.tool()
@tool("count_documents")
def count_documents(env: str, database: str, collection: str, filter: str = "{}") -> str:
    """统计符合条件的文档数（filter 传 JSON 字符串）。

    请勿在大集合上不带条件调用：这里不限制条件，但会受 maxTimeMS 保护。
    """
    _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)
    flt = load_json(filter, field="filter", default={}) or {}
    guard_filter(flt, _settings().guard.forbidden_operators)
    total = _conns().coll(env, database, collection).count_documents(
        flt, maxTimeMS=_settings().guard.timeout_ms
    )
    return _ok({**_env_brief(env), "database": database, "collection": collection,
                "filter_count": total}, source="mongo")


@mcp.tool()
@tool("explain")
def explain(env: str, database: str, collection: str, filter: str = "{}",
            verbosity: str = "executionStats") -> str:
    """查看查询执行计划，判断是否走索引、扫描了多少文档。

    verbosity 可选 queryPlanner / executionStats / allPlansExecution。
    默认只回摘要（stage、命中索引、扫描量、耗时），要看完整计划请用 queryPlanner。
    """
    _env_of(env)
    check_ident("database", database)
    check_ident("collection", collection)
    check_db_allowed(database, _settings().guard.forbidden_dbs)
    if verbosity not in ("queryPlanner", "executionStats", "allPlansExecution"):
        raise GuardError("ARG_IDENT_INVALID",
                         "verbosity 只能是 queryPlanner / executionStats / allPlansExecution")
    flt = load_json(filter, field="filter", default={}) or {}
    guard_filter(flt, _settings().guard.forbidden_operators)
    plan = _conns().db(env, database).command(
        "explain", {"find": collection, "filter": flt}, verbosity=verbosity
    )
    stats = plan.get("executionStats", {}) or {}
    winning = (plan.get("queryPlanner") or {}).get("winningPlan") or {}
    return _ok({
        **_env_brief(env),
        "database": database,
        "collection": collection,
        "winning_stage": winning.get("stage"),
        "index_used": _extract_index(winning),
        "n_returned": stats.get("nReturned"),
        "docs_examined": stats.get("totalDocsExamined"),
        "keys_examined": stats.get("totalKeysExamined"),
        "exec_ms": stats.get("executionTimeMillis"),
        "full_plan": plan if verbosity == "queryPlanner" else "已省略，需要完整计划请用 queryPlanner",
    }, source="mongo")


def _extract_index(node: Any) -> str:
    """在 winplan 里向下找 IXSCAN 的索引名；没走索引时明确说 COLLSCAN。"""
    seen = 0
    while isinstance(node, dict) and seen < 20:
        seen += 1
        if node.get("stage") == "IXSCAN":
            return str(node.get("indexName"))
        nxt = node.get("inputStage") or (node.get("inputStages") or [None])[0]
        if not isinstance(nxt, dict):
            break
        node = nxt
    return "COLLSCAN（未使用索引）" if isinstance(node, dict) and node.get("stage") == "COLLSCAN" else "n/a"


# --------------------------------------------------------------------------
# 巡检工具（走 admin 库内部代码路径，不经 forbidden_dbs 检查）
# --------------------------------------------------------------------------
@mcp.tool()
@tool("current_operations")
def current_operations(env: str, min_running_ms: int = 1000, limit: int = 20) -> str:
    """查看当前正在执行的操作，按耗时倒序。定位长事务 / 慢查询 / 锁等待用。

    需要 admin 库的 clusterMonitor 只读监控角色（不含任何写权限）。
    """
    _env_of(env)
    n = max(1, min(int(limit), 50))
    min_sec = max(0, int(min_running_ms) // 1000)
    ops = []
    cursor = _conns().db(env, "admin").aggregate(
        [
            {"$currentOp": {"allUsers": True, "idleConnections": False}},
            {"$match": {"secs_running": {"$gte": min_sec}}},
            {"$sort": {"secs_running": -1}},
            {"$limit": n},
        ],
        maxTimeMS=_settings().guard.timeout_ms,
    )
    for op in cursor:
        ops.append({
            "opid": op.get("opid"),
            "op": op.get("op"),
            "ns": op.get("ns"),
            "secs_running": op.get("secs_running"),
            "client": op.get("client"),
            "desc": op.get("desc"),
            "command": _brief_command(op.get("command")),
        })
    return _ok({**_env_brief(env), "operations": ops, "count": len(ops),
                "min_running_ms": max(0, int(min_running_ms))}, source="mongo:admin")


def _brief_command(cmd: Any) -> Any:
    if not isinstance(cmd, dict):
        return cmd
    keep = {"find", "filter", "aggregate", "pipeline", "insert", "update", "delete",
            "count", "getMore", "killCursors"}
    trimmed = {k: v for k, v in cmd.items() if k in keep}
    return trimmed or list(cmd.keys())[:8]


@mcp.tool()
@tool("server_status")
def server_status(env: str) -> str:
    """实例状态摘要：版本、运行时长、连接数、内存、opcounters、asserts。"""
    _env_of(env)
    st = _conns().db(env, "admin").command("serverStatus")
    mem = st.get("mem", {}) or {}
    conns = st.get("connections", {}) or {}
    network = {k: round(v / 1048576, 1)
               for k, v in (st.get("network") or {}).items()
               if isinstance(v, (int, float))}
    return _ok({
        **_env_brief(env),
        "version": st.get("version"),
        "uptime_hours": round(st.get("uptime", 0) / 3600, 1),
        "connections": {
            "current": conns.get("current"),
            "available": conns.get("available"),
            "totalCreated": conns.get("totalCreated"),
        },
        "memory_mb": {"resident": mem.get("resident"), "virtual": mem.get("virtual")},
        "opcounters": st.get("opcounters"),
        "network_mb": network,
        "asserts": st.get("asserts"),
    }, source="mongo:admin")


@mcp.tool()
@tool("replica_status")
def replica_status(env: str) -> str:
    """副本集状态：成员角色、健康度、复制延迟、心跳。主从切换与延迟排查用。

    复制延迟 = PRIMARY 的 optimeDate − 各成员 optimeDate（秒）。
    """
    _env_of(env)
    status = _conns().db(env, "admin").command("replSetGetStatus")
    primary_optime = None
    for member in status.get("members", []):
        if member.get("stateStr") == "PRIMARY":
            primary_optime = member.get("optimeDate")
    members = []
    for member in status.get("members", []):
        optime = member.get("optimeDate")
        lag = None
        if primary_optime and optime:
            try:
                lag = int((primary_optime - optime).total_seconds())
            except TypeError:
                lag = None
        members.append({
            "name": member.get("name"),
            "state": member.get("stateStr"),
            "health": member.get("health"),
            "uptime_days": round(member.get("uptime", 0) / 86400, 1)
            if member.get("uptime") else None,
            "lag_seconds": lag,
        })
    return _ok({**_env_brief(env), "set": status.get("set"), "members": members,
                "count": len(members)}, source="mongo:admin")


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
    """唯一一层鉴权：X-MONGODB-RO-MCP-TOKEN 缺失/错误 → 403，且早于 MCP 协议处理。

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
                                           else "X-MONGODB-RO-MCP-TOKEN")
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
                "environments": [
                    {"env": name, "env_actual": env.actual, "display_name": env.display_name,
                     "aliases": env.aliases}
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
                    "max_limit": conf.guard.max_limit,
                    "timeout_ms": conf.guard.timeout_ms,
                    "forbidden_operators": sorted(conf.guard.forbidden_operators),
                    "forbidden_dbs": sorted(conf.guard.forbidden_dbs),
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
# 入口（唯一入口：python -m mongodb_ro_mcp.server）
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mongodb-ro-mcp",
        description="DBA MongoDB 只读 MCP（单服务多环境：dev / test / stage=mirror）",
    )
    parser.add_argument("--config", default=None, help="config/environments.yaml 路径")
    parser.add_argument("--transport", default=os.environ.get("MONGO_MCP_TRANSPORT", "stdio"),
                        choices=["stdio", "http"], help="stdio=本机调试；http=共享部署")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--version", action="version", version="mongodb-ro-mcp %s" % _VERSION)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("MONGO_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = init_state(args.config)
    except ConfigError as exc:
        log.error("配置加载失败: %s", exc)
        return 2

    log.info(
        "mongodb-ro-mcp %s 启动: envs=%s auth=gateway header=%s auth_dir=%s "
        "identity=%s trusted_proxies=%s",
        _VERSION, settings.names(), settings.auth.header, settings.audit.dir,
        bool(settings.identity.record), settings.identity.trusted_proxies,
    )
    for name in settings.names():
        conf = settings.environments[name]
        log.info("  环境 %s → 实际 %s（%s），别名 %s", name, conf.actual,
                 conf.display_name, conf.aliases or "无")

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
