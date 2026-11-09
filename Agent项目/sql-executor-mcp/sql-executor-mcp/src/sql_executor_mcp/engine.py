"""规则引擎：规则来自 `standards/executor.md`，本模块只负责求值与热加载。

三值语义（与 md 里「缺失」字段配合）：
  True  命中
  False 未命中
  None  依赖的事实取不到 → 交给规则的「缺失」处理（默认 block 级 fail-closed、warn 级跳过）

规则文件不再用 YAML：解析与校验在 `spec.py`，本模块保留
`Policy` / `Check` 数据结构与 `eval_cond` 求值器，改标准不用改这里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import parser as P
from .facts import FACT_SPECS, FactSet

LEVEL_BLOCK = "block"
LEVEL_WARN = "warn"

VALID_TYPES = set(P.DDL_KINDS) | set(P.DML_KINDS)


class PolicyError(Exception):
    """标准文件写错时抛这个：加载期就失败，不允许带病启动。"""


@dataclass
class Check:
    id: str
    level: str
    title: str
    when: dict
    message: str = ""
    reason: str = ""
    applies_to: list[str] = field(default_factory=lambda: ["all"])
    on_missing: str | None = None
    enabled: bool = True

    def applies(self, kind: str) -> bool:
        return "all" in self.applies_to or kind in self.applies_to


@dataclass
class Policy:
    version: int
    engine: str
    mode: str
    environments: dict
    scope: dict
    thresholds: dict
    checks: list[Check]
    path: str = ""
    raw: dict = field(default_factory=dict)
    standard_version: str = ""
    sha256: str = ""

    def allowed_types_for(self, env: str) -> list[str]:
        """环境级开放类型；没配就退到 scope（兼容旧配置）。"""
        cfg = self.environments.get(env) or {}
        return list(cfg.get("allowed_types") or self.scope.get("allowed_types") or [])

    def env_cfg(self, env: str) -> dict:
        return dict(self.environments.get(env) or {})

    def resolve_var(self, name: str, facts: FactSet | None = None):
        if name in self.thresholds:
            return self.thresholds[name]
        if name in self.scope:
            return self.scope[name]
        if facts is not None and facts.has(name):
            return facts.get(name)
        return None

    def counts(self) -> dict:
        block = sum(1 for c in self.checks if c.enabled and c.level == LEVEL_BLOCK)
        warn = sum(1 for c in self.checks if c.enabled and c.level == LEVEL_WARN)
        return {"rules_count": len(self.checks), "block_count": block, "warn_count": warn}


# ---------------------------------------------------------------- 加载

def load_policy(path: str | Path) -> Policy:
    """沿用旧名字：入参现在是标准 md（如 standards/executor.md），不再是 policy.yaml。"""
    from . import spec
    return spec.load_standard(path)


class PolicyHolder:
    """按 mtime + size 热加载；校验失败不切换，保留上一版并给出告警。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._sig: tuple | None = None
        self._policy: Policy | None = None
        self.warning: str | None = None
        self.reload(force=True)

    def _sig_of(self) -> tuple:
        st = os.stat(self.path)
        return (st.st_mtime_ns, st.st_size)

    def reload(self, force: bool = False) -> Policy:
        sig = self._sig_of()
        if not force and sig == self._sig and self._policy is not None:
            return self._policy
        try:
            policy = load_policy(self.path)
        except Exception as exc:  # noqa: BLE001
            if self._policy is None:
                raise
            self.warning = f"标准文件校验失败，继续使用上一版：{exc}"
            self._sig = sig
            return self._policy
        self._policy = policy
        self._sig = sig
        self.warning = None
        return policy

    @property
    def policy(self) -> Policy:
        return self.reload()


# ---------------------------------------------------------------- 求值

def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [v]


def _cmp(op: str, left, right) -> bool | None:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op in ("gt", "ge", "lt", "le"):
        if left is None or right is None:
            return None
        try:
            if op == "gt":
                return left > right
            if op == "ge":
                return left >= right
            if op == "lt":
                return left < right
            return left <= right
        except TypeError:
            return None
    if op == "in":
        return left in _as_list(right)
    if op == "not_in":
        return left not in _as_list(right)
    if op == "is_null":
        return left is None
    if op == "not_null":
        return left is not None
    if op == "is_empty":
        return len(_as_list(left)) == 0
    if op == "not_empty":
        return len(_as_list(left)) > 0
    if op == "eq_set":
        return set(_as_list(left)) == set(_as_list(right))
    if op == "intersects":
        return bool(set(_as_list(left)) & set(_as_list(right)))
    raise PolicyError(f"未知操作符 {op!r}")


def eval_leaf(node: dict, facts: FactSet, policy: Policy) -> bool | None:
    name = node["fact"]
    op = node.get("op", "eq")
    raw_value = node.get("value")
    value = policy.resolve_var(raw_value[1:], facts) if isinstance(raw_value, str) \
        and raw_value.startswith("$") else raw_value

    if op == "is_null":
        # 专门判「取不到 / 为空」的情形，不受 missing 影响
        result: bool | None = facts.get(name) is None
    elif name not in facts.values or name in facts.missing:
        result = None
    else:
        result = _cmp(op, facts.get(name), value)

    sub = node.get("and")
    if sub is not None:
        other = eval_cond(sub, facts, policy)
        if other is False or result is False:
            return False
        if other is None or result is None:
            return None
        return True
    return result


def eval_cond(node: dict, facts: FactSet, policy: Policy) -> bool | None:
    if "all" in node:
        saw_none = False
        for item in node["all"]:
            r = eval_cond(item, facts, policy)
            if r is False:
                return False
            if r is None:
                saw_none = True
        return None if saw_none else True
    if "any" in node:
        saw_none = False
        for item in node["any"]:
            r = eval_cond(item, facts, policy)
            if r is True:
                return True
            if r is None:
                saw_none = True
        return None if saw_none else False
    if "not" in node:
        r = eval_cond(node["not"], facts, policy)
        return None if r is None else (not r)
    if "fact" in node:
        return eval_leaf(node, facts, policy)
    raise PolicyError(f"无法识别的条件节点：{node!r}")


def render(template: str, facts: FactSet, policy: Policy) -> str:
    import re

    def sub(m) -> str:
        key = m.group(1)
        val = facts.get(key, policy.resolve_var(key, facts))
        if val is None:
            return m.group(0)
        if isinstance(val, (list, tuple)):
            return ", ".join(str(x) for x in val) if val else "（空）"
        return str(val)

    return re.sub(r"\{(\w+)\}", sub, template or "")


# ---------------------------------------------------------------- 结果

@dataclass
class Hit:
    id: str
    level: str
    title: str
    message: str
    reason: str = ""
    on_missing_hit: bool = False

    def as_dict(self) -> dict:
        d = {"id": self.id, "level": self.level, "title": self.title, "message": self.message}
        if self.reason:
            d["reason"] = self.reason
        if self.on_missing_hit:
            d["because"] = "fact_unavailable"
        return d


def evaluate(policy: Policy, facts: FactSet, kind: str) -> list[Hit]:
    """对单条语句跑全部适用检查，返回命中的项（block 与 warn 都可能出现）。"""
    hits: list[Hit] = []
    for chk in policy.checks:
        if not chk.enabled or not chk.applies(kind):
            continue
        r = eval_cond(chk.when, facts, policy)
        if r is True:
            hits.append(Hit(chk.id, chk.level, chk.title,
                            render(chk.message, facts, policy), chk.reason))
        elif r is None:
            mode = chk.on_missing or (LEVEL_BLOCK if chk.level == LEVEL_BLOCK else "skip")
            if mode == "skip":
                continue
            hits.append(Hit(
                chk.id, mode if mode != "block" else LEVEL_BLOCK, chk.title,
                chk.reason or f"依赖的事实取不到，无法判定（{chk.title}）",
                on_missing_hit=True,
            ))
    return hits
