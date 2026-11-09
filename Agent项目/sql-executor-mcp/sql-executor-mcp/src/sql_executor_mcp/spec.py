"""标准文件解析：把 `standards/executor.md` 编译成规则集（Policy）。

设计口径与 sql-review-mcp 一致：**规则只写在 md 里，代码只负责解析与求值**。
改规则 = 改 md，不改代码、不重启（按 mtime 热加载）。

md 结构（详见 `standards/README.md`）：

    **规则版本**：1            ← 顶部元信息，`**键**：值` 形式
    ## 阈值                    ← 名称 | 值 | 说明
    ## 环境                    ← 环境 | 开放执行 | 允许类型 | 执行窗口
    ### EX-G01 标题            ← 一条规则
    - **级别**：block
    - **适用**：all
    - **判定**：stmt_kind not in $allowed_types
    - **说明**：...{占位符}...

判定表达式编译成引擎现有的条件树（`{"fact":..,"op":..,"value":..}` / `{"all":..}` /
`{"any":..}` / `{"not":..}`），因此 `engine.eval_cond` 不需要为 md 改一行。

任何写错的地方在**加载期**就报错（`PolicyError`），不允许带病启动：
id 不合规 / 重复、级别非法、判定表达式解析失败、引用了未注册的事实或变量、
说明里的占位符未注册、环境表与阈值表取值非法。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .engine import LEVEL_BLOCK, LEVEL_WARN, Check, Policy, PolicyError, VALID_TYPES
from .facts import FACT_SPECS

ID_RE = re.compile(r"^EX-[GTAINU]\d{2}$")

# 顶部元信息：**键**：值（规则字段行以 "- " 开头，不会命中这里）
_META_RE = re.compile(r"^\*\*(?P<key>[^*]+?)\*\*\s*[：:]\s*(?P<val>.*)$")
# 规则字段：- **键**：值
_FIELD_RE = re.compile(r"^-\s*\*\*(?P<key>[^*]+?)\*\*\s*[：:]\s*(?P<val>.*)$")
_RULE_HEAD_RE = re.compile(r"^###\s+(?P<id>EX-[A-Z]\d{2})\s*(?P<title>.*)$")
_H2_RE = re.compile(r"^##\s+(?P<title>.*)$")

_LEVEL_ALIAS = {
    "block": LEVEL_BLOCK, "❌": LEVEL_BLOCK, "blocking": LEVEL_BLOCK,
    "warn": LEVEL_WARN, "⚠️": LEVEL_WARN, "warning": LEVEL_WARN,
}

_REQUIRED_FIELDS = ("级别", "适用", "判定", "说明")


# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<num>-?\d+(?:\.\d+)?)"
    r"|(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<str>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op><>|!=|==|<=|>=|=|<|>)"
    r"|(?P<lp>\()"
    r"|(?P<rp>\))"
    r"|(?P<comma>,)"
    r")"
)

_CMP_OPS = {"=": "eq", "==": "eq", "!=": "ne", "<>": "ne",
            ">": "gt", ">=": "ge", "<": "lt", "<=": "le"}

_LIST_OPS = {"in": "in", "intersects": "intersects", "set": "eq_set"}

_IS_OPS = {("null", False): ("is_null", None), ("null", True): ("not_null", None),
           ("empty", False): ("is_empty", None), ("empty", True): ("not_empty", None),
           ("true", False): ("eq", True), ("true", True): ("ne", True),
           ("false", False): ("eq", False), ("false", True): ("ne", False)}


class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str):
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"{self.kind}:{self.value}"


def _lex(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos = 0
    n = len(text)
    while pos < n:
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise PolicyError(f"判定表达式无法识别：{text!r}（位置 {pos}：{text[pos:pos + 12]!r}）")
        kind = m.lastgroup or ""
        toks.append(_Tok(kind, m.group()))
        pos = m.end()
    return toks


# ---------------------------------------------------------------- 表达式解析

class _ExprParser:
    """递归下降：or < and < not < primary。

    产出的节点直接复用 engine.eval_cond 认识的四种形态。
    """

    def __init__(self, text: str):
        self.text = text
        self.toks = _lex(text)
        self.i = 0

    # -- 基础
    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> _Tok:
        tok = self._peek()
        if tok is None:
            raise PolicyError(f"判定表达式意外结束：{self.text!r}")
        self.i += 1
        return tok

    def _is_kw(self, word: str) -> bool:
        tok = self._peek()
        return tok is not None and tok.kind == "id" and tok.value.lower() == word

    def _kw(self, word: str) -> bool:
        if self._is_kw(word):
            self.i += 1
            return True
        return False

    def _expect_kw(self, word: str) -> None:
        if not self._kw(word):
            raise PolicyError(f"判定表达式缺少关键字 {word}：{self.text!r}")

    def _is_op(self, value: str) -> bool:
        tok = self._peek()
        return tok is not None and tok.kind == "op" and tok.value == value

    def _end(self) -> bool:
        return self.i >= len(self.toks)

    # -- 文法
    def parse(self) -> dict:
        node = self._or()
        if not self._end():
            tok = self._peek()
            raise PolicyError(f"判定表达式有多余内容：{self.text!r}（{tok!r} 处）")
        return node

    def _or(self) -> dict:
        items = [self._and()]
        while self._kw("or"):
            items.append(self._and())
        return items[0] if len(items) == 1 else {"any": items}

    def _and(self) -> dict:
        items = [self._not()]
        while self._kw("and"):
            items.append(self._not())
        return items[0] if len(items) == 1 else {"all": items}

    def _not(self) -> dict:
        if self._kw("not"):
            return {"not": self._not()}
        return self._primary()

    def _primary(self) -> dict:
        tok = self._peek()
        if tok is not None and tok.kind == "lp":
            self.i += 1
            node = self._or()
            nxt = self._next()
            if nxt.kind != "rp":
                raise PolicyError(f"判定表达式括号不匹配：{self.text!r}")
            return node
        return self._leaf()

    def _leaf(self) -> dict:
        name = self._operand()

        if self._is_kw("is"):
            self.i += 1
            neg = self._kw("not")
            key = self._peek()
            if key is None or key.kind != "id" or key.value.lower() not in ("null", "empty", "true", "false"):
                raise PolicyError(f"判定表达式 is 之后只能是 null/empty/true/false：{self.text!r}")
            self.i += 1
            op, value = _IS_OPS[(key.value.lower(), neg)]
            return {"fact": name, "op": op, "value": value}

        if self._kw("not"):
            self._expect_kw("in")
            return {"fact": name, "op": "not_in", "value": self._list()}

        tok = self._peek()
        if tok is not None and tok.kind == "id" and tok.value.lower() in _LIST_OPS:
            self.i += 1
            return {"fact": name, "op": _LIST_OPS[tok.value.lower()], "value": self._list()}

        tok = self._peek()
        if tok is None or tok.kind != "op" or tok.value not in _CMP_OPS:
            raise PolicyError(
                f"判定表达式缺少比较符：{self.text!r}（在 {name} 之后）")
        self.i += 1
        return {"fact": name, "op": _CMP_OPS[tok.value], "value": self._scalar()}

    def _operand(self) -> str:
        tok = self._next()
        if tok.kind == "var":
            return tok.value          # "$allowed_types" —— 保留 $，由 engine.resolve_var 解析
        if tok.kind == "id":
            name = tok.value
            if name.lower() in ("and", "or", "not", "in", "is"):
                raise PolicyError(f"判定表达式缺少左值：{self.text!r}")
            return name.lower()
        raise PolicyError(f"判定表达式左值必须是事实名或 $变量：{self.text!r}")

    def _list(self):
        """集合值：`(a, b)` 或单个值（如 `not in $allowed_types`）。

        单个值原样返回（不包成列表），这样 `$变量` 仍能被 engine.resolve_var 解析，
        而 `_cmp` / `_as_list` 对标量本来就按单元素处理。
        """
        tok = self._peek()
        if tok is None:
            raise PolicyError(f"判定表达式缺少集合值：{self.text!r}")
        if tok.kind != "lp":
            return self._scalar()
        self.i += 1
        items: list[Any] = []
        if self._peek() is not None and self._peek().kind == "rp":
            self.i += 1
            return items
        items.append(self._scalar())
        while True:
            tok = self._peek()
            if tok is not None and tok.kind == "comma":
                self.i += 1
                items.append(self._scalar())
                continue
            nxt = self._next()
            if nxt.kind != "rp":
                raise PolicyError(f"判定表达式的集合值括号不匹配：{self.text!r}")
            return items

    def _scalar(self) -> Any:
        tok = self._next()
        if tok.kind == "num":
            return float(tok.value) if "." in tok.value else int(tok.value)
        if tok.kind == "str":
            return tok.value[1:-1]
        if tok.kind == "var":
            return tok.value
        if tok.kind == "id":
            low = tok.value.lower()
            if low == "true":
                return True
            if low == "false":
                return False
            if low == "null":
                return None
            return tok.value
        raise PolicyError(f"判定表达式的取值无法识别：{tok.value!r}（{self.text!r}）")


def parse_expression(text: str) -> dict:
    if not (text or "").strip():
        raise PolicyError("判定表达式为空")
    return _ExprParser(text.strip()).parse()


# ---------------------------------------------------------------- 结构遍历

def _iter_facts(node: Any):
    if isinstance(node, dict):
        if "fact" in node:
            yield str(node["fact"])
        for v in node.values():
            yield from _iter_facts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_facts(v)


def _iter_vars(node: Any):
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "value":
                if isinstance(val, str) and val.startswith("$"):
                    yield val[1:]
            else:
                yield from _iter_vars(val)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_vars(v)


def _iter_placeholders(text: str):
    for name in re.findall(r"\{(\w+)\}", text or ""):
        yield name


# ---------------------------------------------------------------- 表格

def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells)


def _read_table(lines: list[str], i: int) -> tuple[list[str], list[list[str]], int]:
    rows: list[list[str]] = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        rows.append(_split_row(lines[i]))
        i += 1
    if not rows:
        return [], [], i
    header = rows[0]
    data = [r for r in rows[1:] if not _is_separator(r)]
    return header, data, i


def _coerce(raw: str) -> Any:
    text = (raw or "").strip()
    if text in ("", "-", "—", "null", "NULL", "None", "none", "~"):
        return None
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _col_index(header: list[str], *names: str) -> int:
    for name in names:
        for idx, cell in enumerate(header):
            if cell.strip() == name:
                return idx
    raise PolicyError(f"表头缺少列 {names}：实际为 {header}")


def _cell(cells: list[str], idx: int) -> str:
    return cells[idx] if idx < len(cells) else ""


# ---------------------------------------------------------------- 解析主流程

def _parse_meta(lines: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in lines:
        if line.startswith("### ") or line.startswith("- "):
            continue
        m = _META_RE.match(line.strip())
        if m:
            meta[m.group("key").strip()] = m.group("val").strip()
    return meta


def _parse_thresholds(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    i_name = _col_index(header, "名称", "阈值", "名称 ")
    i_val = _col_index(header, "值", "取值")
    out: dict[str, Any] = {}
    for cells in rows:
        name = _cell(cells, i_name).strip()
        if not name:
            continue
        value = _coerce(_cell(cells, i_val))
        if value is None:
            raise PolicyError(f"阈值 {name} 必须有值")
        out[name] = value
    return out


def _parse_environments(header: list[str], rows: list[list[str]]) -> dict[str, dict]:
    i_env = _col_index(header, "环境")
    i_enabled = _col_index(header, "开放执行", "是否开放", "启用")
    i_types = _col_index(header, "允许类型", "开放类型")
    i_window = _col_index(header, "执行窗口", "窗口")

    out: dict[str, dict] = {}
    for cells in rows:
        env = _cell(cells, i_env).strip()
        if not env:
            continue
        enabled = _coerce(_cell(cells, i_enabled))
        raw_types = _cell(cells, i_types)
        types = [t.strip() for t in raw_types.split(",") if t.strip()
                 and t.strip() not in ("-", "—")]
        window = _coerce(_cell(cells, i_window))
        out[env] = {
            "enabled": bool(enabled),
            "allowed_types": types,
            "window": window if isinstance(window, str) else None,
        }
    return out


def _parse_rules(lines: list[str]) -> list[dict]:
    rules: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _RULE_HEAD_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        rule_id = m.group("id")
        title = m.group("title").strip()
        fields: dict[str, str] = {}
        i += 1
        while i < n:
            line = lines[i]
            if line.startswith("### ") or line.startswith("## "):
                break
            fm = _FIELD_RE.match(line.strip())
            if fm:
                fields[fm.group("key").strip()] = fm.group("val").strip()
            i += 1
        rules.append({"id": rule_id, "title": title or rule_id, "fields": fields})
    return rules


def _build_check(raw: dict, thresholds: dict, environments: dict) -> Check:
    cid = raw["id"]
    fields = raw["fields"]

    for key in _REQUIRED_FIELDS:
        if key not in fields or not fields[key].strip():
            raise PolicyError(f"{cid} 缺少必需字段「{key}」")

    level_raw = fields["级别"].strip()
    if level_raw not in _LEVEL_ALIAS:
        raise PolicyError(f"{cid}.级别 只能是 block / warn（也接受 ❌ / ⚠️），当前 {level_raw!r}")
    level = _LEVEL_ALIAS[level_raw]

    applies_raw = fields["适用"].strip()
    if applies_raw.lower() in ("all", "全部", "*"):
        applies = ["all"]
    else:
        applies = [t.strip() for t in applies_raw.split(",") if t.strip()]
    for t in applies:
        if t != "all" and t not in VALID_TYPES:
            raise PolicyError(f"{cid}.适用 含未知类型 {t!r}（可选：{sorted(VALID_TYPES)}）")

    when = parse_expression(fields["判定"])

    for fact in _iter_facts(when):
        if fact not in FACT_SPECS:
            raise PolicyError(f"{cid}.判定 引用了未注册的事实 {fact!r}")
    for var in _iter_vars(when):
        if var not in thresholds and var not in FACT_SPECS and var not in environments:
            raise PolicyError(f"{cid}.判定 引用了无法解析的变量 ${var}")
    for name in _iter_placeholders(fields["说明"]):
        if name not in FACT_SPECS and name not in thresholds:
            raise PolicyError(f"{cid}.说明 的占位符 {{{name}}} 未注册")

    on_missing = fields.get("缺失") or fields.get("on_missing")
    if on_missing:
        on_missing = on_missing.strip().lower()
        if on_missing in _LEVEL_ALIAS:
            on_missing = _LEVEL_ALIAS[on_missing]
        if on_missing not in (LEVEL_BLOCK, LEVEL_WARN, "skip"):
            raise PolicyError(f"{cid}.缺失 只能是 block / warn / skip，当前 {on_missing!r}")

    enabled_raw = (fields.get("停用") or "").strip().lower()
    enabled = enabled_raw not in ("true", "yes", "1", "是")

    return Check(
        id=cid,
        level=level,
        title=raw["title"],
        when=when,
        message=fields["说明"].strip(),
        reason=(fields.get("原因") or fields.get("整改") or "").strip(),
        applies_to=applies,
        on_missing=on_missing,
        enabled=enabled,
    )


def load_standard(path: str | Path) -> Policy:
    """读 md、校验、编译成 Policy。任何一处写错都直接抛 PolicyError。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyError(f"标准文件不存在：{p}") from exc
    if text.startswith("\ufeff"):
        text = text[1:]

    lines = text.splitlines()
    meta = _parse_meta(lines)

    engine = meta.get("引擎")
    if engine != "exec-v1":
        raise PolicyError(f"引擎 必须是 exec-v1，当前为 {engine!r}")

    mode = (meta.get("模式") or "live").strip()
    if mode not in ("live", "shadow"):
        raise PolicyError(f"模式 只能是 live / shadow，当前为 {mode!r}")

    try:
        version = int(meta.get("规则版本") or 1)
    except ValueError as exc:
        raise PolicyError(f"规则版本 必须是整数，当前为 {meta.get('规则版本')!r}") from exc

    thresholds: dict[str, Any] = {}
    environments: dict[str, dict] = {}

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        h2 = _H2_RE.match(line.strip())
        if h2:
            title = h2.group("title").strip()
            if title in ("阈值", "环境"):
                j = i + 1
                while j < n and not lines[j].lstrip().startswith("|"):
                    if lines[j].startswith("## ") or lines[j].startswith("### "):
                        break
                    j += 1
                header, rows, j2 = _read_table(lines, j)
                if not header:
                    raise PolicyError(f"「{title}」小节必须紧跟一张 markdown 表格")
                if title == "阈值":
                    thresholds = _parse_thresholds(header, rows)
                else:
                    environments = _parse_environments(header, rows)
                i = j2
                continue
        i += 1

    if not thresholds:
        raise PolicyError("缺少「阈值」表")
    if not environments:
        raise PolicyError("缺少「环境」表")

    for env, cfg in environments.items():
        for t in cfg.get("allowed_types") or []:
            if t not in VALID_TYPES:
                raise PolicyError(f"环境 {env} 的允许类型含未知类型 {t!r}")
        if cfg.get("enabled") and not cfg.get("allowed_types"):
            raise PolicyError(f"环境 {env} 已开放执行但没配允许类型（空 = 什么都不放，容易误配）")

    raw_rules = _parse_rules(lines)
    if not raw_rules:
        raise PolicyError("一条规则都没解析到：规则必须以「### EX-xx 标题」开头")

    checks: list[Check] = []
    seen: set[str] = set()
    for raw in raw_rules:
        cid = raw["id"]
        if not ID_RE.match(cid):
            raise PolicyError(f"规则 id 不合规：{cid!r}（应形如 EX-G01）")
        if cid in seen:
            raise PolicyError(f"规则 id 重复：{cid}")
        seen.add(cid)
        checks.append(_build_check(raw, thresholds, environments))

    if not checks:
        raise PolicyError("checks 为空，服务无法判定任何语句")

    return Policy(
        version=version,
        engine=engine,
        mode=mode,
        environments=environments,
        scope={},
        thresholds=thresholds,
        checks=checks,
        path=str(p),
        raw={"meta": meta, "rules": len(checks)},
        standard_version=str(meta.get("标准版本") or "").strip(),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
