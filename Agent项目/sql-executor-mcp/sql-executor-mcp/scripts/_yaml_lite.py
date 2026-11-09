"""极简 YAML 子集解析（仅在没装 PyYAML 的本机环境兜底用）。

**为什么有这个文件**：`scripts/selfcheck.py` 要读 `tests/cases/basic.yaml`。
正式环境按 `requirements.txt` 装 PyYAML，走 `yaml.safe_load`；
但本机联调/交付前自检的机器可能没有 PyYAML，为了「离线自检零依赖」才提供这份兜底实现。

它只实现用例文件用到的那一小撮语法，不要拿它当通用 YAML 用：

* 注释（`#` 开头整行）、空行
* 块映射（按缩进）、块列表（`- ` 开头）
* 字面量块标量（`key: |`）
* 行内流式集合（`{a: 1, b: [x, y]}`、`[a, b]`，支持嵌套）
* 标量：单/双引号字符串、true/false、null/~、整数、浮点、裸字符串

**不支持的**：锚点/别名、多文档、`>` 折叠标量、复杂键、`!tag`。
解析失败直接抛 `ValueError`，不猜。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_BLOCK_RE = re.compile(r"^([^:'\"]+):\s*\|[-+]?\s*$")


def _preprocess(text: str) -> tuple[list[tuple[int, str]], dict[str, str]]:
    """把 `key: |` 的字面量块提前抽出来，换成占位符，其余保留 (缩进, 内容)。"""
    blocks: dict[str, str] = {}
    out: list[tuple[int, str]] = []
    raw = text.splitlines()
    i, n = 0, len(raw)
    while i < n:
        line = raw[i].rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        m = _BLOCK_RE.match(content)
        if m:
            key = m.group(1).strip()
            i += 1
            buf: list[str] = []
            base = None
            while i < n:
                nxt = raw[i].rstrip("\r")
                if not nxt.strip():
                    buf.append("")
                    i += 1
                    continue
                nind = len(nxt) - len(nxt.lstrip(" "))
                if nind <= indent:
                    break
                if base is None:
                    base = nind
                buf.append(nxt[base:] if nind >= base else nxt.strip())
                i += 1
            while buf and buf[-1] == "":
                buf.pop()
            name = f"__BLOCK_{len(blocks)}__"
            blocks[name] = "\n".join(buf)
            out.append((indent, f"{key}: {name}"))
            continue
        out.append((indent, content))
        i += 1
    return out, blocks


def _split_top(text: str, sep: str) -> list[str]:
    """按顶层分隔符切分（跳过引号与 {} [] 内部）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                buf.append(text[i:i + 2])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _find_colon(text: str) -> int:
    depth = 0
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == ":" and depth == 0:
            return i
        i += 1
    return -1


def _strip_comment(text: str) -> str:
    """去掉行尾注释：`#` 前必须是空白，且不在引号内（与 YAML 行为一致）。"""
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _split_kv(text: str) -> tuple[str, str]:
    pos = _find_colon(text)
    if pos < 0:
        raise ValueError(f"不是 key: value 形式：{text!r}")
    key = text[:pos].strip()
    if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
        key = key[1:-1]
    return key, _strip_comment(text[pos + 1:]).strip()


_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"null", "~", ""}


def _scalar(text: str, blocks: dict[str, str]) -> Any:
    t = _strip_comment(text).strip()
    if t in blocks:
        return blocks[t]
    if len(t) >= 2 and t[0] == t[-1] == '"':
        return t[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if t.startswith("{") or t.startswith("["):
        return _flow(t, blocks)
    low = t.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if low in _NULL:
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t


def _flow(text: str, blocks: dict[str, str]) -> Any:
    t = text.strip()
    if t.startswith("["):
        if not t.endswith("]"):
            raise ValueError(f"流式列表未闭合：{text!r}")
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x, blocks) for x in _split_top(inner, ",")]
    if t.startswith("{"):
        if not t.endswith("}"):
            raise ValueError(f"流式映射未闭合：{text!r}")
        inner = t[1:-1].strip()
        out: dict[str, Any] = {}
        if not inner:
            return out
        for part in _split_top(inner, ","):
            if not part.strip():
                continue
            k, v = _split_kv(part)
            out[k] = _scalar(v, blocks)
        return out
    raise ValueError(f"不是流式集合：{text!r}")


def _parse_block(items: list[tuple[int, str]], i: int, indent: int,
                 blocks: dict[str, str]) -> tuple[Any, int]:
    if i < len(items) and items[i][1].startswith("- "):
        return _parse_list(items, i, indent, blocks)
    return _parse_map(items, i, indent, blocks)


def _parse_map(items: list[tuple[int, str]], i: int, indent: int,
               blocks: dict[str, str]) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while i < len(items) and items[i][0] == indent and not items[i][1].startswith("- "):
        key, rest = _split_kv(items[i][1])
        if rest == "":
            i += 1
            if i < len(items) and items[i][0] > indent:
                out[key], i = _parse_block(items, i, items[i][0], blocks)
            else:
                out[key] = None
        else:
            out[key] = _scalar(rest, blocks)
            i += 1
    return out, i


def _parse_list(items: list[tuple[int, str]], i: int, indent: int,
                blocks: dict[str, str]) -> tuple[list, int]:
    out: list[Any] = []
    while i < len(items) and items[i][0] == indent and items[i][1].startswith("- "):
        rest = items[i][1][2:].strip()
        item_indent = indent + 2

        if rest == "":
            i += 1
            if i < len(items) and items[i][0] > indent:
                val, i = _parse_block(items, i, items[i][0], blocks)
            else:
                val = None
            out.append(val)
            continue

        starts_flow = rest.startswith(("{", "["))
        if starts_flow:
            out.append(_scalar(rest, blocks))
            i += 1
            continue

        if _find_colon(rest) < 0:
            out.append(_scalar(rest, blocks))
            i += 1
            continue

        # 列表项本身是一个映射：第一个键在 "- " 里，后续键与它同缩进的块内
        key, val_text = _split_kv(rest)
        item: dict[str, Any] = {}
        if val_text == "":
            i += 1
            if i < len(items) and items[i][0] > item_indent:
                item[key], i = _parse_block(items, i, items[i][0], blocks)
            else:
                item[key] = None
        else:
            item[key] = _scalar(val_text, blocks)
            i += 1

        while i < len(items) and items[i][0] == item_indent \
                and not items[i][1].startswith("- "):
            k2, v2 = _split_kv(items[i][1])
            if v2 == "":
                i += 1
                if i < len(items) and items[i][0] > item_indent:
                    item[k2], i = _parse_block(items, i, items[i][0], blocks)
                else:
                    item[k2] = None
            else:
                item[k2] = _scalar(v2, blocks)
                i += 1
        out.append(item)
    return out, i


def loads(text: str) -> Any:
    """解析 YAML 子集文本。"""
    items, blocks = _preprocess(text)
    if not items:
        return None
    value, i = _parse_block(items, 0, items[0][0], blocks)
    if i != len(items):
        raise ValueError(f"解析未消费完，剩余 {len(items) - i} 行，从 {items[i]!r} 开始")
    return value


def load_file(path: str | Path) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"))
