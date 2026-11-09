"""执行器：判定通过后真正落地，并按「写前落盘」顺序采集 undo。

刻意不 import policy 的判定逻辑：这里独立再校验一遍（设计 §8.5），
任一项不过直接拒。判定与执行两层各自成立，单点失效不会直接变成写库。

事务策略（设计 §8.2）：
  INSERT / UPDATE  显式事务，任何一个环节异常 → ROLLBACK 全部
  DDL              单语句 + 显式 commit，隐式提交、失败不可回滚

DDL 与 DML 不允许同批（EX-G06），所以一批里只有一种事务形态。
"""

from __future__ import annotations

import hashlib
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import parser as P
from . import rollback as RB
from .backend import Provider
from .engine import Policy
from .preflight import DECISION_BLOCKED, DECISION_READY, PreflightResult, run as run_preflight
from .session import Session
from .snapshot import from_ddl, show_create
from .store import ExecutionStore
from .undo import UndoLog

DRY_OFF = "off"
DRY_PLAN = "plan"
DRY_ROLLBACK = "rollback"
DRY_VALUES = {DRY_OFF, DRY_PLAN, DRY_ROLLBACK}

# 语句首关键字黑名单：分类器已拦，这里是独立第二道（防分类器被绕过）
FORBIDDEN_HEADS = {
    "DELETE", "REPLACE", "TRUNCATE", "DROP", "GRANT", "REVOKE", "CALL",
    "LOAD", "HANDLER", "LOCK", "UNLOCK", "RENAME", "PURGE", "SHUTDOWN", "KILL",
}

S_OK = "ok"
S_DRY = "dry_run"
S_ERROR = "error"
S_SKIPPED = "skipped"
S_GUARD = "rolled_back_by_guard"
S_ABORT = "aborted_before_exec"


# ---------------------------------------------------------------- 结果结构


@dataclass
class StmtResult:
    index: int
    kind: str
    table: str = ""
    status: str = S_OK
    affected_rows: int | None = None
    matched_rows: int | None = None
    duration_ms: int = 0
    committed: bool = False
    verify: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {
            "index": self.index, "type": self.kind, "table": self.table,
            "status": self.status, "duration_ms": self.duration_ms,
            "committed": self.committed,
        }
        if self.affected_rows is not None:
            d["affected_rows"] = self.affected_rows
        if self.matched_rows is not None:
            d["matched_rows"] = self.matched_rows
        if self.verify:
            d["verify"] = self.verify
        if self.notes:
            d["notes"] = self.notes
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class ExecOutcome:
    decision: str = DECISION_BLOCKED
    executed: bool = False
    committed: bool = False
    execution_id: str | None = None
    dry_run: str = DRY_OFF
    operator: str = ""
    env: str = ""
    results: list[StmtResult] = field(default_factory=list)
    rollback: RB.RollbackPlan | None = None
    undo: dict = field(default_factory=dict)
    blocked_by: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refused_reason: str = ""
    checks: list[dict] = field(default_factory=list)
    undo_records: list[dict] = field(default_factory=list)

    def as_dict(self, rollback_limit: int | None = 50) -> dict:
        d = {
            "decision": self.decision,
            "executed": self.executed,
            "committed": self.committed,
            "execution_id": self.execution_id,
            "dry_run": self.dry_run,
            "operator": self.operator,
            "env": self.env,
            "results": [r.as_dict() for r in self.results],
            "blocked_by": self.blocked_by,
            "warnings": self.warnings,
        }
        if self.rollback is not None:
            d["rollback"] = self.rollback.to_dict(rollback_limit)
        if self.undo:
            d["undo"] = self.undo
        if self.refused_reason:
            d["refused_reason"] = self.refused_reason
        if self.errors:
            d["errors"] = self.errors
        if self.checks:
            d["checks"] = self.checks
        return d


# ---------------------------------------------------------------- 执行上下文


@dataclass
class Ctx:
    session: Session
    provider: Provider
    undo: UndoLog | None
    database: str
    operator: str
    dry_run: str
    max_affected_rows: int
    timeout: int
    records: list[dict] = field(default_factory=list)
    seq: int = 0
    cur_db: str = ""
    session_factory: Any = None     # DDL 连接断了要另开一条连接去探测结果

    def write_undo(self, rec: dict) -> None:
        self.seq += 1
        rec = {"seq": self.seq, "at": datetime.now().isoformat(timespec="milliseconds"), **rec}
        if self.dry_run == DRY_ROLLBACK:
            rec["dry_run"] = True
        self.records.append(rec)
        if self.undo is not None:
            self.undo.write(rec)

    def db_of(self, st: P.Statement) -> str:
        return st.database or self.database

    def use_db(self, db: str) -> None:
        """SQL 不带库前缀时必须先切库，否则 MySQL 报 No database selected。

        多语句批次可能跨库，所以按语句切、切过就记住，不重复下发 USE。
        库名走 q() 反引号转义：名字里带反引号直接拼会拼坏语句。
        """
        if not db or db == self.cur_db:
            return
        self.session.execute(f"USE {RB.q(db)}")
        self.cur_db = db


# ---------------------------------------------------------------- 独立再校验


def sha256_of(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def revalidate(sql: str, stmts: list[P.Statement], policy: Policy, env: str,
               env_cfg: dict, sql_sha256: str) -> list[str]:
    """设计 §8.5：不依赖判定阶段的结论，自己再查一遍。"""
    errs: list[str] = []

    if policy.mode != "live":
        errs.append(f"policy.mode={policy.mode}，非 live 模式不执行")

    if sql_sha256 and sha256_of(sql) != sql_sha256.lower():
        errs.append("sql 的 sha256 与判定阶段不一致，疑似中途换过 SQL")

    allowed = set(policy.allowed_types_for(env))
    if not env_cfg.get("enabled", False):
        errs.append(f"环境 {env} 未开放执行")
    if not allowed:
        errs.append(f"环境 {env} 没有配置 allowed_types，按 fail-closed 拒绝执行")

    for st in stmts:
        toks = P.tokenize(st.raw)
        head = toks[0].up if toks else ""
        if head in FORBIDDEN_HEADS:
            errs.append(f"第 {st.index} 条以 {head} 开头，属禁止写入类型")
        if st.kind not in allowed:
            errs.append(f"第 {st.index} 条类型 {st.kind} 不在本环境 allowed_types 内")
        # 库级隔离不在这里判：执行账号的 GRANT 只授到具体库，越库会被 MySQL 拒绝
    return errs


# ---------------------------------------------------------------- 入口


def execute(
    sql: str,
    env: str,
    database: str,
    policy: Policy,
    provider: Provider,
    session_factory,
    env_cfg: dict,
    store: ExecutionStore,
    dry_run: str = DRY_OFF,
    operator: str = "",
    sql_sha256: str = "",
    now: datetime | None = None,
    preflight: PreflightResult | None = None,
) -> ExecOutcome:
    out = ExecOutcome(dry_run=dry_run, operator=operator, env=env)
    dry_run = (dry_run or DRY_OFF).lower()
    if dry_run not in DRY_VALUES:
        # 入参错误也要给出可读的结论：只填 errors 的话，调用方看 blocked_by/refused_reason
        # 会是空的，无从判断到底是哪一步拒的。
        out.decision = DECISION_BLOCKED
        out.blocked_by = ["ARG_INVALID"]
        out.errors = [f"dry_run 只能是 {'/'.join(sorted(DRY_VALUES))}，当前 {dry_run!r}"]
        out.refused_reason = out.errors[0]
        return out

    # ---- 1. 判定
    pf = preflight or run_preflight(
        sql=sql, env=env, database=database, policy=policy, provider=provider,
        env_cfg=env_cfg, now=now)
    out.decision = pf.decision
    out.blocked_by = list(pf.blocked_by)
    out.warnings = list(pf.warnings)

    if pf.decision != DECISION_READY:
        out.checks = pf.checks
        out.errors = list(pf.errors)
        out.refused_reason = "判定未通过，未执行"
        return out

    # ---- 2. 只判定不执行
    if dry_run == DRY_PLAN:
        out.checks = pf.checks
        out.refused_reason = "dry_run=plan，只判定不执行"
        return out

    parsed = P.parse(sql, default_db=database)
    stmts = parsed.statements

    # ---- 3. 独立再校验
    errs = revalidate(sql, stmts, policy, env, env_cfg, sql_sha256)
    if errs:
        out.decision = DECISION_BLOCKED
        out.blocked_by = ["EXEC_REVALIDATE"]
        out.errors = errs
        out.refused_reason = "执行前再校验未通过，未执行"
        return out

    kinds = {st.kind for st in stmts}
    if dry_run == DRY_ROLLBACK and kinds & P.DDL_KINDS:
        out.decision = DECISION_BLOCKED
        out.blocked_by = ["DRY_RUN_DDL"]
        out.refused_reason = ("dry_run=rollback 只支持 DML：MySQL 的 DDL 隐式提交，"
                              "无法回滚，假装能回滚才是骗人")
        return out

    # ---- 4. 执行
    eid = store.new_id(now)
    out.execution_id = eid

    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001  连不上就先别建 undo 文件
        out.decision = DECISION_BLOCKED
        out.blocked_by = ["CONNECT"]
        out.refused_reason = f"建立执行会话失败：{type(exc).__name__}: {exc}"
        out.errors = [out.refused_reason]
        return out

    try:
        undo = store.open_undo(eid)
    except Exception as exc:  # noqa: BLE001  undo 建不起来绝对不能往下走：没有写前落盘就没有回滚
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
        out.decision = DECISION_BLOCKED
        out.blocked_by = ["UNDO_INIT"]
        out.refused_reason = f"undo 日志创建失败，不执行：{type(exc).__name__}: {exc}"
        out.errors = [out.refused_reason]
        return out

    thresholds = policy.thresholds
    ctx = Ctx(
        session=session, provider=provider, undo=undo, database=database,
        operator=operator, dry_run=dry_run,
        max_affected_rows=int(thresholds.get("max_affected_rows", 10000)),
        timeout=int(thresholds.get("exec_timeout_seconds", 30)),
        session_factory=session_factory,
    )

    try:
        _apply_session_vars(ctx, thresholds, out)
        if kinds & P.DML_KINDS:
            _run_dml(ctx, stmts, out)
        else:
            _run_ddl(ctx, stmts, out)
    except Exception as exc:  # noqa: BLE001  兜底：任何未捕获异常都要回滚
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        out.errors.append(f"{type(exc).__name__}: {exc}")
        out.errors.extend(traceback.format_exc(limit=3).splitlines()[-3:])
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 5. 回滚 SQL（只覆盖实际生效的语句）
    # undo 必须在任何分支下都关闭：_build_rollback 抛异常时句柄不能漏
    try:
        out.rollback = _build_rollback(stmts, ctx, out.results)
    finally:
        if undo is not None:
            out.undo = undo.digest()
            undo.close()
    out.undo_records = ctx.records

    # ---- 6. 落记录
    record = {
        "execution_id": eid,
        "env": env,
        "database": database,
        "operator": operator,
        "dry_run": dry_run,
        "at": datetime.now().isoformat(timespec="seconds"),
        "sql": sql,
        "sql_sha256": sha256_of(sql),
        "policy_version": policy.version,
        "decision": out.decision,
        "executed": out.executed,
        "committed": out.committed,
        "blocked_by": out.blocked_by,
        "warnings": out.warnings,
        "errors": out.errors,
        "results": [r.as_dict() for r in out.results],
        "rollback": out.rollback.to_dict(None) if out.rollback else None,
        "undo": out.undo,
    }
    try:
        store.save(record)
        store.append_audit({
            "execution_id": eid, "at": record["at"], "env": env, "database": database,
            "operator": operator, "dry_run": dry_run, "executed": out.executed,
            "committed": out.committed, "statement_count": len(out.results),
            "affected_rows": sum(r.affected_rows or 0 for r in out.results),
            "rollback_available": bool(out.rollback and out.rollback.available),
            "undo": out.undo,
            "sql_sha256": record["sql_sha256"],
        })
    except Exception as exc:  # noqa: BLE001  审计写失败不能掩盖执行结果
        out.errors.append(f"审计落盘失败：{type(exc).__name__}: {exc}")

    return out


# ---------------------------------------------------------------- 会话参数


def _apply_session_vars(ctx: Ctx, thresholds: dict, out: ExecOutcome) -> None:
    """锁等待上限，避免 DDL 的 MDL 把后面的请求全堵住（设计 §8.1）。"""
    pairs = [
        ("lock_wait_timeout", int(thresholds.get("lock_wait_timeout", 10))),
        ("innodb_lock_wait_timeout", int(thresholds.get("lock_wait_timeout", 10))),
    ]
    failed = []
    for name, value in pairs:
        try:
            ctx.session.execute(f"SET SESSION {name}={value}")
        except Exception:  # noqa: BLE001
            failed.append(name)
    if failed:
        out.warnings.append(f"会话参数设置失败：{', '.join(failed)}（继续执行，但锁等待不可控）")
    if ctx.dry_run == DRY_ROLLBACK:
        try:
            ctx.session.execute(f"SET SESSION max_execution_time={int(thresholds.get('exec_timeout_seconds', 30)) * 1000}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- DML


def _run_dml(ctx: Ctx, stmts: list[P.Statement], out: ExecOutcome) -> None:
    ctx.session.begin()
    abort = ""
    for st in stmts:
        if abort:
            out.results.append(StmtResult(index=st.index, kind=st.kind, table=st.table or "",
                                          status=S_SKIPPED, notes=[abort]))
            continue
        res = _exec_insert(ctx, st) if st.kind == P.K_INSERT else _exec_update(ctx, st)
        out.results.append(res)

        if res.status == S_ERROR:
            abort = f"第 {st.index} 条失败，整批回滚"
            if res.error:
                out.errors.append(f"第 {st.index} 条执行报错：{res.error}")
        elif res.status == S_ABORT:
            abort = f"第 {st.index} 条执行前被拦，整批回滚"
        elif res.status == S_GUARD:
            abort = f"第 {st.index} 条影响行数超限，整批回滚"

    if abort:
        try:
            ctx.session.rollback()
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"ROLLBACK 失败：{type(exc).__name__}: {exc}")
        out.committed = False
        out.executed = False
        for r in out.results:
            r.committed = False
        out.errors.append(abort)
        return

    if ctx.dry_run == DRY_ROLLBACK:
        ctx.session.rollback()
        out.committed = False
        out.executed = False
        for r in out.results:
            r.committed = False
            r.status = S_DRY
        out.refused_reason = "dry_run=rollback：事务已执行并回滚，affected_rows 为真实值"
        return

    ctx.session.commit()
    out.committed = True
    out.executed = True
    for r in out.results:
        r.committed = True


def _columns(ctx: Ctx, db: str, table: str) -> dict[str, dict]:
    cols = ctx.provider.columns(db, table) if (db and table) else None
    return cols or {}


def _exec_insert(ctx: Ctx, st: P.Statement) -> StmtResult:
    t0 = time.perf_counter()
    db = ctx.db_of(st)
    full = RB.qname(db, st.table or "")
    res = StmtResult(index=st.index, kind=st.kind, table=st.table or "")
    ctx.use_db(db)          # 非限定名 SQL 必须先落库

    cols = _columns(ctx, db, st.table or "")
    pk = [m.get("name", k) for k, m in cols.items() if m.get("primary")]
    auto_pk = [m.get("name", k) for k, m in cols.items()
               if m.get("primary") and m.get("auto_increment")]
    explicit, rejected = RB.explicit_pk_values(st, pk)
    if rejected and pk:
        # 有主键却定位不到，说明主键是函数/表达式，回滚只能靠自增区间
        res.notes.append(f"主键无法按字面量定位：{rejected}")

    # ---- 写前落盘
    ctx.write_undo({
        "type": "insert", "stmt_index": st.index, "table": full,
        "pk_columns": pk, "explicit_pk_values": explicit,
        "explicit_rejected": rejected,
        "insert_rows": st.insert_rows,
        "sql": st.raw,
    })

    try:
        n = ctx.session.execute(st.raw)
    except Exception as exc:  # noqa: BLE001
        res.status = S_ERROR
        res.error = f"{type(exc).__name__}: {exc}"
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res

    res.duration_ms = int((time.perf_counter() - t0) * 1000)
    res.affected_rows = n
    if st.insert_rows is not None and n != st.insert_rows:
        res.notes.append(f"VALUES 给了 {st.insert_rows} 行，实际受影响 {n} 行")

    # ---- 无显式主键时，用自增区间定位（只能执行后补记）
    if not explicit and len(auto_pk) == 1 and n > 0:
        lid = None
        try:
            lid = ctx.session.scalar("SELECT LAST_INSERT_ID() AS lid")
        except Exception:  # noqa: BLE001
            lid = None
        inc = 1
        try:
            inc = int(ctx.session.scalar("SELECT @@auto_increment_increment AS inc") or 1)
        except Exception:  # noqa: BLE001
            inc = 1
        if lid:
            start = int(lid)
            end = start + (n - 1) * inc
            continuous = _range_is_clean(ctx, full, auto_pk[0], start, end, n)
            ctx.write_undo({
                "type": "insert_after", "stmt_index": st.index, "table": full,
                "pk_range": {"column": auto_pk[0], "start": start, "end": end,
                             "continuous": continuous},
                "last_insert_id": lid, "row_count": n,
            })
            if not continuous:
                res.notes.append("自增区间内行数与写入行数不一致，回滚 SQL 需人工核对")

    # ---- 执行后校验
    res.verify = _verify_insert(ctx, db, st.table or "", explicit)
    return res


def _range_is_clean(ctx: Ctx, full: str, col: str, start: int, end: int, n: int) -> bool:
    try:
        got = ctx.session.scalar(
            f"SELECT COUNT(*) AS c FROM {full} WHERE {RB.q(col)} BETWEEN {start} AND {end}")
        return int(got or 0) == int(n)
    except Exception:  # noqa: BLE001
        return False


def _verify_insert(ctx: Ctx, db: str, table: str, explicit: list[dict] | None) -> str:
    """执行后确认插入的行真的在位。

    必须按**全部**显式主键核对：只拿第一行拼 WHERE，再跟总行数比，多行 INSERT
    会永远报「expected 2, found 1」这种假警报（2026-11-04 实测踩到）。
    """
    if not explicit:
        return ""
    cols = list(explicit[0].keys())
    if len(cols) == 1:
        vals = ", ".join(str(v[cols[0]]) for v in explicit)
        where = f"{RB.q(cols[0])} IN ({vals})"
    else:
        tuples = ", ".join("(" + ", ".join(str(v[c]) for c in cols) + ")" for v in explicit)
        where = f"({', '.join(RB.q(c) for c in cols)}) IN ({tuples})"
    try:
        got = ctx.session.scalar(f"SELECT COUNT(*) AS c FROM {RB.qname(db, table)} WHERE {where}")
    except Exception:  # noqa: BLE001
        return "unknown"
    if got is None:
        return "unknown"
    return "ok" if int(got) == len(explicit) else f"expected {len(explicit)}, found {got}"


def _exec_update(ctx: Ctx, st: P.Statement) -> StmtResult:
    t0 = time.perf_counter()
    db = ctx.db_of(st)
    full = RB.qname(db, st.table or "")
    res = StmtResult(index=st.index, kind=st.kind, table=st.table or "")
    ctx.use_db(db)          # 非限定名 SQL 必须先落库

    cols = _columns(ctx, db, st.table or "")
    pk = [m.get("name", k) for k, m in cols.items() if m.get("primary")]
    actual = {k: m.get("name", k) for k, m in cols.items()}
    set_names = [actual.get(c.upper(), c) for c in st.set_columns]

    if not pk:
        res.notes.append("表无主键，无法采集 before-image")
    if not st.where_full:
        # EX-U03 已拦无 WHERE 的 UPDATE，这里只是兜底：真走到这里必须说清楚，
        # 不能静默跳过 before-image 却给出「回滚不可用」这种没头没尾的结论。
        res.notes.append("未解析出 WHERE 条件，跳过 before-image，回滚不可用")

    # ---- before-image：与 UPDATE 完全相同的 WHERE/ORDER/LIMIT，加行锁
    before: list[dict] = []
    if pk and st.where_full:
        select_cols = ", ".join(RB.q(c) for c in dict.fromkeys(pk + set_names))
        where = st.where_full
        if ctx.max_affected_rows and not _has_limit(where):
            where += f" LIMIT {ctx.max_affected_rows + 1}"
        sel = f"SELECT {select_cols} FROM {full} WHERE {where} FOR UPDATE"
        ctx.write_undo({"type": "update_before", "stmt_index": st.index,
                        "table": full, "before_image_sql": sel, "sql": st.raw})
        try:
            before = ctx.session.query(sel)
        except Exception as exc:  # noqa: BLE001
            res.status = S_ERROR
            res.error = f"before-image 采集失败：{type(exc).__name__}: {exc}"
            res.duration_ms = int((time.perf_counter() - t0) * 1000)
            return res
        res.matched_rows = len(before)
        if len(before) > ctx.max_affected_rows:
            res.status = S_ABORT
            res.notes.append(
                f"before-image 已命中 {len(before)} 行，超过上限 {ctx.max_affected_rows}，执行前即放弃")
            res.duration_ms = int((time.perf_counter() - t0) * 1000)
            return res
        # 写前落盘：逐行旧值
        for row in before:
            ctx.write_undo({
                "type": "update_row", "stmt_index": st.index, "table": full,
                "pk": {c: row.get(c) for c in pk},
                "old": {c: row.get(c) for c in set_names if c in row},
            })

    # ---- 执行
    try:
        n = ctx.session.execute(st.raw)
    except Exception as exc:  # noqa: BLE001
        res.status = S_ERROR
        res.error = f"{type(exc).__name__}: {exc}"
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res

    res.affected_rows = n
    res.duration_ms = int((time.perf_counter() - t0) * 1000)

    # ---- 执行后二次校验：TOCTOU 兜底
    if n > ctx.max_affected_rows:
        res.status = S_GUARD
        res.notes.append(f"ROW_COUNT={n} 超过上限 {ctx.max_affected_rows}，事务已回滚")
    if res.matched_rows is not None and res.matched_rows != n:
        res.notes.append(
            f"匹配 {res.matched_rows} 行、实际变更 {n} 行（值未变或期间被改），回滚按匹配行生成")
    if not pk:
        res.notes.append("无主键，回滚 SQL 不可用")
    return res


def _has_limit(text: str) -> bool:
    import re
    return bool(re.search(r"\bLIMIT\b", text, re.I))


# ---------------------------------------------------------------- DDL


def _run_ddl(ctx: Ctx, stmts: list[P.Statement], out: ExecOutcome) -> None:
    """DDL 逐条独立提交。第一条失败即停：后面的语句建立在前面成功的前提上。

    dry_run=rollback 在入口就被拒（DDL 隐式提交回不去），不会走到这里。
    """
    for st in stmts:
        res = _exec_ddl(ctx, st)
        out.results.append(res)
        if res.status == S_ERROR:
            out.errors.append(f"第 {st.index} 条 DDL 失败，后续语句未执行")
            break

    out.executed = any(r.status == S_OK for r in out.results)
    out.committed = out.executed


def _exec_ddl(ctx: Ctx, st: P.Statement) -> StmtResult:
    t0 = time.perf_counter()
    db = ctx.db_of(st)
    full = RB.qname(db, st.table or "")
    res = StmtResult(index=st.index, kind=st.kind, table=st.table or "")
    ctx.use_db(db)          # 非限定名 SQL 必须先落库

    snap = None
    if st.kind in (P.K_ALTER_TABLE, P.K_CREATE_INDEX):
        ddl = show_create(ctx.session, db, st.table or "")
        if ddl:
            snap = from_ddl(ddl, db, st.table or "")

    plan = RB.ddl_reverse(st, snap, db)
    if not plan.available:
        res.notes.append(f"回滚不可用：{plan.note}")

    # ---- 写前落盘：DDL 反向语句 + 原结构快照
    ctx.write_undo({
        "type": "ddl", "stmt_index": st.index, "table": full, "sql": st.raw,
        "undo_sql": plan.sql, "undo_available": plan.available, "undo_note": plan.note,
        "snapshot_create_sql": (snap.create_sql if snap else None),
    })

    try:
        n = ctx.session.execute(st.raw)
        ctx.session.commit()        # DDL 隐式提交，这里显式确认
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        # 大表 DDL 常常跑过 read_timeout：连接断了但语句还在服务端跑，甚至已经成功。
        # 这时直接报「失败」是错误信息，必须另开一条连接去探测对象到底在不在。
        res.verify = _verify_ddl_fresh(ctx, st, db)
        if res.verify == "ok":
            res.status = S_OK
            res.committed = True
            res.notes.append(
                "执行时连接异常，但事后探测到对象已生效：DDL 很可能已成功，"
                "请人工复核后再决定是否需要回滚")
        else:
            res.status = S_ERROR
            res.notes.append(
                "执行时连接异常且探测不到对象：DDL 可能仍在服务端执行，"
                "必须人工确认结果后再做后续操作")
        return res

    res.affected_rows = n
    res.duration_ms = int((time.perf_counter() - t0) * 1000)
    res.committed = True
    res.verify = _verify_ddl(ctx.session, st, db)
    return res


def _verify_ddl_fresh(ctx: Ctx, st: P.Statement, db: str) -> str:
    """原连接已断时，另开一条连接探测 DDL 结果。

    不能因为「探测不到」就断言失败，也不能因为「连不上」就断言成功，
    所以拿不到就返回 unknown，让人工复核。
    """
    if not ctx.session_factory:
        return "unknown"
    try:
        probe: Session = ctx.session_factory()
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        return _verify_ddl(probe, st, db)
    finally:
        try:
            probe.close()
        except Exception:  # noqa: BLE001
            pass


def _verify_ddl(session: Session, st: P.Statement, db: str) -> str:
    """执行后确认对象真的到位；查不到只标 unknown，不判失败。"""
    table = st.table or ""
    try:
        if st.kind == P.K_CREATE_TABLE:
            hit = session.scalar(
                "SELECT 1 AS c FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, table))
            return "ok" if hit else "not_found"
        if st.kind == P.K_CREATE_INDEX:
            hit = session.scalar(
                "SELECT 1 AS c FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s",
                (db, table, st.index_name))
            return "ok" if hit else "not_found"
        if st.kind == P.K_ALTER_TABLE:
            missing = []
            for op in st.ops:
                if op.kind == P.OP_ADD_COLUMN and op.column:
                    hit = session.scalar(
                        "SELECT 1 AS c FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                        (db, table, op.column))
                    if not hit:
                        missing.append(op.column)
                if op.kind in (P.OP_ADD_INDEX, P.OP_ADD_PRIMARY) and op.index_name:
                    hit = session.scalar(
                        "SELECT 1 AS c FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s",
                        (db, table, op.index_name))
                    if not hit:
                        missing.append(op.index_name)
            return "ok" if not missing else f"missing: {missing}"
    except Exception:  # noqa: BLE001
        return "unknown"
    return ""


def render_text(out: ExecOutcome, rollback_limit: int = 20) -> str:
    """人读版执行结果，CLI 与联调都用它。"""
    head = "已提交" if out.committed else ("已回滚" if out.executed is False and out.results
                                          else "未执行")
    lines = [
        f"结论：{out.decision}｜执行：{head}｜dry_run={out.dry_run}",
        f"execution_id：{out.execution_id or '-'}",
    ]
    if out.refused_reason:
        lines.append(f"说明：{out.refused_reason}")
    if out.blocked_by:
        lines.append(f"阻塞项：{', '.join(out.blocked_by)}")

    if out.results:
        lines.append("")
        lines.append("逐条结果：")
        for r in out.results:
            bits = [f"#{r.index}", r.kind, r.status]
            if r.matched_rows is not None:
                bits.append(f"matched={r.matched_rows}")
            if r.affected_rows is not None:
                bits.append(f"affected={r.affected_rows}")
            bits.append(f"{r.duration_ms}ms")
            if r.verify:
                bits.append(f"verify={r.verify}")
            lines.append("  " + "  ".join(bits))
            if r.error:
                lines.append(f"      错误：{r.error}")
            for n in r.notes:
                lines.append(f"      - {n}")

    rb = out.rollback
    if rb is not None:
        lines.append("")
        if rb.available:
            lines.append(f"回滚 SQL：{len(rb.sql)} 条（undone_by={rb.undone_by}）")
            for s in rb.sql[:rollback_limit]:
                lines.append(f"  {s}")
            if len(rb.sql) > rollback_limit:
                lines.append(f"  …其余 {len(rb.sql) - rollback_limit} 条见审计记录")
        else:
            lines.append(f"回滚 SQL：不可用 —— {rb.note}")
        if rb.available and rb.note:
            lines.append(f"注意：{rb.note}")

    if out.undo:
        lines.append("")
        lines.append(f"undo 日志：{out.undo.get('path')}"
                     f"（{out.undo.get('lines')} 行，sha256 {str(out.undo.get('sha256'))[:16]}…）")
    if out.warnings:
        lines.append(f"风险提示：{', '.join(out.warnings)}")
    for e in out.errors:
        lines.append(f"错误：{e}")
    return "\n".join(lines)


def _build_rollback(stmts: list[P.Statement], ctx: Ctx,
                    results: list[StmtResult]) -> RB.RollbackPlan:
    """只为「真的生效了」的语句生成回滚，不按输入语句列表一把梭。

    两种错法都很要命：
      · DML 整批回滚（执行报错 / 执行前拦截 / 行数超限）后一条都没落地，
        却照样给出「可用」的 DELETE/UPDATE，DBA 会去撤一个不存在的变更；
      · DDL 批里第 2 条失败，第 3 条根本没执行，却也被生成回滚语句。
    """
    live = {r.index for r in results if r.status in (S_OK, S_DRY)}
    if not live:
        return RB.RollbackPlan(
            available=False,
            note="本次没有任何语句实际生效（未执行或已整批回滚），无需回滚")

    plans: list[RB.RollbackPlan] = []
    for st in stmts:
        if st.index not in live:
            continue
        db = ctx.db_of(st)
        if st.kind == P.K_INSERT:
            plans.append(RB.insert_reverse(ctx.records, db, st.table or "", st.index))
        elif st.kind == P.K_UPDATE:
            plans.append(RB.update_reverse(ctx.records, db, st.table or "", st.index))
        else:
            snap = next((r.get("snapshot_create_sql") for r in ctx.records
                         if r.get("type") == "ddl" and r.get("stmt_index") == st.index), None)
            plans.append(RB.ddl_reverse(st, from_ddl(snap, db, st.table or "") if snap else None, db))

    if not plans:
        return RB.RollbackPlan(available=False, note="无语句")
    sql = [s for p in plans for s in p.sql]
    notes = [p.note for p in plans if p.note]
    return RB.RollbackPlan(
        available=all(p.available for p in plans),
        sql=sql,
        undone_by=plans[0].undone_by,
        note="；".join(dict.fromkeys(notes)),
    )
