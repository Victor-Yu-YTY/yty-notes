"""MCP 服务入口（1.160 测试版，端口 8040）。

execute 走真实执行。HTTP 模式两层保护：
  ① TokenMiddleware —— 服务令牌缺失/错误直接 403，早于 MCP 协议处理；
  ② 工具级 _gate —— stdio 模式与中间件失效时的兜底。
绑非回环地址且未配令牌时拒绝启动。

判定规则来自 `standards/executor.md`（唯一事实源），按 mtime 热加载：
改 md 即改标准，不用改代码、不用重启。
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import sys
from typing import Any

from . import __version__
from . import parser as P
from . import rollback as RB
from .backend import MySQLProvider, Provider, StubProvider
from .config import Settings
from .engine import PolicyHolder
from .executor import DRY_OFF, execute as run_execute, render_text as render_exec_text
from .preflight import DECISION_BLOCKED, render_text, run as run_preflight
from .session import MySQLSession, Session
from .snapshot import from_ddl, show_create
from .store import ExecutionStore
from .undo import read_records

log = logging.getLogger("sql_executor_mcp")

_SETTINGS = Settings.load()
_HOLDER = PolicyHolder(_SETTINGS.standard_file)
_PROVIDERS: dict[str, Provider] = {}
_STORE = ExecutionStore(_SETTINGS.logs_dir)


def _standard_brief() -> dict[str, Any]:
    """结果里带上「按哪版标准判的」，便于事后对账。"""
    policy = _HOLDER.policy
    return {
        "standard_version": policy.standard_version,
        "rules_version": policy.version,
        "rules_count": len(policy.checks),
        "standard_sha256": (policy.sha256 or "")[:16],
    }


def _env_cfg(env: str) -> dict:
    """判定参数来自 standards 的环境表，连接参数来自 targets.yaml。

    库白名单已取消：库级隔离由账号 GRANT 承担，不再往判定参数里塞 databases。

    标准里没有这个环境（写错名字、或用未列出的环境）时返回空 dict，
    让 preflight 走「环境未在标准的环境表中配置」的早退分支（blocked_by=["ENV"]），
    而不是把它当成「环境存在但被关闭」（那会给出 EX-G02，容易误导）。
    """
    cfg = dict(_HOLDER.policy.env_cfg(env))
    if not cfg:
        return {}
    cfg["connection"] = _SETTINGS.env_cfg(env)
    return cfg


def _provider(env: str) -> Provider:
    if _SETTINGS.backend == "stub":
        return StubProvider({})
    prov = _PROVIDERS.get(env)
    if prov is not None:
        return prov
    conn = _SETTINGS.env_cfg(env)
    if not conn:
        # 环境没在 targets.yaml 配连接（如未开放的 prod、写错的环境名）：
        # 不抛异常，退化成「所有事实取不到」，让判定层自己 fail-closed 给出
        # EX-G02 / EX-G07 这类结论 —— 客户端拿到的是判定结果，不是 500。
        log.warning("环境 %s 未在 targets.yaml 配置连接信息：按「事实取不到」fail-closed 判定", env)
        return Provider()
    prov = MySQLProvider(conn)
    _PROVIDERS[env] = prov
    return prov


def _session_factory(env: str):
    """执行会话是独立的写连接，和事实采集用的只读连接分开。

    返回工厂而不是会话本身：`dry_run=plan` 根本不该建连接。
    """
    if _SETTINGS.backend == "stub":
        def _refuse():
            raise RuntimeError("backend=stub 不支持真实执行，请用 dry_run=plan 只看判定结果")
        return _refuse
    conn = _SETTINGS.env_cfg(env)
    if not conn:
        def _no_cfg():
            raise RuntimeError(f"环境 {env} 未在 targets.yaml 配置连接信息")
        return _no_cfg
    return lambda: MySQLSession(conn)


def _do_preflight(sql: str, env: str, database: str):
    cfg = _env_cfg(env)
    prov = _provider(env)
    return run_preflight(
        sql=sql, env=env, database=database,
        policy=_HOLDER.policy, provider=prov, env_cfg=cfg,
    )


def _gate() -> dict[str, Any] | None:
    """工具级兜底鉴权：HTTP 正常由 TokenMiddleware 拦住，stdio 模式靠这里。

    令牌没配（本机联调）→ 放行；取了请求头但缺失/不符 → 拒绝。
    这是个能写库的服务，宁可多一道，不能只靠外层。
    """
    if not _SETTINGS.token:
        return None
    want = _SETTINGS.token_header.lower()
    provided = ""
    try:
        from fastmcp.server.dependencies import get_http_headers
        for k, v in (get_http_headers() or {}).items():
            if k.lower() == want:
                provided = (v or "").strip()
    except Exception:  # noqa: BLE001  stdio 下没有 HTTP 头，属于正常情况
        return None
    if not provided:
        return {"decision": DECISION_BLOCKED, "blocked_by": ["AUTH_TOKEN_MISSING"],
                "refused_reason": f"缺少服务令牌（请求头 {_SETTINGS.token_header}）",
                "service_version": __version__}
    if not hmac.compare_digest(provided, _SETTINGS.token):
        return {"decision": DECISION_BLOCKED, "blocked_by": ["AUTH_FORBIDDEN"],
                "refused_reason": "服务令牌无效",
                "service_version": __version__}
    return None


# ---------------------------------------------------------------- MCP 工具


def _guard(tool: str, fn, *args, **kwargs):
    """工具层兜底：任何未预期异常都转成结构化 BLOCKED，绝不把异常抛给客户端。

    判定服务对调用方的契约是「永远给一个结论」，异常文本会让 agent 无从判断
    （是环境配错了、还是库不通、还是它自己的 SQL 有问题）。
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("工具 %s 执行异常", tool)
        return {
            "decision": DECISION_BLOCKED,
            "blocked_by": ["INTERNAL_ERROR"],
            "refused_reason": f"服务内部错误，未执行任何语句：{type(exc).__name__}: {exc}",
            "service_version": __version__,
        }


try:
    from fastmcp import FastMCP
    _mcp = FastMCP("sql-executor-mcp")
except Exception:  # noqa: BLE001  # 允许无 fastmcp 时用于本机自检
    _mcp = None


def _tool(fn):
    return _mcp.tool()(fn) if _mcp is not None else fn


@_tool
def preflight(sql: str, env: str = "dev", database: str = "") -> dict[str, Any]:
    """判定一条 SQL 在当前环境能不能执行，不执行任何语句。

    Args:
        sql: 待判定的 SQL，可含多条语句（上限 20）。
        env: dev / test / stage。
        database: 目标库名，SQL 未带库前缀时用于补全；库级权限由执行账号的 GRANT 决定。

    Returns:
        decision（READY / BLOCKED）、逐条检查结果、事实快照、阻塞项与风险提示。
    """
    denied = _gate()
    if denied:
        return denied

    def body():
        res = _do_preflight(sql, env, database)
        out = res.as_dict()
        out["service_version"] = __version__
        out.update(_standard_brief())
        return out

    return _guard("preflight", body)


def _do_execute(sql: str, env: str, database: str, dry_run: str,
                operator: str, sql_sha256: str) -> dict[str, Any]:
    cfg = _env_cfg(env)
    out = run_execute(
        sql=sql, env=env, database=database,
        policy=_HOLDER.policy, provider=_provider(env),
        session_factory=_session_factory(env),
        env_cfg=cfg, store=_STORE,
        dry_run=dry_run or DRY_OFF, operator=operator,
        sql_sha256=sql_sha256,
    )
    d = out.as_dict()
    d["policy_version"] = _HOLDER.policy.version
    d["policy_mode"] = _HOLDER.policy.mode
    d["service_version"] = __version__
    d.update(_standard_brief())
    if _HOLDER.warning:
        d["policy_warning"] = _HOLDER.warning
    return d


def _do_preview_rollback(sql: str, env: str, database: str) -> dict[str, Any]:
    pf = _do_preflight(sql, env, database)
    out: dict[str, Any] = {"decision": pf.decision, "blocked_by": pf.blocked_by}
    if pf.decision == DECISION_BLOCKED:
        out["note"] = "判定未通过，不预演回滚"
        out["service_version"] = __version__
        out.update(_standard_brief())
        return out

    parsed = P.parse(sql, default_db=database)
    plans = []
    conn = _SETTINGS.env_cfg(env)
    session: Session | None = None
    conn_error = ""
    if _SETTINGS.backend == "stub":
        conn_error = "stub 后端拿不到表结构，无法预演"
    elif not conn:
        conn_error = f"环境 {env} 未在 targets.yaml 配置连接信息，无法预演"

    try:
        for st in parsed.statements:
            db = st.database or database
            snap, note = None, ""
            if st.kind in P.DDL_KINDS and st.kind != P.K_CREATE_TABLE:
                if conn_error:
                    note = conn_error
                else:
                    try:
                        if session is None:
                            session = MySQLSession(conn)
                        ddl = show_create(session, db, st.table or "")
                    except Exception as exc:  # noqa: BLE001  连不上就说连不上，别让整个工具挂掉
                        ddl = None
                        note = f"取表结构失败：{type(exc).__name__}: {exc}"
                    snap = from_ddl(ddl, db, st.table or "") if ddl else None
                    if snap is None and not note:
                        note = "取不到表结构，无法预演"
            plan = RB.ddl_reverse(st, snap, db) if st.kind in P.DDL_KINDS \
                else RB.RollbackPlan(available=False, note="DML 的旧值必须真实执行时采集，无法预演")
            item = plan.to_dict()
            if note:
                item["note"] = note
            item["index"] = st.index
            item["type"] = st.kind
            plans.append(item)
    finally:
        if session is not None:
            session.close()
    out["rollback"] = plans
    out["service_version"] = __version__
    out.update(_standard_brief())
    return out


def _do_get_execution(execution_id: str, include_undo: bool) -> dict[str, Any]:
    rec = _STORE.load(execution_id)
    if rec is None:
        return {
            "error": "not_found",
            "message": f"未找到执行记录 {execution_id}",
            "service_version": __version__,
        }
    out = dict(rec)
    if include_undo:
        out["undo_records"] = read_records(_STORE.undo_path(execution_id))
    out["service_version"] = __version__
    return out


@_tool
def execute(
    sql: str,
    env: str = "dev",
    database: str = "",
    dry_run: str = "off",
    operator: str = "",
    sql_sha256: str = "",
) -> dict[str, Any]:
    """判定并执行 SQL。判定不过一律不执行。

    Args:
        sql: 待执行的 SQL，可含多条语句（上限 20，DDL 与 DML 不得同批）。
        env: dev / test / stage。
        database: 目标库名。
        dry_run: off 正常执行；plan 只判定不执行；rollback 仅 DML，事务内真跑再回滚。
        operator: 操作人，落审计。
        sql_sha256: 可选，传入则执行前校验 SQL 未被替换。
    """
    denied = _gate()
    if denied:
        return denied
    return _guard("execute", _do_execute, sql, env, database, dry_run, operator, sql_sha256)


@_tool
def preview_rollback(sql: str, env: str = "dev", database: str = "") -> dict[str, Any]:
    """判定的同时预演回滚形态，不连库执行、不产生 undo 记录。

    只对 DDL 有完整结论（靠 SHOW CREATE TABLE 推反向语句）；DML 的逐行旧值
    必须在真实事务里采，这里只能给形态说明。

    Args:
        sql: 待判定的 SQL。
        env: dev / test / stage。
        database: 目标库名。
    """
    denied = _gate()
    if denied:
        return denied
    return _guard("preview_rollback", _do_preview_rollback, sql, env, database)


@_tool
def get_execution(execution_id: str, include_undo: bool = False) -> dict[str, Any]:
    """查询某次执行的结果、回滚 SQL 与 undo 日志位置。

    Args:
        execution_id: execute 返回的执行号，形如 exe-20261103-112045-a1b2。
        include_undo: 是否把 undo 明细（逐行旧值）一并返回，行数多时慎用。
    """
    denied = _gate()
    if denied:
        return denied
    return _guard("get_execution", _do_get_execution, execution_id, include_undo)


@_tool
def list_checks(level: str = "") -> dict[str, Any]:
    """列出当前生效的检查项与阈值（规则来自 standards/executor.md）。

    Args:
        level: block / warn，留空列全部。
    """
    denied = _gate()
    if denied:
        return denied

    def body():
        policy = _HOLDER.policy
        items = []
        for c in policy.checks:
            if not c.enabled:
                continue
            if level and c.level != level:
                continue
            items.append({
                "id": c.id, "level": c.level, "title": c.title,
                "applies_to": c.applies_to, "on_missing": c.on_missing,
            })
        return {
            "policy_version": policy.version,
            "engine": policy.engine,
            "mode": policy.mode,
            "total": len(items),
            "checks": items,
            "thresholds": policy.thresholds,
            "environments": policy.environments,
            "counts": policy.counts(),
            "standard": {
                "file": policy.path,
                "standard_version": policy.standard_version,
                "sha256": policy.sha256,
            },
            "policy_warning": _HOLDER.warning,
            "service_version": __version__,
        }

    return _guard("list_checks", body)


# ---------------------------------------------------------------- HTTP 闸门


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _healthz_payload(settings: Settings) -> dict:
    """健康检查：除存活外，同时自证「按哪一版标准判」。"""
    policy = _HOLDER.policy
    counts = policy.counts()
    return {
        "ok": True,
        "service": settings.name,
        "version": __version__,
        "auth": "on" if settings.token else "off",
        "backend": settings.backend,
        "mode": policy.mode,
        "standard_version": policy.standard_version,
        "rules_version": policy.version,
        "rules_count": counts["rules_count"],
        "block_count": counts["block_count"],
        "warn_count": counts["warn_count"],
        "standard_sha256": (policy.sha256 or "")[:16],
        "standard_file": policy.path,
    }


class TokenMiddleware:
    """服务令牌闸门：缺失/错误 → 403，且早于 MCP 协议处理。

    执行服务比审核服务更要紧：这里放进来一条 SQL 就可能写库，
    所以令牌校验必须挡在协议层前面，不能只靠工具函数里的兜底。
    """

    def __init__(self, app, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        self._header = settings.token_header.lower()
        self._token = settings.token or ""
        self._path = settings.path or "/mcp"

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if method in ("GET", "HEAD") and path.rstrip("/") in ("/healthz", "/health"):
            await _send_json(send, 200, _healthz_payload(self._settings))
            return
        if not self._token:
            await self._app(scope, receive, send)
            return
        if path == self._path or path == self._path + "/" or path.startswith(self._path + "/"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            provided = (headers.get(self._header) or "").strip()
            if not provided or not hmac.compare_digest(provided, self._token):
                client = scope.get("client") or ("", 0)
                log.warning("拒绝未授权请求：path=%s client=%s reason=%s", path, client[0],
                            "missing" if not provided else "invalid")
                await _send_json(send, 403, {"ok": False, "error": {
                    "code": "FORBIDDEN",
                    "message": f"缺少或无效的服务令牌（请求头 {self._settings.token_header}）"}})
                return
        await self._app(scope, receive, send)


def build_http_app(settings: Settings):
    factory = getattr(_mcp, "http_app", None) or getattr(_mcp, "streamable_http_app", None)
    if factory is None:
        return None
    for kwargs in ({"path": settings.path}, {}):
        try:
            return TokenMiddleware(factory(**kwargs), settings)
        except TypeError:
            continue
    return None


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sql-executor-mcp")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "http"])
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--path", default=None)
    ap.add_argument("--demo", metavar="SQL", help="不启动服务，直接判定并打印（不执行）")
    ap.add_argument("--demo-exec", metavar="SQL",
                    help="不启动服务，判定并执行；默认 dry_run=plan，需显式 --demo-dry-run off 才真写")
    ap.add_argument("--demo-dry-run", default="plan", choices=["off", "plan", "rollback"])
    ap.add_argument("--demo-operator", default="")
    ap.add_argument("--demo-get", metavar="EXECUTION_ID", help="查看某次执行记录与回滚 SQL")
    ap.add_argument("--with-undo", action="store_true", help="配合 --demo-get 输出逐行 undo 明细")
    ap.add_argument("--demo-env", default="dev")
    ap.add_argument("--demo-db", default="")
    ap.add_argument("--json", action="store_true", help="配合 --demo / --demo-exec 输出 JSON")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, _SETTINGS.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    if args.demo:
        res = _do_preflight(args.demo, args.demo_env, args.demo_db)
        out = res.as_dict()
        out.update(_standard_brief())
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else render_text(res))
        return 0

    if args.demo_exec:
        import os
        operator = args.demo_operator or os.environ.get("USERNAME") \
            or os.environ.get("USER") or "cli"
        out = run_execute(
            sql=args.demo_exec, env=args.demo_env, database=args.demo_db,
            policy=_HOLDER.policy, provider=_provider(args.demo_env),
            session_factory=_session_factory(args.demo_env),
            env_cfg=_env_cfg(args.demo_env), store=_STORE,
            dry_run=args.demo_dry_run, operator=operator,
        )
        print(json.dumps(out.as_dict(), ensure_ascii=False, indent=2) if args.json
              else render_exec_text(out))
        # 退出码：判定不过 3，执行未提交 4，正常 0
        if out.decision == DECISION_BLOCKED:
            return 3
        return 0 if out.committed else 4

    if args.demo_get:
        rec = _do_get_execution(args.demo_get, args.with_undo)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0 if "error" not in rec else 5

    if _mcp is None:
        print("未安装 fastmcp，只能用 --demo / --demo-exec / --demo-get 模式", file=sys.stderr)
        return 2

    host = args.host or _SETTINGS.host
    port = args.port or _SETTINGS.port
    if args.path:
        _SETTINGS.path = args.path
    if args.transport != "http":
        _mcp.run()
        return 0

    # 能写库的服务不许裸奔：绑非回环地址却配不出令牌，直接不启动
    if not _SETTINGS.token and not _is_loopback(host):
        log.error("绑定 %s（非回环）但未配置服务令牌 %s，拒绝启动；"
                  "请注入令牌环境变量，或改绑 127.0.0.1", host, _SETTINGS.token_header)
        return 2

    policy = _HOLDER.policy
    log.info("启动 HTTP %s:%s%s 鉴权=%s backend=%s 标准=%s(rules=%d, sha=%s) version=%s",
             host, port, _SETTINGS.path, "on" if _SETTINGS.token else "off",
             _SETTINGS.backend, policy.standard_version, len(policy.checks),
             (policy.sha256 or "")[:8], __version__)
    app = build_http_app(_SETTINGS)
    if app is None:
        log.warning("未取到 ASGI app，回退 SDK 内置 http 服务（令牌校验改由工具内兜底，风险更高）")
        try:
            _mcp.run(transport="http", host=host, port=port, path=_SETTINGS.path)
        except TypeError:
            _mcp.run(transport="http", host=host, port=port)
        return 0
    import uvicorn
    uvicorn.run(app, host=host, port=port)
    return 0


def _is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "[::1]")


if __name__ == "__main__":
    raise SystemExit(main())
