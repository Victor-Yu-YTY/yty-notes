"""DBA MCP Server —— 线下 MySQL 只读元数据与查询服务（v1.0.0）。

对外只暴露「环境 + 库名 + 表名」，实例信息只存在于配置与路由层。
所有工具统一返回 JSON 字符串。

v0.2.4 只做一件事：**下掉假鉴权**
  1) 删除第二层"用户凭证校验"：不再有 auth.mode / verify_url / scope 判定，
     不再向任何地址（原为本机 9000 的 mock 服务）转发调用方凭证；
  2) 只剩准入令牌一层：缺失/错误 -> HTTP 403，早于 MCP 协议层；
  3) 令牌为空时拒绝启动（require_token_on_start）。
  监听面 / 导出能力 / 9 个工具契约 **均未改动**（与原 v0.2.3 一致）。

启动：
    python -m dba_mcp.server --transport stdio              # 本机调试
    python -m dba_mcp.server --transport http --port 8000   # 共享部署
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qs
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from fastmcp import FastMCP

from . import __version__ as _VERSION
from . import metadata
from .audit import write_audit
from .auth import IdentityCaptureMiddleware, current_identity, pick_header, token_ok
from .cache import MetadataCache
from .config import IDENT_RE, ConfigError, Settings, UnknownEnvironment, load_settings
from .export import ExportStore
from .pool import ConnectionManager
from .sql_guard import ensure_limit, validate_readonly

log = logging.getLogger("dba_mcp.server")

MAX_CELL_CHARS = int(os.environ.get("DBA_MCP_MAX_CELL_CHARS", "2000"))

_STATE: dict[str, Any] = {}
_started: contextvars.ContextVar[float] = contextvars.ContextVar("dba_mcp_started", default=0.0)


# --------------------------------------------------------------------------
# 状态初始化
# --------------------------------------------------------------------------
def init_state(config_path: str | None = None) -> Settings:
    if "settings" in _STATE:
        return _STATE["settings"]
    settings = load_settings(config_path or os.environ.get("DBA_MCP_CONFIG"))
    _STATE["settings"] = settings
    _STATE["conns"] = ConnectionManager(settings)
    _STATE["cache"] = MetadataCache(
        settings.cache.path, settings.cache.metadata_ttl, settings.cache.query_ttl
    )
    # v0.2.4：不再构建任何鉴权 provider（第二层鉴权已移除，只剩协议层准入令牌）
    _STATE["export"] = (
        ExportStore(settings.export.dir, settings.export.ttl_seconds)
        if settings.export.enabled
        else None
    )
    return settings


def _ensure() -> Settings:
    return _STATE.get("settings") or init_state()


# --------------------------------------------------------------------------
# 统一返回结构
# --------------------------------------------------------------------------
def _clean(value: Any) -> Any:
    """把驱动返回的 Decimal/datetime/bytes 等转成 JSON 友好的值，并截断超长文本。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return _clean(bytes(value).decode("utf-8", "replace"))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(_clean(k)): _clean(v) for k, v in value.items()}
    value = str(value)
    if len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + f"...<truncated {len(value) - MAX_CELL_CHARS} chars>"
    return value


def _elapsed_ms() -> float:
    start = _started.get()
    return round((time.perf_counter() - start) * 1000, 1) if start else 0.0


def _ok(data: dict[str, Any], *, source: str, truncated: bool = False, warning: str | None = None) -> str:
    payload: dict[str, Any] = {
        "ok": True,
        "source": source,
        "elapsed_ms": _elapsed_ms(),
        "truncated": truncated,
        "data": data,
    }
    if warning:
        payload["warning"] = warning
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(code: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, **extra},
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _export_sign_secret(settings: Settings) -> str:
    """导出下载链接的签名密钥：默认复用准入令牌；单独配置时读 EXPORT_SIGN_KEY。"""
    if settings.export.sign_with_gateway_token:
        return settings.auth.token or ""
    return os.environ.get("EXPORT_SIGN_KEY", "")


def _export_signature(secret: str, key: str, expires_at: int) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"{key}.{expires_at}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _attach_export(text: str) -> str:
    """把结果落成 .json 文件，并在响应里追加带签名/过期的下载链接。

    写文件失败时不影响主流程（返回原 JSON）。
    """
    settings: Settings | None = _STATE.get("settings")
    store: ExportStore | None = _STATE.get("export")
    if settings is None or store is None or not settings.export.enabled:
        return text
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if not isinstance(payload, dict):
        return text
    try:
        record = store.save(payload)
    except Exception:
        log.warning("导出文件写入失败", exc_info=True)
        return text
    url = f"{settings.export.base_url}/export/{record['uuid']}.json"
    secret = _export_sign_secret(settings)
    if secret:
        expires_at = int(time.time()) + max(60, settings.export.ttl_seconds)
        url += f"?exp={expires_at}&sig={_export_signature(secret, record['uuid'], expires_at)}"
    payload["export"] = {
        "url": url,
        "file": record["file"],
        "expires_in": settings.export.ttl_seconds,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------
# 鉴权 / 参数校验 / 审计
# --------------------------------------------------------------------------
def _audit_identity() -> dict[str, str]:
    """审计用身份（网关注入的 X-Agent-Id / X-Agent-Name / X-Request-Id）。

    只写日志，不参与授权：本版没有"用户凭证"可校验，
    调用方身份与环境授权都在网关侧由 AgentID 逐请求核验。
    """
    return current_identity()


def _check_ident(kind: str, value: str) -> str | None:
    if not value or not IDENT_RE.match(value):
        return _err(
            "INVALID_IDENTIFIER",
            f"{kind} 不合法: {value!r}（只允许字母、数字、下划线和 $）",
        )
    return None


def _audit(tool_name: str, *, env: str | None, status: str, detail: dict[str, Any] | None = None) -> None:
    try:
        write_audit(
            _STATE["settings"].audit_dir,
            {
                "tool": tool_name,
                "env": env,
                "status": status,
                # 网关注入的身份：只作追溯锚点，不参与授权判定
                "identity": _audit_identity() or None,
                "detail": detail or {},
            },
        )
    except Exception:
        pass


def tool(tool_name: str) -> Callable:
    """工具装饰器：统一计时、异常兜底、审计（鉴权已在协议层之前完成）。

    注意：被装饰的函数签名里不要再出现带默认值的内部参数（比如 started），
    MCP SDK 会把默认值一起作为调用参数传进来，容易和装饰器注入的参数冲突。
    计时改用 contextvar。
    """

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            _started.set(time.perf_counter())
            env = kwargs.get("env")
            status = "ok"
            try:
                # 准入令牌由 _GatewayTokenMiddleware 在协议层之前校验，缺/错返回 403，
                # 根本进不到这里。本层不再做任何"用户凭证"校验（v0.2.4 已移除）。
                return _attach_export(fn(*args, **kwargs))
            except UnknownEnvironment as exc:
                status = "error"
                return _attach_export(
                    _err("ENV_NOT_FOUND", str(exc), available=sorted(_STATE["settings"].environments))
                )
            except Exception as exc:
                status = "error"
                log.exception("tool=%s 执行失败", tool_name)
                return _attach_export(_err("TOOL_ERROR", f"{type(exc).__name__}: {exc}"))
            finally:
                _audit(tool_name, env=env, status=status)

        return wrapper

    return deco


# --------------------------------------------------------------------------
# 元数据读取：缓存优先，未命中回源 information_schema
# --------------------------------------------------------------------------
def _load_databases(env: str) -> tuple[list[dict], str]:
    settings: Settings = _ensure()
    cache: MetadataCache = _STATE["cache"]
    hit = cache.get_databases(env)
    if hit is not None:
        return hit, "cache"

    with _STATE["conns"].connection(env) as conn:
        rows = metadata.fetch_databases(conn)
    db_env = settings.environments[env]
    cache.put_databases(env, rows, db_env.source_instance_map)
    return (
        [
            {
                "database": r["database"],
                "charset": r.get("charset"),
                "collation": r.get("collation"),
                "source_instance": db_env.source_instance_map.get(r["database"], ""),
            }
            for r in rows
        ],
        "information_schema",
    )


def _load_tables(env: str, database: str) -> tuple[list[dict], str]:
    cache: MetadataCache = _STATE["cache"]
    hit = cache.get_tables(env, database)
    if hit is not None:
        return hit, "cache"

    with _STATE["conns"].connection(env) as conn:
        rows = metadata.fetch_tables(conn, database)
    cache.put_tables(env, database, rows)
    return (
        [
            {
                "table": r["table"],
                "engine": r.get("engine"),
                "collation": r.get("collation"),
                "comment": r.get("comment"),
            }
            for r in rows
        ],
        "information_schema",
    )


def _source_instance(env: str, database: str) -> str:
    return _ensure().environments[env].source_instance_map.get(database, "")


def _suggest_tables(conn: Any, database: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME AS `table` FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME LIMIT 10",
            (database, f"%{table}%"),
        )
        return [r["table"] for r in cur.fetchall()]


# --------------------------------------------------------------------------
# MCP 工具
# --------------------------------------------------------------------------
mcp = FastMCP("db-read-mcp")


@mcp.tool()
def list_environments() -> str:
    """列出可用的线下数据库环境（dev 开发 / test 测试 / stage 模拟）。

    当研发没有说明环境、或提到的环境名不确定是否存在时使用，
    拿到环境名后再调用其它工具。这是所有元数据查询的第一步。
    """
    _audit("list_environments", env=None, status="ok")
    settings: Settings = _ensure()
    data = [{"env": name, "display_name": cfg.display_name} for name, cfg in settings.environments.items()]
    return _attach_export(_ok({"environments": data, "count": len(data)}, source="config"))


@mcp.tool()
@tool("list_databases")
def list_databases(env: str) -> str:
    """列出指定环境下的所有数据库。

    env 必须是 dev / test / stage 之一。只返回库清单，不返回表信息。
    """
    _ensure().env(env)
    rows, source = _load_databases(env)
    return _ok({"env": env, "databases": rows, "count": len(rows)}, source=source)


@mcp.tool()
@tool("list_tables")
def list_tables(env: str, database: str, pattern: str = "%") -> str:
    """列出指定环境下某个数据库中的所有表。

    pattern 支持 SQL LIKE 语法（% 匹配任意字符），例如 'clue%'。
    只返回表名，不返回字段；要查字段请用 describe_table。
    """
    _ensure().env(env)
    if bad := _check_ident("database", database):
        return bad
    if not pattern or len(pattern) > 128:
        return _err("INVALID_PATTERN", f"pattern 不合法: {pattern!r}")

    rows, source = _load_tables(env, database)
    if pattern != "%":
        needle = pattern.replace("%", "").replace("_", "")
        if needle:
            rows = [r for r in rows if needle.lower() in r["table"].lower()]
    return _ok(
        {"env": env, "database": database, "pattern": pattern, "tables": rows, "count": len(rows)},
        source=source,
    )


@mcp.tool()
@tool("describe_table")
def describe_table(env: str, database: str, table: str) -> str:
    """查询指定环境下某张表的字段清单（字段名、类型、是否可空、默认值、键、注释）以及索引信息。

    当研发问「某库某表有哪些字段」「表结构是什么样的」时使用，这是最常用的工具。
    只返回结构定义，不返回表里的实际数据；要查数据请用 run_select_query。
    """
    _ensure().env(env)
    for kind, value in (("database", database), ("table", table)):
        if bad := _check_ident(kind, value):
            return bad

    cache: MetadataCache = _STATE["cache"]
    columns = cache.get_columns(env, database, table)
    indexes = cache.get_indexes(env, database, table)

    if columns is None or indexes is None:
        with _STATE["conns"].connection(env) as conn:
            columns = metadata.fetch_columns(conn, database, table)
            indexes = metadata.fetch_indexes(conn, database, table)
            if not columns:
                return _err(
                    "TABLE_NOT_FOUND",
                    f"表不存在或当前账号无权限查看: {env}.{database}.{table}",
                    suggestions=_suggest_tables(conn, database, table),
                )
        cache.put_columns(env, database, table, columns, _source_instance(env, database))
        cache.put_indexes(env, database, table, indexes)
        source = "information_schema"
    else:
        source = "cache"

    grouped: dict[str, list[dict]] = {}
    for idx in indexes:
        grouped.setdefault(idx["index_name"], []).append(idx)

    return _ok(
        {
            "env": env,
            "database": database,
            "table": table,
            "column_count": len(columns),
            "columns": columns,
            "indexes": [
                {
                    "index_name": name,
                    "columns": [i["column_name"] for i in items],
                    "non_unique": items[0]["non_unique"],
                    "index_type": items[0]["index_type"],
                }
                for name, items in grouped.items()
            ],
        },
        source=source,
    )


@mcp.tool()
@tool("get_create_table")
def get_create_table(env: str, database: str, table: str) -> str:
    """获取指定环境下某张表的完整建表语句（SHOW CREATE TABLE），含字段、索引、表选项。

    需要看到完整 DDL（比如照着建一张同样的表、核对引擎和字符集）时使用；
    只想看字段清单时 describe_table 更轻量。
    """
    _ensure().env(env)
    for kind, value in (("database", database), ("table", table)):
        if bad := _check_ident(kind, value):
            return bad

    with _STATE["conns"].connection(env) as conn:
        ddl = metadata.fetch_create_table(conn, database, table)
    if not ddl:
        return _err("TABLE_NOT_FOUND", f"表不存在或当前账号无权限查看: {env}.{database}.{table}")
    return _ok({"env": env, "database": database, "table": table, "ddl": ddl}, source="mysql")


@mcp.tool()
@tool("search_column")
def search_column(column_name: str, env: str = "", database: str = "") -> str:
    """按字段名反查：找出哪些库/哪些表含有这个字段。

    column_name 支持 LIKE 语法（% 通配）。env 留空表示在三个环境里都查（较慢）。
    当研发问「这个字段在哪些表里」「某个业务字段对应哪张表」时使用。
    """
    settings: Settings = _ensure()
    if env:
        settings.env(env)
    if database and (bad := _check_ident("database", database)):
        return bad
    if not column_name or len(column_name) > 128:
        return _err("INVALID_PATTERN", f"column_name 不合法: {column_name!r}")

    targets = [env] if env else list(settings.environments)
    results: list[dict] = []
    sources: set[str] = set()

    for target in targets:
        with _STATE["conns"].connection(target) as conn:
            rows = metadata.search_columns(conn, column_name)
            if database:
                rows = [r for r in rows if r["database"] == database]
        sources.add("information_schema")
        for row in rows:
            row["env"] = target
        results.extend(rows)

    return _ok(
        {"column_name": column_name, "env": env or "all", "matches": results, "count": len(results)},
        source="+".join(sorted(sources)) or "information_schema",
    )


@mcp.tool()
@tool("run_select_query")
def run_select_query(
    env: str,
    sql: str,
    database: str = "",
    limit: int = 200,
    use_cache: bool = False,
) -> str:
    """在指定环境执行一条只读 SELECT 查询，返回结构化结果。

    只允许 SELECT；禁止 INSERT/UPDATE/DELETE/DDL、多语句、INTO OUTFILE、FOR UPDATE。
    未写 LIMIT 时自动补上 limit（默认 200，最大 1000）。
    只想看表有哪些字段时请用 describe_table，不要 SELECT * 全表扫描。
    """
    settings: Settings = _ensure()
    settings.env(env)

    guard = validate_readonly(sql, forbidden_schemas=settings.guard.forbidden_schemas)
    if not guard.ok:
        return _err("SQL_REJECTED", guard.reason)

    limit = min(limit if limit > 0 else settings.guard.default_limit, settings.guard.max_limit)
    final_sql = ensure_limit(guard.sql, limit)

    cache: MetadataCache = _STATE["cache"]
    key = hashlib.sha256(f"{env}|{database}|{final_sql}".encode("utf-8")).hexdigest()
    if use_cache and (hit := cache.get_query(key)) is not None:
        return _ok(hit, source="cache")

    if database and (bad := _check_ident("database", database)):
        return bad

    with _STATE["conns"].connection(env) as conn:
        with conn.cursor() as cur:
            if database:
                cur.execute(f"USE `{database}`")
            cur.execute(final_sql)
            rows = [_clean(dict(r)) for r in cur.fetchall()]

    payload = {
        "env": env,
        "database": database or None,
        "sql": final_sql,
        "row_count": len(rows),
        "limit": limit,
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
    }
    if use_cache:
        cache.put_query(key, env, final_sql, payload)
    return _ok(payload, source="mysql", truncated=len(rows) >= limit)


@mcp.tool()
@tool("explain_query")
def explain_query(env: str, sql: str, database: str = "") -> str:
    """对一条 SELECT 执行 EXPLAIN，返回执行计划（只做计划分析，不执行原查询）。

    当研发问「这条 SQL 走没走索引」「为什么慢」时使用。
    """
    settings: Settings = _ensure()
    settings.env(env)

    guard = validate_readonly(sql, forbidden_schemas=settings.guard.forbidden_schemas)
    if not guard.ok:
        return _err("SQL_REJECTED", guard.reason)

    if database and (bad := _check_ident("database", database)):
        return bad

    with _STATE["conns"].connection(env) as conn:
        with conn.cursor() as cur:
            if database:
                cur.execute(f"USE `{database}`")
            cur.execute(f"EXPLAIN {guard.sql}")
            rows = [_clean(dict(r)) for r in cur.fetchall()]
    return _ok({"env": env, "database": database or None, "plan": rows}, source="mysql")


@mcp.tool()
@tool("refresh_metadata")
def refresh_metadata(env: str, database: str = "") -> str:
    """强制刷新元数据缓存：清空指定范围后重新采集 information_schema。

    研发刚改完表结构、发现查到的还是旧结构时使用。
    database 留空表示只刷新该环境的库清单；指定 database 会刷新该库下所有表的字段与索引。
    """
    settings: Settings = _ensure()
    settings.env(env)
    if database and (bad := _check_ident("database", database)):
        return bad

    cache: MetadataCache = _STATE["cache"]
    cache.purge(env, database or None)

    with _STATE["conns"].connection(env) as conn:
        if not database:
            rows = metadata.fetch_databases(conn)
            cache.put_databases(env, rows, settings.environments[env].source_instance_map)
            return _ok({"env": env, "scope": "databases", "count": len(rows)}, source="information_schema")

        tables = metadata.fetch_tables(conn, database)
        cache.put_tables(env, database, tables)
        for tbl in tables:
            cols = metadata.fetch_columns(conn, database, tbl["table"])
            idxs = metadata.fetch_indexes(conn, database, tbl["table"])
            if cols:
                cache.put_columns(env, database, tbl["table"], cols, _source_instance(env, database))
            cache.put_indexes(env, database, tbl["table"], idxs)

    return _ok(
        {"env": env, "scope": f"database:{database}", "table_count": len(tables)},
        source="information_schema",
    )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
async def _send_json(send, status: int, payload: dict[str, Any], *, filename: str | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if filename:
        headers.append((b"content-disposition", f'attachment; filename="{filename}"'.encode("utf-8")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _ExportRouteMiddleware:
    """ASGI 中间件：GET /export/<uuid>.json?exp=&sig= 下载 JSON 结果文件。

    带签名设计：链接 = <base_url>/export/<uuid>.json?exp=<过期时间戳>&sig=<HMAC-SHA256>；
    校验签名或过期时间失败 → 403；文件超过 TTL 或不存在 → 404。
    链接中不含任何明文令牌，只含 uuid 与签名。
    """

    def __init__(self, app, store: ExportStore, settings: Settings) -> None:
        self._app = app
        self._store = store
        self._secret = _export_sign_secret(settings)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("method") == "GET":
            path = scope.get("path", "")
            if path.startswith("/export/") and path.endswith(".json"):
                key = path[len("/export/"):-len(".json")]
                if self._secret:
                    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
                    exp_s = (query.get("exp") or [""])[0]
                    sig = (query.get("sig") or [""])[0]
                    ok = False
                    if exp_s.isdigit() and sig:
                        expires_at = int(exp_s)
                        if expires_at > int(time.time()) and hmac.compare_digest(
                            sig, _export_signature(self._secret, key, expires_at)
                        ):
                            ok = True
                    if not ok:
                        client = scope.get("client") or ("", 0)
                        _audit(
                            "export_download",
                            env=None,
                            status="denied",
                            detail={
                                "key_prefix": key[:8],
                                "reason": "bad_signature_or_expired",
                                "client": str(client[0]),
                            },
                        )
                        await _send_json(
                            send,
                            403,
                            {"ok": False, "error": {"code": "FORBIDDEN", "message": "下载链接无效或已过期"}},
                        )
                        return
                body = self._store.read(key)
                if body is None:
                    await _send_json(send, 404, {"error": "文件不存在或已过期（默认 1 小时）"})
                    return
                headers = [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-disposition", f'attachment; filename="dba-mcp-{key[:8]}.json"'.encode("utf-8")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ]
                await send({"type": "http.response.start", "status": 200, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


class _GatewayTokenMiddleware:
    """准入令牌（唯一一层鉴权）：HTTP 层校验，缺失/错误 → 403，不进入 MCP 协议。

    约定：
      - 仅保护 MCP 路径（server.path，默认 /mcp）；GET /healthz 白名单放行；
      - /export/* 下载链接沿用随机 uuid + 签名 + 1 小时过期（本版未改动，见配置说明）；
      - 不缓存任何校验结果：没有"通过了就记住"的分支；
      - 令牌不落日志、不写审计，只参与常量时间比较；拒绝时审计记录来源 IP 与原因。
    """

    def __init__(self, app, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        auth = settings.auth
        self._enabled = auth.enabled
        # 兼容期：同时接受新头名与旧头名；legacy_header 留空即只认新头名
        self._headers = auth.accepted_headers()
        self._token = (auth.token or "").strip()
        self._header_hint = auth.header or (self._headers[0] if self._headers else "X-DB-READ-MCP-TOKEN")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if method in ("GET", "HEAD") and path.rstrip("/") in ("/healthz", "/health"):
            await _send_json(send, 200, {"ok": True, "service": "db-read-mcp"})
            return
        if not self._enabled:
            await self._app(scope, receive, send)
            return
        mcp_path = self._settings.server.path or "/mcp"
        if path == mcp_path or path == mcp_path + "/" or path.startswith(mcp_path + "/"):
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            _hdr, provided = pick_header(headers, self._headers)
            if not token_ok(provided, self._token):
                client = scope.get("client") or ("", 0)
                _audit(
                    "gateway_token",
                    env=None,
                    status="denied",
                    detail={
                        "reason": "missing" if not provided else "invalid",
                        "header": _hdr or self._header_hint,
                        "path": path,
                        "client": str(client[0]),
                    },
                )
                await _send_json(
                    send,
                    403,
                    {
                        "ok": False,
                        "error": {
                            "code": "FORBIDDEN",
                            "message": f"缺少或无效的网关准入令牌（请求头 {self._header_hint}）",
                        },
                    },
                )
                return
        await self._app(scope, receive, send)


def build_http_app():
    """构建 ASGI app，并挂上身份抓取中间件 + 导出下载路由。

    实测 fastmcp 4.1.1：只有 FastMCP.http_app(path=..., middleware=[...])，
    没有 streamable_http_app / sse_app。
    """
    settings = _ensure()
    factory = getattr(mcp, "http_app", None) or getattr(mcp, "streamable_http_app", None)
    if factory is None:
        return None
    store = _STATE.get("export")
    for kwargs in ({"path": settings.server.path}, {}):
        try:
            # 注意：不要走 Starlette 的 middleware=[...] 参数，它要求
            # (cls, args, kwargs) 元组形式，传裸类会在请求阶段抛 TypeError。
            inner = IdentityCaptureMiddleware(factory(**kwargs))
            if store is not None:
                inner = _ExportRouteMiddleware(inner, store, settings)
            return _GatewayTokenMiddleware(inner, settings)
        except TypeError:
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="db-read-mcp", description="线下 MySQL 只读 MCP 服务")
    parser.add_argument("--config", default=None, help="environments.yaml 路径")
    parser.add_argument(
        "--transport",
        default=os.environ.get("DBA_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "http"],
        help="stdio=本机调试；http=共享部署",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("DBA_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = init_state(args.config)
    except ConfigError as exc:
        log.error("配置加载失败: %s", exc)
        return 2

    log.info(
        "db-read-mcp %s 启动: envs=%s auth=gateway header=%s cache=%s",
        _VERSION,
        sorted(settings.environments),
        settings.auth.header,
        settings.cache.path,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    host = args.host or settings.server.host
    port = args.port or settings.server.port
    app = build_http_app()
    if app is None:
        log.warning("未取到 ASGI app，回退到 SDK 内置 http 服务（身份抓取走 SDK 通道）")
        mcp.run(transport="http", host=host, port=port, path=settings.server.path)
        return 0

    import uvicorn

    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
