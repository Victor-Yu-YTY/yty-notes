"""SQL 词法与基础切分工具（纯标准库，无第三方依赖）。

只服务 DDL 审核场景：建表、变更表、索引、破坏性操作。
"""

from __future__ import annotations

from dataclasses import dataclass


class ParseError(ValueError):
    """SQL 无法解析（返回 422 / INVALID_SQL）。"""


@dataclass
class Token:
    kind: str          # word / ident / string / number / punct
    value: str
    raw: str

    def is_kw(self, *words: str) -> bool:
        return self.kind == "word" and self.value.upper() in {w.upper() for w in words}

    def is_punct(self, *chars: str) -> bool:
        return self.kind == "punct" and self.value in chars


def tokenize(sql: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == "\\" and quote != "`" and j + 1 < n:
                    buf.append(sql[j + 1])
                    j += 2
                    continue
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        buf.append(quote)
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            if j >= n:
                raise ParseError(f"未闭合的引号 {quote}（位置 {i}）")
            kind = "string" if quote == "'" else "ident"
            out.append(Token(kind, "".join(buf), sql[i:j + 1]))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == "."):
                j += 1
            out.append(Token("number", sql[i:j], sql[i:j]))
            i = j
            continue
        if ch.isalnum() or ch in "_$":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            out.append(Token("word", sql[i:j], sql[i:j]))
            i = j
            continue
        out.append(Token("punct", ch, ch))
        i += 1
    return out


def split_statements(sql: str) -> list[str]:
    """按顶层分号拆多条语句（忽略字符串/注释里的分号）。"""
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote = None
    depth = 0
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                # 处理 '' 或 `` 转义
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            seg = sql[i:] if j < 0 else sql[i:j]
            buf.append(seg)
            i = n if j < 0 else j
            continue
        if ch == "#":
            j = sql.find("\n", i)
            seg = sql[i:] if j < 0 else sql[i:j]
            buf.append(seg)
            i = n if j < 0 else j
            continue
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            end = n if j < 0 else j + 2
            buf.append(sql[i:end])
            i = end
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def find_matching(tokens: list[Token], start: int) -> int:
    """tokens[start] == '(' → 返回配对的 ')' 下标。"""
    depth = 0
    for i in range(start, len(tokens)):
        t = tokens[i]
        if t.is_punct("("):
            depth += 1
        elif t.is_punct(")"):
            depth -= 1
            if depth == 0:
                return i
    raise ParseError("括号不配对")


def split_top(tokens: list[Token], sep: str = ",") -> list[list[Token]]:
    parts, cur, depth = [], [], 0
    for t in tokens:
        if t.is_punct("("):
            depth += 1
        elif t.is_punct(")"):
            depth -= 1
        if depth == 0 and t.is_punct(sep):
            parts.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def join_raw(tokens: list[Token]) -> str:
    out = []
    for t in tokens:
        if t.kind == "ident":
            out.append(f"`{t.value}`")
        elif t.kind == "string":
            out.append("'" + t.value.replace("'", "''") + "'")
        else:
            out.append(t.raw)
    return " ".join(out)


def first_word(sql: str) -> str:
    """取语句第一个关键字（用于识别类型）。"""
    for t in tokenize(sql):
        if t.kind == "word":
            return t.value.upper()
        if t.kind == "punct":
            return t.value
    return ""
