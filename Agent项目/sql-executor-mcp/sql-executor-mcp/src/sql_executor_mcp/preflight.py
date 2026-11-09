"""preflight：判定一条（批）SQL 在当前环境能不能执行。

不执行任何语句，只采事实 + 跑规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import parser as P
from .backend import Provider
from .engine import LEVEL_BLOCK, Policy, PolicyHolder, evaluate, render
from .facts import CollectCtx, FactSet, collect

DECISION_READY = "READY"
DECISION_BLOCKED = "BLOCKED"


@dataclass
class PreflightResult:
    decision: str = DECISION_BLOCKED
    policy_version: int = 0
    policy_mode: str = "live"
    statement_count: int = 0
    statements: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    missing_facts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    policy_warning: str | None = None

    def as_dict(self) -> dict:
        d = {
            "decision": self.decision,
            "policy_version": self.policy_version,
            "policy_mode": self.policy_mode,
            "statement_count": self.statement_count,
            "statements": self.statements,
            "checks": self.checks,
            "blocked_by": self.blocked_by,
            "warnings": self.warnings,
            "facts": self.facts,
            "missing_facts": self.missing_facts,
        }
        if self.errors:
            d["errors"] = self.errors
        if self.policy_warning:
            d["policy_warning"] = self.policy_warning
        return d


def _stmt_brief(st: P.Statement) -> dict:
    brief = {"index": st.index, "type": st.kind, "database": st.database, "table": st.table}
    if st.kind == P.K_ALTER_TABLE:
        brief["ops"] = [op.kind for op in st.ops]
    if st.kind == P.K_CREATE_INDEX:
        brief["index_name"] = st.index_name
        brief["index_columns"] = st.index_columns
    if st.kind == P.K_INSERT:
        brief["insert_rows"] = st.insert_rows
    if st.kind == P.K_UPDATE:
        brief["has_where"] = st.has_where
        brief["limit"] = st.limit_value
    return brief


def run(
    sql: str,
    env: str,
    database: str,
    policy: Policy,
    provider: Provider,
    env_cfg: dict | None = None,
    recent_same_sql_count: int = 0,
    now: datetime | None = None,
) -> PreflightResult:
    res = PreflightResult(
        policy_version=policy.version,
        policy_mode=policy.mode,
    )

    parsed = P.parse(sql, default_db=database)
    if not parsed.ok:
        res.errors = list(parsed.errors)
        res.decision = DECISION_BLOCKED
        res.statement_count = len(parsed.statements)
        res.blocked_by = ["PARSE"]
        return res
    if not parsed.statements:
        res.errors = ["未识别到任何 SQL 语句"]
        res.decision = DECISION_BLOCKED
        res.blocked_by = ["PARSE"]
        return res

    stmts = parsed.statements
    res.statement_count = len(stmts)
    res.statements = [_stmt_brief(s) for s in stmts]

    # 目标库必须能确定：要么入参给了，要么 SQL 自带库名前缀
    if not database and not any(s.database for s in stmts):
        res.errors = ["未指定目标库：database 参数与 SQL 库名前缀至少要有一个"]
        res.decision = DECISION_BLOCKED
        res.blocked_by = ["DATABASE"]
        return res

    cfg = env_cfg if env_cfg is not None else policy.env_cfg(env)
    if not cfg:
        res.errors = [f"环境 {env} 未在 policy.environments 中配置"]
        res.decision = DECISION_BLOCKED
        res.blocked_by = ["ENV"]
        return res

    ctx = CollectCtx(
        env=env,
        env_cfg=cfg,
        scope=policy.scope,
        thresholds=policy.thresholds,
        provider=provider,
        default_db=database,
        recent_same_sql_count=recent_same_sql_count,
        now=now or datetime.now(),
    )

    fact_sets: list[FactSet] = collect(stmts, ctx)

    blocked: list[str] = []
    warns: list[str] = []
    all_checks: list[dict] = []
    seen_check: set[str] = set()
    missing: set[str] = set()

    for st, fs in zip(stmts, fact_sets):
        hits = evaluate(policy, fs, st.kind)
        hit_map = {h.id: h for h in hits}
        for h in hits:
            if h.level == LEVEL_BLOCK:
                if h.id not in blocked:
                    blocked.append(h.id)
            else:
                if h.id not in warns:
                    warns.append(h.id)
        # 全量检查项（含未命中），联调时能一眼看到判了哪些
        for chk in policy.checks:
            if not chk.enabled or not chk.applies(st.kind):
                continue
            if chk.id in seen_check:
                continue
            seen_check.add(chk.id)
            h = hit_map.get(chk.id)
            item = {"id": chk.id, "level": chk.level, "title": chk.title, "hit": bool(h)}
            if h:
                item["message"] = h.message
                if h.on_missing_hit:
                    item["because"] = "fact_unavailable"
            all_checks.append(item)
        missing |= set(fs.missing)

    res.checks = all_checks
    res.blocked_by = blocked
    res.warnings = warns
    res.decision = DECISION_BLOCKED if blocked else DECISION_READY
    res.facts = fact_sets[0].only if fact_sets else {}
    # 只回传与判定相关的事实缺失，避免噪音
    relevant = set()
    for chk in policy.checks:
        if chk.id in blocked or chk.id in warns:
            relevant |= _facts_of(chk.when)
    res.missing_facts = sorted(missing & relevant)
    return res


def _facts_of(node) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        if "fact" in node:
            out.add(node["fact"])
        for v in node.values():
            out |= _facts_of(v)
    elif isinstance(node, list):
        for v in node:
            out |= _facts_of(v)
    return out


def run_with_holder(
    sql: str, env: str, database: str, holder: PolicyHolder, provider: Provider,
    env_cfg: dict | None = None, recent_same_sql_count: int = 0, now: datetime | None = None,
) -> PreflightResult:
    policy = holder.policy
    res = run(sql, env, database, policy, provider, env_cfg,
              recent_same_sql_count, now)
    res.policy_warning = holder.warning
    return res


def render_text(res: PreflightResult) -> str:
    """人读版结论，execute 与 CLI 都用它。"""
    lines = [f"结论：{res.decision}（policy v{res.policy_version}）", ""]
    if res.errors:
        lines.append("阻塞原因：")
        for e in res.errors:
            lines.append(f"  - {e}")
    if res.blocked_by:
        lines.append("未通过的检查项：")
        for item in res.checks:
            if item["id"] in res.blocked_by:
                msg = item.get("message") or item["title"]
                lines.append(f"  ❌ {item['id']} {msg}")
    if res.warnings:
        lines.append("风险提示：")
        for item in res.checks:
            if item["id"] in res.warnings:
                lines.append(f"  ⚠️ {item['id']} {item.get('message') or item['title']}")
    if res.decision == DECISION_READY and not res.warnings:
        lines.append("✅ 全部检查通过")
    if res.missing_facts:
        lines.append("")
        lines.append("无法取到的事实：" + "、".join(res.missing_facts))
    return "\n".join(lines)
