"""判定引擎：DML 表达式求值（三值逻辑）+ 结论渲染。

DDL 的"检查"由 ddlchecks.run_scope() 直接跑；DML 的"判定"是文档里的一行表达式，
由本模块对事实集求值。两边共用同一套 RuleSet（spec.py）与同一套渲染逻辑。
"""

from __future__ import annotations

import re

from .spec import Rule, RuleSet, render

# --------------------------------------------------------------------------
# 三值求值：True / False / None（算不出来）
# --------------------------------------------------------------------------


def _resolve(value, thresholds):
    if isinstance(value, str) and value.startswith("$"):
        return thresholds.get(value[1:])
    return value


def _eq(actual, expect) -> bool:
    if isinstance(actual, bool) or isinstance(expect, bool):
        return bool(actual) == bool(expect)
    if isinstance(actual, (int, float)) and isinstance(expect, (int, float)):
        return float(actual) == float(expect)
    return str(actual) == str(expect)


def _leaf(cond: dict, facts, thresholds) -> bool | None:
    name = str(cond["fact"])
    op = str(cond["op"])
    if op == "is_null":
        return facts.get(name) is None
    if op == "not_null":
        return facts.get(name) is not None
    actual = facts.get(name)
    if actual is None:
        return None                      # 事实算不出来 → 不猜
    expect = _resolve(cond.get("value"), thresholds)
    if op == "is_empty":
        try:
            return len(actual) == 0
        except TypeError:
            return None
    if op == "not_empty":
        try:
            return len(actual) > 0
        except TypeError:
            return None
    if op == "in":
        return actual in (expect or [])
    if op == "not_in":
        return actual not in (expect or [])
    if op == "eq":
        return _eq(actual, expect)
    if op == "ne":
        return not _eq(actual, expect)
    if isinstance(actual, bool) or isinstance(expect, bool):
        return None
    try:
        if op == "gt":
            return actual > expect
        if op == "ge":
            return actual >= expect
        if op == "lt":
            return actual < expect
        if op == "le":
            return actual <= expect
    except TypeError:
        return None
    return None


def eval_condition(cond, facts, thresholds) -> bool | None:
    if not isinstance(cond, dict):
        return None
    if "all" in cond:
        vals = [eval_condition(c, facts, thresholds) for c in cond["all"]]
        if any(v is False for v in vals):
            return False
        if any(v is None for v in vals):
            return None
        return True
    if "any" in cond:
        vals = [eval_condition(c, facts, thresholds) for c in cond["any"]]
        if any(v is True for v in vals):
            return True
        if any(v is None for v in vals):
            return None
        return False
    if "not" in cond:
        v = eval_condition(cond["not"], facts, thresholds)
        return None if v is None else (not v)
    return _leaf(cond, facts, thresholds)


# --------------------------------------------------------------------------
# 跑规则
# --------------------------------------------------------------------------


class DmlHit:
    __slots__ = ("rule", "level", "message", "fix")

    def __init__(self, rule: Rule, level: str, message: str, fix: str):
        self.rule = rule
        self.level = level
        self.message = message
        self.fix = fix


def run_dml_rules(ruleset: RuleSet | None, facts, scope: str, target: str) -> list[DmlHit]:
    """对一条 DML（或批次）跑文档里的全部规则。

    事实取不到 → 标 ⏸ unknown（除非文档把 on_missing 设成 skip，这里统一按 unknown 处理）。
    """
    if ruleset is None:
        return []
    hits: list[DmlHit] = []
    th = ruleset.thresholds
    for rule in ruleset.enabled_rules(scope):
        if not rule.when:
            continue
        missing = [f for f in rule.require if facts.get(f) is None]
        if missing:
            hits.append(DmlHit(rule, "unknown",
                               f"缺少事实 {'、'.join(missing)}，无法判定「{rule.title}」",
                               "补齐 env + database（或由调用方传入表结构）后可复审"))
            continue
        verdict = eval_condition(rule.when, facts, th)
        if verdict is True:
            snapshot = {}
            for name in _placeholders(rule.message + rule.fix):
                snapshot[name] = facts.get(name)
            hits.append(DmlHit(rule, rule.level,
                               render(rule.message, snapshot, th) or rule.title,
                               render(rule.fix, snapshot, th)))
        elif verdict is None:
            hits.append(DmlHit(rule, "unknown",
                               f"语句写法导致相关事实无法静态确定，本条未判定「{rule.title}」",
                               "简化写法（单表、无嵌套子查询、函数不套在条件列上），"
                               "或补齐 env + database 后复审"))
    return hits


def _placeholders(template: str) -> list[str]:
    return [m for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template or "")]


def missing_facts(ruleset: RuleSet | None, facts, scope: str) -> list[str]:
    """本次审核里「本该用上但拿不到」的事实，供返回体提示研发补什么。"""
    if ruleset is None:
        return []
    need: list[str] = []
    for rule in ruleset.enabled_rules(scope):
        for fname in rule.require:
            if fname not in need and facts.get(fname) is None:
                need.append(fname)
    return sorted(need)
