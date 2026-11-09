"""统一规则规范解析器：把 standards/*.md 解析成可执行规则集。

设计目标（v1.4 规则全外置）
--------------------------
**规则本体一律在文档里**，代码只提供两类"判定原语"：

1. ``检查``（check）：结构化判定函数（DDL 用，见 ddlchecks.py），返回命中明细；
2. ``判定``（when）：一行布尔表达式（DML 用），对事实集求值（见 engine.py）。

文档里能改、改了立即生效（不用改代码、不用重启）的内容：

    级别 / 适用范围 / 阈值 / 参数 / 条款 / 说明 / 整改 / 情形文案 /
    停用开关 / 是否允许自动改写 / 反问目录

文档结构（ddl.md、dml.md 完全同构）
-----------------------------------

    # 标题
    **规则版本**：2026-11-03

    ## 阈值
    | 名称 | 值 | 说明 |
    | --- | --- | --- |
    | max_columns | 30 | 单表字段数上限 |

    ## 允许项            （可选，DML 用：声明"这样做是合规的"）
    ### DM-P01 允许单表 UPDATE 使用 LIMIT
    - **适用**：update
    - **判定**：has_limit = true
    - **条款**：...
    - **说明**：...

    ## 规则
    ### TB-03 禁用 FLOAT / DOUBLE 存浮点
    - **级别**：❌
    - **适用**：create_table, add_column
    - **检查**：forbidden_types
    - **参数**：types = FLOAT, DOUBLE, REAL; replace_with = DECIMAL(12,2)
    - **条款**：存储浮点数必须使用 DECIMAL，禁止使用 FLOAT 和 DOUBLE
    - **说明**：字段 {name} 使用了 {type}，必须改用 DECIMAL
    - **整改**：已改为 DECIMAL(12,2)
    - **自动改写**：true
    - **反问**：Q-DECIMAL (blocking)

每条规则必须有且只有一个 ``检查`` 或 ``判定``。
带 ``**情形**`` 的规则（同一规则多种命中形态）按 ``情形 = 级别 | 说明 | 整改`` 逐行声明。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

LEVELS = {"error", "warn", "info", "unknown"}

LEVEL_BY_MARK = {
    "❌": "error", "⚠️": "warn", "⚠": "warn", "✅": "info", "⏸": "unknown",
    "error": "error", "warn": "warn", "info": "info", "unknown": "unknown",
    "强制": "error", "推荐": "warn", "参考": "info",
}

# 语句范围：DDL 用语句类型，DML 用语句种类，batch 用于批次级规则
DDL_SCOPES = {
    "create_table", "alter_table", "add_column", "modify_column", "drop_column",
    "add_index", "drop_index", "create_index", "drop_table", "truncate",
    "drop_database",
}
DML_SCOPES = {"insert", "update", "delete", "replace", "batch"}
ALL_SCOPES = DDL_SCOPES | DML_SCOPES

RULE_ID_RE = re.compile(r"^(?P<prefix>[A-Z]{2,3})-(?P<num>P?\d+)$")
ITEM_RE = re.compile(r"^###\s+(?P<head>.+?)\s*$")
SECTION_RE = re.compile(r"^##\s+(?P<head>.+?)\s*$")
FIELD_RE = re.compile(r"^\s*[-*]\s*\*\*(?P<key>[^*]+?)\*\*\s*[：:]\s*(?P<val>.*?)\s*$")
VERSION_RE = re.compile(r"\*\*规则版本\*\*\s*[：:]\s*(?P<ver>\S+)")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# md 字段名 → 内部键
FIELD_KEYS = {
    "级别": "level",
    "适用": "applies_to",
    "检查": "check",
    "判定": "when",
    "依赖": "require",
    "参数": "params",
    "阈值": "thresholds",
    "条款": "clause",
    "说明": "message",
    "整改": "fix",
    "情形": "cases",
    "自动改写": "auto_fix",
    "反问": "ask",
    "停用": "enabled",
    "样例": "samples",
}

# `**样例**：命中 = <SQL>` / `**样例**：通过 = <SQL>` 的值前缀 → 内部键
SAMPLE_KINDS = {"命中": "hit", "必中": "hit", "hit": "hit", "命中案例": "hit",
                "通过": "pass", "不命中": "pass", "pass": "pass", "反例": "pass"}

_TRUE = {"true", "yes", "是", "on", "1"}
_FALSE = {"false", "no", "否", "off", "0"}


class SpecError(ValueError):
    """规范文档写错。带文件名与行号，便于直接改文档。"""

    def __init__(self, where: str, message: str):
        super().__init__(f"{where}: {message}")
        self.where = where
        self.message = message


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class RuleCase:
    """一种命中情形：级别 + 说明 + 整改（都可由文档覆盖）。"""

    case: str
    level: str = ""
    message: str = ""
    fix: str = ""


@dataclass
class Rule:
    id: str
    title: str
    level: str = "error"
    applies_to: tuple[str, ...] = ()
    check: str = ""                 # DDL：检查函数名
    when: dict | None = None        # DML：判定表达式
    require: tuple[str, ...] = ()
    params: dict = field(default_factory=dict)
    clause: str = ""
    message: str = ""
    fix: str = ""
    cases: dict[str, RuleCase] = field(default_factory=dict)
    auto_fix: bool = False
    asks: tuple[tuple[str, bool], ...] = ()
    enabled: bool = True
    samples: dict = field(default_factory=dict)   # {"hit": [SQL...], "pass": [SQL...]}
    doc: str = "ddl"                # ddl / dml
    line: int = 0

    # ---- 便捷 ----

    @property
    def category(self) -> str:
        prefix = self.id.split("-")[0]
        return {
            "TB": "建表", "IX": "索引", "AL": "变更", "DX": "破坏性", "SC": "规模",
            "DM": "DML",
        }.get(prefix, prefix)

    def applicable(self, scope: str) -> bool:
        return scope in self.applies_to

    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def p_list(self, key: str, default: list | None = None) -> list:
        v = self.params.get(key, default)
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    def p_bool(self, key: str, default: bool = False) -> bool:
        v = self.params.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in _TRUE

    def p_int(self, key: str, default: int = 0) -> int:
        v = self.params.get(key, default)
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default

    def case_of(self, name: str) -> RuleCase | None:
        return self.cases.get(name)

    def as_dict(self, include_samples: bool = False) -> dict:
        """转成返回体用的 dict。

        samples 默认不返回（57 条规则全带样例会让 get_rules 响应从 ~20KB 涨到 ~42KB），
        只有按 rule_id 单查时才带上。
        """
        data = {
            "id": self.id,
            "category": self.category,
            "scope": self.doc,
            "level": self.level,
            "title": self.title,
            "applies_to": list(self.applies_to),
            "check": self.check,
            "require": list(self.require),
            "params": self.params,
            "clause": self.clause,
            "message": self.message,
            "fix": self.fix,
            "cases": {k: {"level": v.level, "message": v.message, "fix": v.fix}
                      for k, v in self.cases.items()},
            "auto_fix": self.auto_fix,
            "asks": [{"id": q, "blocking": b} for q, b in self.asks],
            "enabled": self.enabled,
            "line": self.line,
        }
        if include_samples:
            data["samples"] = {k: list(v) for k, v in self.samples.items()}
        else:
            n = sum(len(v) for v in (self.samples or {}).values())
            if n:
                data["samples_count"] = n
        return data

    def sample_sqls(self, kind: str) -> list[str]:
        return list((self.samples or {}).get(kind) or [])


@dataclass
class Permit:
    id: str
    title: str
    clause: str = ""
    applies_to: tuple[str, ...] = ()
    leaf: dict | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "clause": self.clause,
                "applies_to": list(self.applies_to), "leaf": self.leaf or {}}


@dataclass
class RuleSet:
    """一份规范文档解析出来的规则集（DDL 或 DML）。"""

    path: str = ""
    doc: str = "ddl"
    sha256: str = ""
    rules_version: str = "unknown"
    title: str = ""
    thresholds: dict[str, int] = field(default_factory=dict)
    permits: list[Permit] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- 查询 ----

    def get(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def enabled_rules(self, scope: str | None = None) -> list[Rule]:
        out = [r for r in self.rules if r.enabled]
        if scope is None:
            return out
        return [r for r in out if r.applicable(scope)]

    def index(self) -> dict[str, Rule]:
        return {r.id: r for r in self.rules}


# --------------------------------------------------------------------------
# 判定表达式：词法 / 语法（DML 用；DDL 的"检查"不走这里）
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""\s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
    | (?P<thr>\$[A-Za-z_][A-Za-z0-9_]*)
    | (?P<str>'[^']*'|"[^"]*")
    | (?P<sym>>=|<=|!=|<>|==|=|>|<|\(|\)|,)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.X,
)

_CMP = {"=": "eq", "==": "eq", "!=": "ne", "<>": "ne",
        ">": "gt", ">=": "ge", "<": "lt", "<=": "le"}


def _tokenize_expr(text: str, line: int, where: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise SpecError(where, f"第 {line} 行判定表达式有无法识别的字符 {text[pos]!r}：{text}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _ExprParser:
    def __init__(self, toks: list[tuple[str, str]], line: int, text: str, where: str):
        self.toks = toks
        self.i = 0
        self.line = line
        self.text = text
        self.where = where

    def _err(self, msg: str):
        raise SpecError(self.where, f"第 {self.line} 行判定表达式：{msg}；原文 {self.text!r}")

    def _peek(self, offset: int = 0):
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else (None, None)

    def _eat(self):
        tok = self._peek()
        self.i += 1
        return tok

    def _is_word(self, *words: str) -> bool:
        kind, val = self._peek()
        return kind == "word" and str(val).lower() in {w.lower() for w in words}

    def parse(self) -> dict:
        node = self._or()
        if self.i < len(self.toks):
            self._err(f"多出的内容 {self._peek()[1]!r}")
        return node

    def _or(self) -> dict:
        parts = [self._and()]
        while self._is_word("or"):
            self._eat()
            parts.append(self._and())
        return parts[0] if len(parts) == 1 else {"any": parts}

    def _and(self) -> dict:
        parts = [self._unary()]
        while self._is_word("and"):
            self._eat()
            parts.append(self._unary())
        return parts[0] if len(parts) == 1 else {"all": parts}

    def _unary(self) -> dict:
        if self._is_word("not"):
            self._eat()
            return {"not": self._unary()}
        if self._peek()[0] == "sym" and self._peek()[1] == "(":
            self._eat()
            node = self._or()
            if not (self._peek()[0] == "sym" and self._peek()[1] == ")"):
                self._err("缺少右括号")
            self._eat()
            return node
        return self._atom()

    def _atom(self) -> dict:
        kind, val = self._peek()
        if kind != "word":
            self._err(f"这里应该是事实名，实际是 {val!r}")
        self._eat()
        fact = val
        if self._is_word("not") and self._peek(1)[0] == "word" \
                and str(self._peek(1)[1]).lower() == "in":
            self._eat()
            self._eat()
            return {"fact": fact, "op": "not_in", "value": self._list()}
        if self._is_word("in"):
            self._eat()
            return {"fact": fact, "op": "in", "value": self._list()}
        if self._is_word("is"):
            self._eat()
            negate = False
            if self._is_word("not"):
                self._eat()
                negate = True
            if self._is_word("null"):
                self._eat()
                return {"fact": fact, "op": "not_null" if negate else "is_null"}
            if self._is_word("empty"):
                self._eat()
                return {"fact": fact, "op": "not_empty" if negate else "is_empty"}
            if self._is_word("true", "false"):
                if negate:
                    self._err("不支持「is not true/false」，请写成 = false / = true")
                word = str(self._eat()[1]).lower()
                return {"fact": fact, "op": "eq", "value": word == "true"}
            self._err("is 后面只支持 null / not null / empty / not empty / true / false")
        kind, val = self._peek()
        if kind != "sym" or val not in _CMP:
            self._err(f"事实 {fact} 后面缺少比较操作符")
        self._eat()
        return {"fact": fact, "op": _CMP[val], "value": self._value()}

    def _value(self):
        kind, val = self._peek()
        if kind is None:
            self._err("比较操作符后面缺少值")
        self._eat()
        if kind == "num":
            return int(val) if re.fullmatch(r"-?\d+", val) else float(val)
        if kind == "thr":
            return val
        if kind == "str":
            return val[1:-1]
        if kind == "word":
            low = str(val).lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            return low
        self._err(f"无法识别的值 {val!r}")

    def _list(self) -> list:
        if not (self._peek()[0] == "sym" and self._peek()[1] == "("):
            self._err("in / not in 后面必须是括号列表，如 (delete, replace)")
        self._eat()
        items = [self._value()]
        while self._peek()[0] == "sym" and self._peek()[1] == ",":
            self._eat()
            items.append(self._value())
        if not (self._peek()[0] == "sym" and self._peek()[1] == ")"):
            self._err("括号列表缺少右括号")
        self._eat()
        return items


def parse_expr(text: str, line: int = 0, where: str = "") -> dict:
    text = (text or "").strip()
    if not text:
        raise SpecError(where, f"第 {line} 行「判定」为空；DDL 规则请写 `- **检查**：xxx`，DML 规则请写判定表达式")
    return _ExprParser(_tokenize_expr(text, line, where), line, text, where).parse()


# --------------------------------------------------------------------------
# 字段值解析
# --------------------------------------------------------------------------


def _strip_md(s: str) -> str:
    return s.replace("**", "").replace("`", "").strip()


def split_list(value: str) -> list[str]:
    return [x.strip().lower() for x in re.split(r"[,，、;；\s]+", value or "") if x.strip()]


def _parse_scalar(text: str) -> Any:
    t = _strip_md(text)
    low = t.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


def parse_params(value: str, line: int, where: str) -> dict:
    """``types = FLOAT, DOUBLE; replace_with = DECIMAL`` → dict。

    值里带逗号的自动转成列表；true/false 转布尔；纯整数转 int。
    """
    out: dict[str, Any] = {}
    for chunk in re.split(r"[;；]", value or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise SpecError(where, f"第 {line} 行「参数」缺少 `=`：{chunk!r}"
                                   "（格式：key = value; key2 = v1, v2）")
        key, _, val = chunk.partition("=")
        key = _strip_md(key)
        parent, dot, child = key.partition(".")
        if dot:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parent):
                raise SpecError(where, f"第 {line} 行参数名 {key!r} 只能由字母、数字、下划线组成")
            child = child.strip()
            if not child or re.search(r"[\s.]", child):
                raise SpecError(where, f"第 {line} 行参数子键 {child!r} 不能为空或含空格/点")
            out.setdefault(parent, {})
            if not isinstance(out[parent], dict):
                raise SpecError(where, f"第 {line} 行参数 {parent} 既当值又当子键，写法有冲突")
            out[parent][child] = _parse_scalar(val.strip())
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SpecError(where, f"第 {line} 行参数名 {key!r} 只能由字母、数字、下划线组成")
        raw = val.strip()
        parts = [p for p in re.split(r"[,，|]", raw) if p.strip() != ""]
        out[key] = ([_parse_scalar(p) for p in parts] if len(parts) > 1
                    else _parse_scalar(raw))
    return out


def parse_case(value: str, line: int, where: str) -> RuleCase:
    """``uppercase = ❌ | 表名必须大写 | 已改为 {fixed}`` → RuleCase。"""
    parts = [p.strip() for p in value.split("|")]
    if len(parts) < 2:
        raise SpecError(where, f"第 {line} 行「情形」格式应为 `情形名 = 级别 | 说明 | 整改`，收到 {value!r}")
    name_part, level_part = parts[0], parts[1]
    if "=" not in name_part:
        raise SpecError(where, f"第 {line} 行「情形」缺少 `情形名 = `：{value!r}")
    name = _strip_md(name_part.split("=", 1)[0])
    lv = LEVEL_BY_MARK.get(_strip_md(level_part))
    if lv is None:
        # 允许省略级别（只写 `情形名 | 说明 | 整改`）
        if "=" not in parts[0]:
            raise SpecError(where, f"第 {line} 行「情形」的级别 {level_part!r} 无法识别")
        lv = ""
    msg = parts[2] if len(parts) > 2 else ""
    fix = parts[3] if len(parts) > 3 else ""
    return RuleCase(case=name, level=lv, message=_strip_md(msg), fix=_strip_md(fix))


def parse_asks(value: str) -> tuple[tuple[str, bool], ...]:
    """``Q-DECIMAL (blocking); Q-COMMENT`` → ((Q-DECIMAL, True), (Q-COMMENT, False))。"""
    out: list[tuple[str, bool]] = []
    for chunk in re.split(r"[;；]", value or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(?P<qid>Q-[A-Za-z0-9-]+)\s*(?:\((?P<flag>[^)]*)\))?$", chunk)
        if not m:
            out.append((chunk, False))
            continue
        flag = (m.group("flag") or "").strip().lower()
        out.append((m.group("qid"), flag in ("blocking", "是", "true", "强制")))
    return tuple(out)


# --------------------------------------------------------------------------
# 文档解析
# --------------------------------------------------------------------------


def _parse_threshold_table(lines: list[tuple[int, str]], where: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for lineno, text in lines:
        t = text.strip()
        if not t.startswith("|"):
            continue
        cells = [c.strip() for c in t.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " ", ":"} or cells[0] == "名称":
            continue
        name = _strip_md(cells[0])
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise SpecError(where, f"第 {lineno} 行阈值名 {name!r} 只能由字母、数字、下划线组成")
        raw = _strip_md(cells[1]).replace(",", "").replace("_", "")
        try:
            out[name] = int(raw)
        except ValueError:
            raise SpecError(where, f"第 {lineno} 行阈值 {name} 必须是整数，收到 {cells[1]!r}") from None
    return out


def parse_doc(md: str, path: str = "", doc: str = "ddl") -> RuleSet:
    """把一份规范 md 解析成 RuleSet（结构解析，不做语义校验）。"""
    where = Path(path).name or doc
    lines = md.splitlines()
    title = ""
    for raw in lines:
        if raw.startswith("# "):
            title = _strip_md(raw[2:])
            break

    m = VERSION_RE.search(md)
    rs = RuleSet(path=str(path), doc=doc, sha256=hashlib.sha256(md.encode("utf-8")).hexdigest(),
                 rules_version=(m.group("ver") if m else "unknown"), title=title)

    # ---- 按 `## 章节` 归集行 ----
    sections: dict[str, list[tuple[int, str]]] = {}
    cur = ""
    for idx, raw in enumerate(lines, start=1):
        text = raw.rstrip()
        ms = SECTION_RE.match(text)
        if ms:
            cur = _strip_md(ms.group("head"))
            sections.setdefault(cur, [])
            continue
        if cur:
            sections[cur].append((idx, text))

    rs.thresholds = _parse_threshold_table(sections.get("阈值", []), where)

    # ---- 允许项 / 规则：`### 编号 标题` + `- **字段**：值` ----
    for section, kind in (("允许项", "permits"), ("规则", "rules")):
        entry: dict | None = None
        entry_line = 0
        cases: dict[str, RuleCase] = {}
        for lineno, text in sections.get(section, []):
            mi = ITEM_RE.match(text)
            if mi:
                if entry is not None:
                    _finish_entry(rs, entry, cases, entry_line, where, kind)
                head = _strip_md(mi.group("head"))
                m2 = re.match(r"^(?P<id>[A-Z]{2,3}-(?P<num>P?\d+))\s*(?P<title>.*)$", head)
                if not m2:
                    raise SpecError(where, f"第 {lineno} 行小节标题必须形如「### TB-03 规则名」，收到 {head!r}")
                rid = m2.group("id")
                if m2.group("num").upper().startswith("P") and kind != "permits":
                    raise SpecError(where, f"第 {lineno} 行 {rid}：DM-Pxx 只能出现在「## 允许项」")
                if kind == "permits" and not m2.group("num").upper().startswith("P"):
                    raise SpecError(where, f"第 {lineno} 行 {rid}：允许项编号必须形如 DM-P01")
                entry = {"id": rid, "title": m2.group("title").strip()}
                entry_line = lineno
                cases = {}
                continue
            if entry is None:
                continue
            mf = FIELD_RE.match(text)
            if not mf:
                continue
            key = FIELD_KEYS.get(_strip_md(mf.group("key")))
            if key is None:
                continue
            val = mf.group("val").strip()
            if key == "level":
                lv = LEVEL_BY_MARK.get(_strip_md(val))
                if lv is None:
                    raise SpecError(where, f"第 {lineno} 行「级别」只接受 ❌ / ⚠️ / ✅ / ⏸（或 error/warn/info/unknown），"
                                           f"收到 {val!r}")
                entry["level"] = lv
            elif key == "applies_to":
                entry["applies_to"] = split_list(val)
            elif key == "check":
                entry["check"] = _strip_md(val)
            elif key == "when":
                entry["when"] = parse_expr(val, lineno, where)
                entry["when_line"] = lineno
            elif key == "require":
                entry["require"] = split_list(val)
            elif key == "params":
                entry.setdefault("params", {}).update(parse_params(val, lineno, where))
            elif key == "cases":
                c = parse_case(val, lineno, where)
                if c.case in cases:
                    raise SpecError(where, f"第 {lineno} 行「情形」{c.case} 重复声明")
                cases[c.case] = c
            elif key == "auto_fix":
                entry["auto_fix"] = _strip_md(val).lower() in _TRUE
            elif key == "ask":
                entry["ask"] = tuple(entry.get("ask", ())) + parse_asks(val)
            elif key == "enabled":
                # 「停用：true」= 该规则不生效（enabled=False）
                entry["enabled"] = _strip_md(val).lower() not in _TRUE
            elif key == "samples":
                # 「样例：命中 = <SQL>」/「样例：通过 = <SQL>」，一行一条，由 check_specs 自动跑
                bucket = entry.setdefault("samples", {"hit": [], "pass": []})
                raw_v = val.strip()
                if "=" not in raw_v:
                    raise SpecError(where, f"第 {lineno} 行「样例」格式应为 "
                                           f"`- **样例**：命中 = <SQL>` 或 `通过 = <SQL>`，收到 {raw_v!r}")
                kind_txt, _, sql_txt = raw_v.partition("=")
                k = SAMPLE_KINDS.get(_strip_md(kind_txt).lower())
                if k is None:
                    raise SpecError(where, f"第 {lineno} 行「样例」前缀只能是 命中 / 通过，"
                                           f"收到 {kind_txt.strip()!r}")
                sql_txt = sql_txt.strip().strip("`").strip()
                if not sql_txt:
                    raise SpecError(where, f"第 {lineno} 行「样例」的 SQL 为空")
                bucket.setdefault(k, []).append(sql_txt)
            else:                       # clause / message / fix
                entry[key] = _strip_md(val)
        if entry is not None:
            _finish_entry(rs, entry, cases, entry_line, where, kind)

    if not rs.rules:
        raise SpecError(where, "没有解析到任何规则（检查是否有 `## 规则` 与 `### XX-nn` 小节）")
    return rs


def _finish_entry(rs: RuleSet, entry: dict, cases: dict[str, RuleCase],
                  line: int, where: str, kind: str):
    """把解析出的条目规整成 Rule / Permit 并校验必填项。"""
    rid = entry["id"]
    if kind == "permits":
        leaf = entry.get("when")
        p = Permit(id=rid, title=entry.get("title", ""),
                   clause=entry.get("clause", ""),
                   applies_to=tuple(entry.get("applies_to") or ()),
                   leaf=leaf)
        rs.permits.append(p)
        return

    applies = tuple(entry.get("applies_to") or ())
    if not applies:
        raise SpecError(where, f"第 {line} 行 {rid}：缺少「适用」（写入语句范围，如 create_table / update / batch）")
    for s in applies:
        if s not in ALL_SCOPES:
            raise SpecError(where, f"第 {line} 行 {rid}：「适用」含未知取值 {s!r}，"
                                   f"可用：{', '.join(sorted(ALL_SCOPES))}")
    level = entry.get("level", "error")
    check = entry.get("check", "")
    when = entry.get("when")
    if bool(check) == bool(when):
        raise SpecError(where, f"第 {line} 行 {rid}：必须有且只有一个「检查」（DDL）或「判定」（DML）")
    rule = Rule(
        id=rid, title=entry.get("title", ""), level=level, applies_to=applies,
        check=check, when=when, require=tuple(entry.get("require") or ()),
        params=dict(entry.get("params") or {}), clause=entry.get("clause", ""),
        message=entry.get("message", ""), fix=entry.get("fix", ""),
        cases=dict(cases), auto_fix=bool(entry.get("auto_fix", False)),
        asks=tuple(entry.get("ask") or ()), enabled=bool(entry.get("enabled", True)),
        samples={k: list(v) for k, v in (entry.get("samples") or {}).items()},
        doc=rs.doc, line=line,
    )
    if rule.id in {r.id for r in rs.rules}:
        raise SpecError(where, f"第 {line} 行规则编号 {rid} 重复")
    rs.rules.append(rule)


# --------------------------------------------------------------------------
# 语义校验（事实名 / 检查名 / 占位符 / 阈值）
# --------------------------------------------------------------------------


def validate(rs: RuleSet, fact_names: set[str], check_fields: dict[str, dict],
             where: str = "") -> RuleSet:
    """校验一份 RuleSet。

    fact_names   : DML 可用事实名集合（dmfacts.FACT_NAMES_REGISTERED）
    check_fields : 检查名 → {"fields": set(), "cases": set()}（ddlchecks.CHECKS）
    """
    where = where or Path(rs.path).name
    for rule in rs.rules:
        if rule.check:
            spec = check_fields.get(rule.check)
            if spec is None:
                raise SpecError(where, f"第 {rule.line} 行 {rule.id}：未知的检查 {rule.check!r}；"
                                       f"可用检查：{', '.join(sorted(check_fields))}")
            allowed = set(spec.get("fields") or ()) | set(rule.params) | set(rs.thresholds)
            for case in rule.cases.values():
                if spec.get("cases") and case.case not in spec["cases"]:
                    raise SpecError(where, f"第 {rule.line} 行 {rule.id}：情形 {case.case!r} 不属于检查 "
                                           f"{rule.check}（可用：{', '.join(sorted(spec['cases']))}）")
                _check_placeholders(case.message, allowed, where, rule)
                _check_placeholders(case.fix, allowed, where, rule)
            _check_placeholders(rule.message, allowed, where, rule)
            _check_placeholders(rule.fix, allowed, where, rule)
            _check_placeholders(rule.clause, allowed, where, rule)
        else:                                   # DML：表达式 + 事实
            _walk_condition(rule.when, f"{where} 第 {rule.line} 行 {rule.id}",
                            rs.thresholds, fact_names)
            for fname in rule.require:
                if fname not in fact_names:
                    raise SpecError(where, f"第 {rule.line} 行 {rule.id}：「依赖」引用了未注册的事实 {fname!r}")
            allowed = set(fact_names) | set(rs.thresholds)
            _check_placeholders(rule.message, allowed, where, rule)
            _check_placeholders(rule.fix, allowed, where, rule)
            _check_placeholders(rule.clause, allowed, where, rule)
    for permit in rs.permits:
        if permit.leaf:
            _walk_condition(permit.leaf, f"{where} 允许项 {permit.id}", rs.thresholds, fact_names)
    return rs


def _check_placeholders(template: str, allowed: set[str], where: str, rule: Rule):
    for name in PLACEHOLDER_RE.findall(template or ""):
        if name not in allowed:
            raise SpecError(where, f"第 {rule.line} 行 {rule.id}：文案占位符 {{{name}}} 既不是可用字段、"
                                   f"参数名，也不是阈值名（可用：{', '.join(sorted(allowed)) or '无'}）")


def _walk_condition(cond: Any, where: str, thresholds: dict,
                    fact_names: set[str] | None = None):
    if not isinstance(cond, dict):
        raise SpecError(where, "条件必须是对象")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        items = cond[key]
        if not isinstance(items, list) or not items:
            raise SpecError(f"{where}.{key}", "必须是非空列表")
        for i, sub in enumerate(items):
            _walk_condition(sub, f"{where}.{key}[{i}]", thresholds, fact_names)
        return
    if "not" in cond:
        _walk_condition(cond["not"], f"{where}.not", thresholds, fact_names)
        return
    fact = cond.get("fact")
    if not fact:
        raise SpecError(where, "叶子条件缺少 fact")
    if fact_names is not None and str(fact) not in fact_names:
        raise SpecError(where, f"引用了未注册的事实 {fact!r}（可用事实见 dml.md 附录 B / dmfacts.py）")
    op = str(cond.get("op") or "")
    if op in ("is_null", "not_null", "is_empty", "not_empty"):
        return
    val = cond.get("value")
    if isinstance(val, str) and val.startswith("$") and val[1:] not in thresholds:
        raise SpecError(where, f"引用了不存在的阈值 {val!r}（阈值表里只有 {sorted(thresholds)}）")


# --------------------------------------------------------------------------
# 加载 + 热加载
# --------------------------------------------------------------------------


def load_doc(path: str | Path, doc: str, fact_names: set[str],
             check_fields: dict[str, dict]) -> RuleSet:
    p = Path(path)
    where = p.name or doc
    if not p.exists():
        raise SpecError(str(p), "规范文件不存在（检查 config/settings.yaml 的 standard 段）")
    try:
        md = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(str(p), f"必须存成 UTF-8 文本：{exc}") from None
    rs = parse_doc(md, str(p), doc)
    validate(rs, fact_names, check_fields, where)
    _check_permit_conflicts(rs, where)
    return rs


def _check_permit_conflicts(rs: RuleSet, where: str):
    """允许项 vs error 规则：条件完全相同 → 配置自相矛盾，拒绝加载。"""
    for permit in rs.permits:
        if not permit.leaf:
            continue
        for rule in rs.rules:
            if not rule.enabled or rule.level != "error" or not rule.when:
                continue
            if not (set(rule.applies_to) & set(permit.applies_to)):
                continue
            if _same_leaf(rule.when, permit.leaf):
                raise SpecError(where, f"规则 {rule.id} 的条件与允许项 {permit.id}（{permit.title}）"
                                       "直接冲突；要么停用该规则，要么调整允许项")


def _same_leaf(a: dict, b: dict) -> bool:
    return (str(a.get("fact")) == str(b.get("fact"))
            and str(a.get("op")) == str(b.get("op"))
            and a.get("value") == b.get("value"))


_CACHE: dict[tuple[str, str], tuple[tuple, RuleSet, str]] = {}


def _stamp(p: Path) -> tuple:
    try:
        st = p.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


def get_ruleset(path: str | Path, doc: str, fact_names: set[str],
                check_fields: dict[str, dict], force: bool = False) -> tuple[RuleSet | None, str]:
    """按 mtime + size 热加载。文档写坏时**不切换**，继续用上一版；没有上一版返回 (None, 原因)。"""
    p = Path(path)
    key = (str(p), doc)
    stamp = _stamp(p)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp and not force:
        return hit[1], hit[2]
    try:
        rs = load_doc(p, doc, fact_names, check_fields)
    except SpecError as exc:
        if hit:
            warn = f"{p.name} 校验失败，已继续使用上一版规则（{hit[1].rules_version}）：{exc}"
            _CACHE[key] = (hit[0], hit[1], warn)
            return hit[1], warn
        return None, f"{p.name} 校验失败且无可用的上一版规则：{exc}"
    except Exception as exc:  # noqa: BLE001
        if hit:
            warn = f"{p.name} 读取失败，已继续使用上一版规则：{type(exc).__name__}: {exc}"
            _CACHE[key] = (hit[0], hit[1], warn)
            return hit[1], warn
        return None, f"{p.name} 读取失败：{type(exc).__name__}: {exc}"
    _CACHE[key] = (stamp, rs, "")
    return rs, ""


# --------------------------------------------------------------------------
# 模板渲染
# --------------------------------------------------------------------------


def render(template: str, fields: dict | None = None, thresholds: dict | None = None) -> str:
    """把 ``{字段名}`` / ``{阈值名}`` 替换成实际值。"""
    if not template:
        return ""
    fields = fields or {}
    thresholds = thresholds or {}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in fields:
            return _render_value(fields[name])
        if name in thresholds:
            return str(thresholds[name])
        return m.group(0)

    return PLACEHOLDER_RE.sub(sub, template)


def _render_value(value: Any) -> str:
    if value is None:
        return "（未知）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
        return "、".join(items) if items else "（无）"
    if isinstance(value, dict):
        return "、".join(f"{k}={v}" for k, v in value.items()) or "（无）"
    return str(value)


def summary(rs: RuleSet | None, warning: str = "") -> dict:
    """给返回体/healthz 用的规则集摘要。"""
    if rs is None:
        return {"available": False, "rules_version": "unknown", "rules_count": 0,
                "enabled_count": 0, "permits_count": 0, "sha256": "", "warning": warning}
    return {
        "available": True,
        "source": "/".join(Path(rs.path).parts[-2:]) if rs.path else "",
        "rules_version": rs.rules_version,
        "rules_count": len(rs.rules),
        "enabled_count": len(rs.enabled_rules()),
        "permits_count": len(rs.permits),
        "thresholds": dict(rs.thresholds),
        "sha256": rs.sha256,
        "warning": warning,
    }
