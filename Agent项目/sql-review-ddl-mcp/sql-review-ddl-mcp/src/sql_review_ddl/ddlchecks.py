"""DDL 检查原语：只做「判定 + 必要时改写 + 填数据字段」。

**本文件不含任何规则条款**：级别、说明、整改、阈值、参数、情形文案、停用开关，
全部来自 ``standards/ddl.md``（见 spec.py）。本文件只在下列位置需要改代码：

* 新增一种**判定能力**（现在读不出来的事实）→ 新增一个检查函数并登记到 CHECKS；
* 新增一种**自动改写动作**（把 SQL 改写成什么样）→ 在对应检查里实现。

参数化的"同类规则"（新增禁用类型、新增必备字段、调整阈值、换文案、停用）
只需要改 ddl.md，不用碰本文件。
"""

from __future__ import annotations

import re
from typing import Callable

from .common import (ERROR, INFO, NAME_RE, RESERVED_WORDS, TableFact, UNKNOWN, WARN,
                     arg_int, norm_default)
from .model import Column, Index, IndexCol, Table

# --------------------------------------------------------------------------
# 词表
# --------------------------------------------------------------------------

BIG_TEXT = {"TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", "BLOB", "TINYBLOB",
            "MEDIUMBLOB", "LONGBLOB", "VARBINARY", "JSON", "GEOMETRY"}
TEXT_LIKE = {"TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT"}
BLOB_LIKE = {"TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"}
TIME_TYPES = {"DATE", "TIME", "DATETIME", "TIMESTAMP", "YEAR"}
INT_TYPES = {"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT"}

_SUSPECT_TYPE = re.compile(r"(^|_)(TMP|TEMP|BAK|BACKUP|HIST|HISTORY|ARCHIVE|ARCHIVER)(_|$)")
_TMP_BAK = re.compile(r"_(TMP|BAK)_(\d{8})_(\d+)$")
_LOCK_RE = re.compile(r"(ALGORITHM|LOCK)\s*=", re.I)

# 建表/变更必备字段的兜底样例（文档里给了 `**参数**：columns = ...` 时以文档为准）
DEFAULT_REQUIRED_COLUMNS = [
    {"name": "ID", "type": "BIGINT", "args": [], "not_null": True, "auto_increment": True,
     "default": None, "comment": "无意义主键ID", "on_update": False},
    {"name": "IS_DEL", "type": "TINYINT", "args": [], "not_null": True, "auto_increment": False,
     "default": "0", "comment": "是否删除", "on_update": False},
    {"name": "CREATE_TIME", "type": "DATETIME", "args": [], "not_null": True, "auto_increment": False,
     "default": "CURRENT_TIMESTAMP", "comment": "创建时间", "on_update": False},
    {"name": "MODIFY_TIME", "type": "DATETIME", "args": [], "not_null": True, "auto_increment": False,
     "default": "CURRENT_TIMESTAMP", "comment": "修改时间", "on_update": True},
    {"name": "__MODIFY_TIME__", "type": "DATETIME", "args": [], "not_null": True,
     "auto_increment": False, "default": "CURRENT_TIMESTAMP", "comment": "BI", "on_update": True},
]
DEFAULT_REQUIRED_INDEXES = [
    {"kind": "PRIMARY", "name": "", "columns": ["ID"]},
    {"kind": "INDEX", "name": "IDX_CREATE_TIME", "columns": ["CREATE_TIME"]},
    {"kind": "INDEX", "name": "IDX__MODIFY_TIME__", "columns": ["__MODIFY_TIME__"]},
]

# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _ctx(res) -> dict:
    return res.extra.get("ctx") or {}


def _th(res, key: str, default: int = 0) -> int:
    try:
        return int((res.thresholds or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _table(res) -> Table | None:
    return _ctx(res).get("table")


def _alter(res):
    return _ctx(res).get("alter")


def _fact(res) -> TableFact | None:
    return _ctx(res).get("fact")


def _indexes(res) -> list:
    return _ctx(res).get("indexes") or []


def _coltypes(res) -> dict | None:
    return _ctx(res).get("coltypes")


def _squash(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    return re.sub(r"_+", "_", s).strip("_")


def _want(type_sql: str) -> str:
    return re.sub(r"\s+", "", (type_sql or "").upper())


def _parse_type_spec(spec) -> tuple[str, list]:
    """``BIGINT`` / ``VARCHAR(64)`` / ``DECIMAL(12,2)`` → (TYPE, [args])。"""
    text = str(spec or "").strip()
    m = re.match(r"([A-Za-z]+)\s*(?:\(([^)]*)\))?", text)
    if not m:
        return text.upper(), []
    args = [a.strip() for a in m.group(2).split(",")] if m.group(2) else []
    return m.group(1).upper(), args


def _icol(name: str, prefix: str | None = None) -> IndexCol:
    return IndexCol(name=name, prefix_len=prefix)


def _required_columns(rule) -> list[dict]:
    """``columns = ID BIGINT | IS_DEL TINYINT | ...`` + ``on_update = MODIFY_TIME`` → 字段样例。"""
    raw = rule.p_list("columns")
    if not raw:
        return list(DEFAULT_REQUIRED_COLUMNS)
    on_update = {str(x).upper() for x in rule.p_list("on_update")}
    out = []
    for item in raw:
        parts = str(item).split()
        if not parts:
            continue
        name = parts[0].upper()
        typ, args = _parse_type_spec(parts[1]) if len(parts) > 1 else (name, [])
        out.append({
            "name": name, "type": typ, "args": args, "not_null": True,
            "auto_increment": (name == "ID" and typ == "BIGINT"),
            "default": None if name == "ID" else (
                "0" if typ == "TINYINT" else "CURRENT_TIMESTAMP"),
            "comment": None,
            "on_update": (name in on_update),
        })
    return out or list(DEFAULT_REQUIRED_COLUMNS)


def _required_indexes(rule) -> list[dict]:
    """``indexes = PRIMARY ID | IDX_CREATE_TIME CREATE_TIME | ...`` → 索引样例清单。"""
    raw = rule.p_list("indexes")
    if not raw:
        return list(DEFAULT_REQUIRED_INDEXES)
    out = []
    for item in raw:
        parts = str(item).split()
        if not parts:
            continue
        head = parts[0].upper()
        cols = _split_cols(" ".join(parts[1:])) if len(parts) > 1 else ["ID"]
        if head == "PRIMARY":
            out.append({"kind": "PRIMARY", "name": "", "columns": cols})
        elif head in ("UNIQUE", "UNIQ"):
            nm = parts[1] if len(parts) > 2 else ""
            out.append({"kind": "UNIQUE", "name": nm,
                        "columns": _split_cols(" ".join(parts[2:])) or cols})
        else:
            out.append({"kind": "INDEX", "name": parts[0], "columns": cols})
    return out or list(DEFAULT_REQUIRED_INDEXES)


def _split_cols(text: str) -> list[str]:
    return [c.strip().strip("()") for c in re.split(r"[,，]", str(text)) if c.strip()]


# ==========================================================================
# TB 建表
# ==========================================================================


def check_forbid_ctas(res, rule) -> None:
    tbl = _table(res)
    if tbl is None or not getattr(tbl, "create_select", False):
        return
    res.emit(rule, "语句")
    res.ask("Q-CTAS", rule.id, fallback="该语句是 CTAS，请改为「先建表 + 再迁移数据」两步", blocking=True)
    res.fatal = f"{rule.id}：CREATE TABLE ... SELECT 不允许自动改写"


def check_required_columns(res, rule) -> None:
    tbl = _table(res)
    if tbl is None or getattr(tbl, "create_select", False):
        return
    specs = _required_columns(rule)
    existing = {c.name.upper(): c for c in tbl.columns}
    fix = rule.auto_fix
    for spec in specs:
        name = spec["name"].upper()
        if name in existing:
            continue
        col = Column(name=name, type=spec["type"].upper(), args=list(spec.get("args") or []),
                     nullable=bool(spec.get("not_null") is False),
                     default=spec.get("default"),
                     auto_increment=bool(spec.get("auto_increment")),
                     comment=spec.get("comment"))
        if spec.get("on_update"):
            col.on_update = "CURRENT_TIMESTAMP"
        if fix:
            if name == "ID":
                tbl.columns.insert(0, col)
            else:
                tbl.columns.append(col)
            existing[name] = col
        res.emit(rule, f"字段 `{name}`", {"name": name, "type": spec["type"]},
                 case="missing", auto_fixed=fix and bool(rule.auto_fix))

    for spec in specs:
        name = spec["name"].upper()
        col = existing.get(name)
        if col is None:
            continue
        changed = []
        want_type = spec["type"].upper()
        want_args = [str(a) for a in (spec.get("args") or [])]
        if col.type.upper() != want_type:
            changed.append(f"类型 {col.type_sql()} → {spec['type']}")
            if fix:
                col.type, col.args = want_type, want_args
        elif want_args and len(col.args) != len(want_args):
            changed.append(f"参数 {col.type_sql()} → {spec['type']}")
            if fix:
                col.args = want_args
        if spec.get("not_null") and col.nullable:
            changed.append("改为 NOT NULL")
            if fix:
                col.nullable = False
        if name == "ID":
            if spec.get("auto_increment") and not col.auto_increment:
                changed.append("补 AUTO_INCREMENT")
                if fix:
                    col.auto_increment = True
        else:
            want_default = spec.get("default")
            if want_default is not None and norm_default(col.default) != str(want_default).upper():
                changed.append(f"DEFAULT 改为 {want_default}")
                if fix:
                    col.default = str(want_default)
            if spec.get("on_update") and not col.on_update:
                changed.append("补 ON UPDATE CURRENT_TIMESTAMP")
                if fix:
                    col.on_update = "CURRENT_TIMESTAMP"
        if spec.get("comment") and not col.comment:
            changed.append(f"补 COMMENT '{spec['comment']}'")
            if fix:
                col.comment = spec["comment"]
        if changed:
            res.emit(rule, f"字段 `{name}`", {"name": name, "changed": changed},
                     case="mismatch", auto_fixed=bool(changed and fix))


def check_required_indexes(res, rule) -> None:
    tbl = _table(res)
    if tbl is None or getattr(tbl, "create_select", False):
        return
    fix = rule.auto_fix
    for spec in _required_indexes(rule):
        kind = spec["kind"]
        name = spec.get("name") or ""
        cols = spec.get("columns") or []
        found = None
        if kind == "PRIMARY":
            found = next((i for i in tbl.indexes if i.kind == "PRIMARY"), None)
        else:
            found = next((i for i in tbl.indexes if i.key_name().upper() == name.upper()), None)
        label = "PRIMARY KEY (ID)" if kind == "PRIMARY" else f"{kind} {name}"
        if found is None:
            if fix:
                tbl.indexes.append(Index(kind=kind, name=name,
                                         columns=[_icol(c) for c in cols]))
            res.emit(rule, f"索引 {name or 'PRIMARY'}", {"name": name or "PRIMARY",
                                                        "label": label},
                     case="missing", auto_fixed=bool(fix))
            continue
        want = [c.upper() for c in cols]
        got = [c.name.upper() for c in found.columns]
        if want and got != want:
            if fix:
                found.columns = [_icol(c) for c in cols]
            res.emit(rule, f"索引 {name or 'PRIMARY'}",
                     {"name": name or "PRIMARY KEY", "columns": want}, case="mismatch",
                     auto_fixed=bool(fix))


def check_naming(res, rule) -> None:
    tbl = _table(res)
    if tbl is None:
        return
    fix = rule.auto_fix
    max_len = rule.p_int("max_len", 64)
    manual: list[str] = []

    def _emit(case, target, fields, auto=False):
        res.emit(rule, target, fields, case=case, auto_fixed=auto and fix)

    tname = tbl.name
    if len(tname) > max_len:
        _emit("length", "表名", {"name": tname, "kind": "表名", "max_len": max_len})
        manual.append(f"表名 {tname} 过长")
    if tname != tname.upper():
        old = tname
        if fix:
            tbl.name = tname.upper()
        tname = tbl.name
        _emit("uppercase", f"表名 `{old}`", {"name": old, "kind": "表名", "fixed": tname},
              auto=fix)
    if tname.upper() in RESERVED_WORDS:
        _emit("reserved", f"表名 `{tname}`", {"name": tname, "kind": "表名"})
        manual.append(f"表名 {tname} 是保留字")
    if not NAME_RE.match(tname):
        fixed = _squash(tname)
        if fixed and NAME_RE.match(fixed):
            if fix:
                tbl.name = fixed
            _emit("charset", f"表名 `{tname}`", {"name": tname, "kind": "表名", "fixed": fixed},
                  auto=fix)
            tname = tbl.name
        else:
            _emit("illegal", f"表名 `{tname}`", {"name": tname, "kind": "表名"})
            manual.append(f"表名 {tname} 含非法字符")
    if "_" not in tname:
        _emit("no_business", f"表名 `{tname}`", {"name": tname, "kind": "表名"})
        res.ask("Q-BIZ-NAME", rule.id, {"name": tname}, blocking=True,
                fallback=f"表名 {tname} 没有业务名，请按「业务名_表名」补齐")

    seen = set()
    for col in tbl.columns:
        cname = col.name
        if len(cname) > max_len:
            _emit("length", f"字段 `{cname}`", {"name": cname, "kind": "字段名", "max_len": max_len})
            manual.append(f"字段 {cname} 过长")
        if cname != cname.upper():
            old = cname
            if fix:
                col.name = cname.upper()
            cname = col.name
            _emit("uppercase", f"字段 `{old}`", {"name": old, "kind": "字段名", "fixed": cname},
                  auto=fix)
        if cname.upper() in RESERVED_WORDS:
            _emit("reserved", f"字段 `{cname}`", {"name": cname, "kind": "字段名"})
            manual.append(f"字段 {cname} 是保留字")
        if cname.upper() == "__MODIFY_TIME__":
            seen.add(cname.upper())
            continue
        if not NAME_RE.match(cname):
            fixed = _squash(cname)
            if fixed and NAME_RE.match(fixed):
                if fix:
                    col.name = fixed
                _emit("charset", f"字段 `{cname}`",
                      {"name": cname, "kind": "字段名", "fixed": fixed}, auto=fix)
                cname = fixed
            else:
                _emit("illegal", f"字段 `{cname}`", {"name": cname, "kind": "字段名"})
                manual.append(f"字段 {cname} 含非法字符")
        if cname.upper() in seen:
            _emit("duplicate", f"字段 `{cname}`", {"name": cname, "kind": "字段名"})
            manual.append(f"字段 {cname} 重复")
        seen.add(cname.upper())
    if manual:
        res.ask("Q-RENAME", rule.id, {"items": "；".join(manual)}, blocking=True,
                fallback="以下名称无法自动改写，请确认新名称：" + "；".join(manual))


def check_comments(res, rule) -> None:
    tbl = _table(res)
    if tbl is None or getattr(tbl, "create_select", False):
        return
    fix = rule.auto_fix
    if not tbl.comment:
        if fix:
            tbl.comment = "TODO:待补充表注释"
        res.emit(rule, "表", {"name": tbl.name, "comment": "TODO:待补充表注释"},
                 case="table_missing", auto_fixed=fix, assumed=fix)
    for col in tbl.columns:
        if not col.comment:
            todo = f"TODO:待补充{col.name}注释"
            if fix:
                col.comment = todo
            res.emit(rule, f"字段 `{col.name}`", {"name": col.name, "comment": todo},
                     case="column_missing", auto_fixed=fix, assumed=fix)
    todo_cols = [c.name for c in tbl.columns if (c.comment or "").startswith("TODO:")]
    if todo_cols:
        res.ask("Q-COMMENT", rule.id, {"name": "、".join(todo_cols)},
                fallback="以下字段注释是占位符，请补充真实含义：" + ", ".join(todo_cols))


def check_forbidden_types(res, rule) -> None:
    """参数化：禁用类型清单 + 改写方式（decimal / enum / none）。"""
    tbl = _table(res)
    if tbl is None:
        return
    types = {str(t).upper() for t in rule.p_list("types")}
    if not types:
        return
    mode = str(rule.p("fix_mode", "none")).lower()
    replace_with = str(rule.p("replace_with", ""))
    fix = rule.auto_fix and mode in ("decimal", "enum")
    for col in tbl.columns:
        if col.type.upper() not in types:
            continue
        old = col.type_sql()
        fields = {"name": col.name, "type": col.type.upper(), "old": old,
                  "replace_with": replace_with}
        if mode == "decimal" and fix:
            col.type, col.args = "DECIMAL", ["12", "2"]
            res.emit(rule, f"字段 `{col.name}`", fields, auto_fixed=True, assumed=True)
            res.ask("Q-DECIMAL", rule.id, {"name": col.name}, blocking=True,
                    fallback=f"字段 {col.name} 的精度请确认（当前按 DECIMAL(12,2) 处理）")
        elif mode == "enum" and fix:
            values = [v.strip().strip("'\"") for v in col.args] if col.args else []
            numeric = all(v.isdigit() for v in values if v != "")
            mapping = "，".join(f"{v}={i}" for i, v in enumerate(values) if v != "")
            col.type, col.args = "TINYINT", []
            note = f"枚举映射：{mapping}" if mapping else "枚举字段"
            col.comment = ((col.comment or "") + "；" + note).strip("；")
            fields["note"] = note
            res.emit(rule, f"字段 `{col.name}`", fields, auto_fixed=True, assumed=not numeric)
            if not numeric:
                res.ask("Q-ENUM-MAP", rule.id, {"name": col.name, "values": values},
                        blocking=True,
                        fallback=f"字段 {col.name} 的取值 {values} 请确认数字映射（已按顺序 0..n 生成）")
        else:
            res.emit(rule, f"字段 `{col.name}`", fields)


def check_datetime_types(res, rule) -> None:
    tbl = _table(res)
    if tbl is None:
        return
    allowed = [str(t).upper() for t in rule.p_list("allowed")] or ["DATETIME"]
    forbid_defaults = [str(t) for t in rule.p_list("forbid_defaults")] or [
        "0000-00-00 00:00:00", "0000-00-00"]
    replace_default = str(rule.p("replace_default", "2000-01-01 00:00:00"))
    fix = rule.auto_fix
    for col in tbl.columns:
        t = col.type.upper()
        if t in TIME_TYPES and t not in allowed:
            if fix:
                col.type = allowed[0]
            res.emit(rule, f"字段 `{col.name}`",
                     {"name": col.name, "old": t, "new": allowed[0], "allowed": allowed},
                     case="type", auto_fixed=fix)
        d = norm_default(col.default)
        if any(d.startswith(str(x).upper()) for x in forbid_defaults if str(x).startswith("0000")):
            new_default = ("'" + replace_default + "'") if " " in replace_default else replace_default
            if fix:
                col.default = new_default
            res.emit(rule, f"字段 `{col.name}`",
                     {"name": col.name, "default": new_default, "replace_default": replace_default},
                     case="default_value", auto_fixed=fix)


def check_not_null(res, rule) -> None:
    tbl = _table(res)
    if tbl is None:
        return
    fix = rule.auto_fix
    for col in tbl.columns:
        changed = []
        if col.nullable:
            changed.append("改为 NOT NULL")
            if fix:
                col.nullable = False
        if norm_default(col.default) == "NULL":
            changed.append("去掉 DEFAULT NULL")
            if fix:
                col.default = None
            res.ask("Q-DEFAULT", rule.id, {"name": col.name},
                    fallback=f"字段 {col.name} 改为 NOT NULL 但没有默认值，请补充业务默认值")
        if changed:
            res.emit(rule, f"字段 `{col.name}`", {"name": col.name, "changed": changed},
                     auto_fixed=fix)


def check_engine_charset(res, rule) -> None:
    tbl = _table(res)
    if tbl is None or getattr(tbl, "create_select", False):
        return
    want_engine = str(rule.p("engine", "InnoDB"))
    want_charset = str(rule.p("charset", "utf8mb4"))
    fix = rule.auto_fix
    changed = []
    if not tbl.engine:
        changed.append(f"补 ENGINE={want_engine}")
        if fix:
            tbl.engine = want_engine
    elif tbl.engine.upper() != want_engine.upper():
        changed.append(f"ENGINE {tbl.engine} → {want_engine}")
        if fix:
            tbl.engine = want_engine
    if not tbl.charset:
        changed.append(f"补 DEFAULT CHARSET={want_charset}")
        if fix:
            tbl.charset = want_charset
    elif tbl.charset.lower() != want_charset.lower():
        changed.append(f"CHARSET {tbl.charset} → {want_charset}")
        if fix:
            tbl.charset = want_charset
    if changed:
        res.emit(rule, "表", {"engine": want_engine, "charset": want_charset,
                              "changed": changed}, auto_fixed=fix)


def check_table_type_naming(res, rule) -> None:
    tbl = _table(res)
    if tbl is None:
        return
    name = (tbl.name or "").upper()
    suffixes = rule.p("suffixes") or {"历史表": "_HISTORY", "临时表": "_TMP_日期_月数",
                                      "备份表": "_BAK_日期_月数"}
    prefixes = rule.p("prefixes") or {"归档表": "ARCHIVER_"}
    hist = str(suffixes.get("历史表", "_HISTORY")) if isinstance(suffixes, dict) else "_HISTORY"
    arch = str(prefixes.get("归档表", "ARCHIVER_")) if isinstance(prefixes, dict) else "ARCHIVER_"
    if name.endswith(hist) or (arch and name.startswith(arch)):
        return
    m = _TMP_BAK.search(name)
    if m:
        date_part, months = m.group(2), m.group(3)
        try:
            y, mo, d = int(date_part[:4]), int(date_part[4:6]), int(date_part[6:])
            if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
                raise ValueError
        except ValueError:
            res.emit(rule, f"表名 `{name}`", {"name": name, "date": date_part}, case="bad_date")
        if int(months) <= 0:
            res.emit(rule, f"表名 `{name}`", {"name": name, "months": months}, case="no_months")
        return
    if _SUSPECT_TYPE.search(name):
        rule_txt = "；".join([f"{k}用 {v}" for k, v in
                             list(prefixes.items() if isinstance(prefixes, dict) else [])
                             + list(suffixes.items() if isinstance(suffixes, dict) else [])])
        res.emit(rule, f"表名 `{name}`", {"name": name, "rules": rule_txt}, case="suspect")
        res.ask("Q-TABLE-TYPE", rule.id, {"name": name}, blocking=True,
                fallback=f"表名 {name} 含 TMP/BAK/HISTORY 等字样，请确认表类型与保留月数")


def check_max_columns(res, rule) -> None:
    tbl = _table(res)
    if tbl is None:
        return
    ncol = len(tbl.columns)
    limit = rule.p_int("max_columns", _th(res, "max_columns", 30))
    if ncol > limit:
        res.emit(rule, "表", {"count": ncol, "max": limit})


def check_type_width(res, rule) -> None:
    tbl = _table(res)
    if tbl is None:
        return
    forbid_int_len = rule.p_bool("forbid_int_length", True)
    char_max = rule.p_int("char_max", 20)
    varchar_hint = rule.p_int("varchar_hint", 1024)
    fix = rule.auto_fix
    for col in tbl.columns:
        if forbid_int_len and col.type.upper() in INT_TYPES and col.args:
            old = col.type_sql()
            if fix:
                col.args = []
            res.emit(rule, f"字段 `{col.name}`", {"name": col.name, "old": old,
                                                  "new": col.type_sql()},
                     case="int_length", auto_fixed=fix)
        if col.type.upper() == "CHAR":
            n = arg_int(col)
            if n and n > char_max:
                if fix:
                    col.type = "VARCHAR"
                res.emit(rule, f"字段 `{col.name}`",
                         {"name": col.name, "length": n, "max": char_max,
                          "new": f"VARCHAR({n})"},
                         case="char_wide", auto_fixed=fix,
                         assumed=rule.p_bool("char_assumed", True))
        if col.type.upper() == "VARCHAR":
            n = arg_int(col)
            if n and n > varchar_hint:
                res.emit(rule, f"字段 `{col.name}`", {"name": col.name, "length": n,
                                                      "hint": varchar_hint},
                         case="varchar_hint")


def check_db_name(res, rule) -> None:
    tbl = _table(res)
    db = (tbl.db if tbl is not None else "") or _ctx(res).get("database") or ""
    if not db:
        return
    prefix = str(rule.p("prefix", "demo_"))
    max_len = rule.p_int("max_len", 64)
    if not re.fullmatch(re.escape(prefix) + r"[a-z0-9_]+", db) or len(db) > max_len:
        res.emit(rule, f"库名 `{db}`", {"db": db, "prefix": prefix, "max_len": max_len})


def check_table_exists(res, rule) -> None:
    fact = _fact(res)
    tbl = _table(res)
    if fact is None or fact.exists is not True:
        return
    name = tbl.name if tbl is not None else fact.table
    res.emit(rule, f"表 `{name}`", {"table": name, "rows": fact.table_rows},
             auto_fixed=False)
    res.ask("Q-TABLE-EXISTS", rule.id, {"table": name}, blocking=True,
            fallback=f"{fact.database}.{name} 已存在，请确认是新建表还是变更现有表")


# ==========================================================================
# IX 索引
# ==========================================================================


def check_foreign_key(res, rule) -> None:
    for idx in _indexes(res):
        if idx.kind == "FOREIGN":
            res.emit(rule, f"索引 {idx.key_name() or 'FOREIGN KEY'}",
                     {"name": idx.key_name()})


def check_index_column_exists(res, rule) -> None:
    fact = _fact(res)
    scope = _ctx(res).get("scope", "")
    coltypes = _coltypes(res)
    if fact is not None and fact.exists is False:
        res.emit(rule, f"表 `{fact.table}`", {"table": fact.table}, case="table_missing")
        return
    if scope == "drop_index" and fact is not None and fact.exists:
        name = _ctx(res).get("index_name", "")
        if name and not fact.has_index(name):
            res.emit(rule, f"索引 `{name}`", {"name": name}, case="index_missing")
        return
    if coltypes is None:
        return
    known = {k.upper() for k in coltypes}
    for idx in _indexes(res):
        if idx.kind == "PRIMARY":
            continue
        missing = [c.name for c in idx.columns if c.name.upper() not in known]
        if missing:
            res.emit(rule, f"索引 {idx.key_name()}", {"name": idx.key_name(),
                                                      "columns": missing})


def check_index_big_text(res, rule) -> None:
    coltypes = _coltypes(res)
    if not coltypes:
        return
    allow = {str(t).upper() for t in rule.p_list("types")} or set(TEXT_LIKE | BLOB_LIKE)
    upper_map = {k.upper(): v for k, v in coltypes.items()}
    for idx in _indexes(res):
        bad = [c.name for c in idx.columns
               if c.name.upper() in upper_map and upper_map[c.name.upper()].type.upper() in allow]
        if bad:
            res.emit(rule, f"索引 {idx.key_name()}", {"name": idx.key_name(), "columns": bad})
            if rule.auto_fix:
                idx.columns = [c for c in idx.columns if c.name not in bad]


def check_index_naming(res, rule) -> None:
    idx_prefix = str(rule.p("idx_prefix", "IDX_"))
    uniq_prefix = str(rule.p("uniq_prefix", "UNIQ_"))
    fix = rule.auto_fix
    for idx in _indexes(res):
        if idx.kind not in ("INDEX", "UNIQUE", "FULLTEXT", "SPATIAL"):
            continue
        name = idx.key_name()
        prefix = uniq_prefix if idx.kind == "UNIQUE" else idx_prefix
        if idx.kind in ("FULLTEXT", "SPATIAL"):
            prefix = idx.kind + "_"
        body = name.upper()
        for p in ("IDX_", "UNIQ_", "INDEX_", "KEY_"):
            if body.startswith(p):
                body = body.split("_", 1)[1]
                break
        if not body and idx.columns:
            body = "_".join(c.name.upper() for c in idx.columns)
        want = prefix + body
        if want and want != name:
            if fix:
                idx.name = want
            res.emit(rule, f"索引 `{name}`", {"name": name, "want": want}, auto_fixed=fix)


def check_varchar_prefix(res, rule) -> None:
    coltypes = _coltypes(res)
    if not coltypes:
        return
    upper_map = {k.upper(): v for k, v in coltypes.items()}
    max_prefix = rule.p_int("max_prefix", _th(res, "varchar_prefix_max", 8))
    threshold = rule.p_int("length_threshold", _th(res, "varchar_prefix_threshold", 64))
    fix = rule.auto_fix
    for idx in _indexes(res):
        if idx.kind not in ("INDEX", "UNIQUE"):
            continue
        for ic in idx.columns:
            col = upper_map.get(ic.name.upper())
            if col is None or col.type.upper() not in ("VARCHAR", "VARBINARY"):
                continue
            length = arg_int(col) or 0
            if length <= threshold or ic.prefix_len:
                continue
            if idx.kind == "UNIQUE":
                res.emit(rule, f"索引 {idx.key_name()}",
                         {"name": idx.key_name(), "column": ic.name, "length": length,
                          "max": max_prefix}, case="unique_bad")
                res.ask("Q-VARCHAR-LEN", rule.id,
                        {"name": idx.key_name(), "column": ic.name, "length": length},
                        blocking=True,
                        fallback=f"唯一索引 {idx.key_name()} 建在 VARCHAR({length}) 上，请给出实际最大宽度")
            else:
                if fix:
                    ic.prefix_len = str(max_prefix)
                res.emit(rule, f"索引 {idx.key_name()}",
                         {"name": idx.key_name(), "column": ic.name, "length": length,
                          "max": max_prefix}, case="prefixed", auto_fixed=fix, assumed=fix)
                res.ask("Q-VARCHAR-LEN", rule.id,
                        {"name": idx.key_name(), "column": ic.name, "length": length},
                        fallback=f"索引 {idx.key_name()} 中字段 {ic.name} 已按前缀 {max_prefix} 处理，请确认实际宽度")


def check_index_limits(res, rule) -> None:
    ctx = _ctx(res)
    max_indexes = rule.p_int("max_indexes", _th(res, "max_indexes", 5))
    max_cols = rule.p_int("max_index_cols", _th(res, "max_index_cols", 5))
    max_unique = rule.p_int("max_unique_cols", _th(res, "max_unique_cols", 3))
    count = ctx.get("index_count")
    if count is not None and count > max_indexes and not ctx.get("_index_count_reported"):
        ctx["_index_count_reported"] = True
        res.emit(rule, "表", {"count": count, "max": max_indexes}, case="count")
    fact = _fact(res)
    duplicate = ctx.get("duplicate_index")
    if fact is not None and fact.exists and duplicate:
        res.emit(rule, f"索引 `{duplicate}`", {"name": duplicate}, case="duplicate")
    for idx in _indexes(res):
        if idx.kind in ("INDEX", "UNIQUE") and len(idx.columns) > max_cols:
            res.emit(rule, f"索引 {idx.key_name()}",
                     {"name": idx.key_name(), "count": len(idx.columns), "max": max_cols},
                     case="cols")
        if idx.kind == "UNIQUE" and len(idx.columns) > max_unique:
            res.emit(rule, f"索引 {idx.key_name()}",
                     {"name": idx.key_name(), "count": len(idx.columns), "max": max_unique},
                     case="unique_cols")


# ==========================================================================
# AL 变更
# ==========================================================================


def check_after_first(res, rule) -> None:
    alter = _alter(res)
    if alter is None:
        return
    fix = rule.auto_fix
    for op in alter.ops:
        if not getattr(op, "first_or_after", ""):
            continue
        clause = op.first_or_after
        if fix:
            op.first_or_after = ""
        res.emit(rule, f"字段 `{op.column.name}` 的 {clause} 子句",
                 {"column": op.column.name, "clause": clause}, auto_fixed=fix)


def check_add_column(res, rule) -> None:
    alter = _alter(res)
    if alter is None:
        return
    ctx = _ctx(res)
    fact = _fact(res)
    fix = rule.auto_fix
    float_types = {str(t).upper() for t in (ctx.get("float_types") or [])}
    enum_types = {str(t).upper() for t in (ctx.get("enum_types") or [])}
    forbid_big = {str(t).upper() for t in (ctx.get("blob_types") or [])}
    for op in alter.ops:
        if op.kind != "ADD_COLUMN" or op.column is None:
            continue
        col = op.column
        if fact is not None and fact.exists:
            exist = fact.col_type(col.name)
            if exist:
                res.emit(rule, f"字段 `{col.name}`",
                         {"column": col.name, "type": exist}, case="exists")
                continue
        changed = []
        if col.type.upper() in float_types:
            old = col.type_sql()
            if fix:
                col.type, col.args = "DECIMAL", ["12", "2"]
            changed.append(f"{old} → DECIMAL(12,2)")
            res.ask("Q-DECIMAL", rule.id, {"name": col.name}, blocking=True,
                    fallback=f"新增字段 {col.name} 的精度请确认（当前 DECIMAL(12,2)）")
        if col.type.upper() in enum_types:
            values = [v.strip().strip("'\"") for v in col.args]
            mapping = "，".join(f"{v}={i}" for i, v in enumerate(values))
            if fix:
                col.type, col.args = "TINYINT", []
                col.comment = ((col.comment or "") + "；枚举映射：" + mapping).strip("；")
            changed.append(f"ENUM/SET → TINYINT（注释写入映射：{mapping}）")
            res.ask("Q-ENUM-MAP", rule.id, {"name": col.name}, blocking=True,
                    fallback=f"新增字段 {col.name} 的枚举映射请确认")
        if col.type.upper() in forbid_big:
            res.emit(rule, f"字段 `{col.name}`", {"column": col.name,
                                                  "type": col.type.upper()}, case="big_object")
        if col.type.upper() == "TIMESTAMP":
            if fix:
                col.type = "DATETIME"
            changed.append("TIMESTAMP → DATETIME")
        if norm_default(col.default).startswith("0000-00-00"):
            if fix:
                col.default = "'2000-01-01 00:00:00'"
            changed.append("默认值 0000-00-00 → '2000-01-01 00:00:00'")
        if col.nullable:
            if fix:
                col.nullable = False
            changed.append("改为 NOT NULL")
            res.ask("Q-DEFAULT", rule.id, {"name": col.name},
                    fallback=f"新增字段 {col.name} 改为 NOT NULL，请补充业务默认值")
        if norm_default(col.default) == "NULL":
            if fix:
                col.default = None
            changed.append("去掉 DEFAULT NULL")
        if not col.comment:
            todo = f"TODO:待补充{col.name}注释"
            if fix:
                col.comment = todo
            changed.append(f"补占位注释 '{todo}'")
            res.ask("Q-COMMENT", rule.id, {"name": col.name},
                    fallback=f"新增字段 {col.name} 的注释请补充")
        if col.type.upper() in INT_TYPES and col.args:
            old = col.type_sql()
            if fix:
                col.args = []
            changed.append(f"去掉整型长度（{old} → {col.type_sql()}）")
        if changed:
            res.emit(rule, f"字段 `{col.name}`", {"column": col.name, "changed": changed},
                     auto_fixed=fix)


def check_forbid_rename(res, rule) -> None:
    alter = _alter(res)
    if alter is None:
        return
    ctx = _ctx(res)
    fact = _fact(res)
    for op in alter.ops:
        kind = op.kind
        if kind == "CHANGE_COLUMN":
            res.emit(rule, f"字段 `{op.old_name}` → `{op.column.name}`",
                     {"column": op.column.name, "old": op.old_name,
                      "new": op.column.name}, case="rename_col")
        elif kind == "RENAME_COLUMN":
            res.emit(rule, f"字段 `{op.old_name}` → `{op.name}`",
                     {"column": op.name, "old": op.old_name, "new": op.name},
                     case="rename_col")
        elif kind == "RENAME_INDEX":
            res.emit(rule, f"索引 `{op.old_name}` → `{op.name}`",
                     {"old": op.old_name, "new": op.name}, case="rename_index")
        elif kind == "RENAME_TABLE":
            res.emit(rule, f"表 `{alter.name}` → `{op.name}`",
                     {"table": alter.name, "new": op.name}, case="rename_table")
        elif kind == "DROP_COLUMN":
            exists = bool(fact is not None and fact.exists and fact.col_type(op.name))
            if exists:
                res.emit(rule, f"字段 `{op.name}`",
                         {"column": op.name, "rows": fact.table_rows}, case="drop_col_exists")
            res.emit(rule, f"字段 `{op.name}`", {"column": op.name}, case="drop_col")
        elif kind == "TABLE_OPTION" and re.search(r"CHARACTER|CHARSET|COLLATE", (op.tail or "").upper()):
            res.emit(rule, "表", {"table": alter.name}, case="charset")
        elif kind == "OTHER" and re.match(r"CONVERT", (op.tail or "").upper()):
            res.emit(rule, "表", {"table": alter.name}, case="charset_convert")


def check_modify_type(res, rule) -> None:
    alter = _alter(res)
    if alter is None:
        return
    fact = _fact(res)
    ops = [op for op in alter.ops if op.kind in ("MODIFY_COLUMN", "CHANGE_COLUMN") and op.column]
    if not ops:
        return
    tb06 = res.rule("TB-06")
    if fact is not None and fact.exists is False:
        res.emit(rule, f"表 `{alter.name}`", {"table": alter.name}, case="table_missing")
        return
    if fact is None or not fact.columns:
        res.emit(rule, "MODIFY/CHANGE", {}, case="unknown")
        res.ask("Q-CTX-STRUCT", rule.id,
                {"column": "、".join(op.column.name for op in ops) or "被修改字段"},
                fallback="MODIFY/CHANGE 需要原字段定义才能判定；请传 env + database，或直接贴 SHOW CREATE TABLE")
        return
    for op in ops:
        col = op.column
        cur = fact.col_type(col.name)
        if cur is None:
            res.emit(rule, f"字段 `{col.name}`", {"column": col.name}, case="no_column")
            continue
        new_type = _want(col.type_sql())
        old_type = _want(cur)
        old_base = (cur or "").strip().split("(")[0].split()[0].upper()
        if old_base == "TIMESTAMP" and col.type.upper() == "DATETIME":
            if tb06 is not None and tb06.auto_fix:
                col.type = "DATETIME"
            if tb06 is not None:
                res.emit(tb06, f"字段 `{col.name}`", {"name": col.name, "old": old_base,
                                                      "new": "DATETIME"}, case="type",
                         auto_fixed=bool(tb06.auto_fix))
            continue
        if old_type == new_type:
            res.emit(rule, f"字段 `{col.name}`", {"column": col.name, "old": old_type},
                     case="consistent")
            continue
        res.emit(rule, f"字段 `{col.name}`",
                 {"column": col.name, "new": new_type, "old": old_type}, case="mismatch")
        res.ask("Q-TYPE-CHANGE", rule.id,
                {"name": col.name, "old": old_type, "new": new_type}, blocking=True,
                fallback=f"字段 {col.name} 现网类型为 {old_type}，你写的是 {new_type}，请确认是有意变更还是笔误")


def check_varchar_instant(res, rule) -> None:
    alter = _alter(res)
    if alter is None:
        return
    fact = _fact(res)
    bytes_limit = rule.p_int("bytes", _th(res, "varchar_instant_bytes", 255))
    ops = [op for op in alter.ops if op.kind in ("MODIFY_COLUMN", "ADD_COLUMN") and op.column]
    for op in ops:
        col = op.column
        if col.type.upper() != "VARCHAR":
            continue
        new_n = arg_int(col) or 0
        if new_n <= 0:
            continue
        if op.kind == "ADD_COLUMN":
            if new_n > bytes_limit:
                res.emit(rule, f"字段 `{col.name}`",
                         {"column": col.name, "length": new_n, "bytes": bytes_limit},
                         case="add_cross")
            continue
        if fact is None or not fact.columns:
            res.emit(rule, f"字段 `{col.name}`", {"column": col.name}, case="unknown")
            continue
        cur = fact.col_type(col.name) or ""
        m = re.search(r"\((\d+)", cur)
        old_n = int(m.group(1)) if m else 0
        if old_n and ((old_n <= bytes_limit < new_n) or (new_n <= bytes_limit < old_n)):
            res.emit(rule, f"字段 `{col.name}`",
                     {"column": col.name, "old": old_n, "new": new_n, "bytes": bytes_limit},
                     case="modify_cross")


def check_algorithm_hint(res, rule) -> None:
    alter = _alter(res)
    if alter is None:
        return
    heavy = {op.kind for op in alter.ops} & {
        "MODIFY_COLUMN", "CHANGE_COLUMN", "DROP_COLUMN", "RENAME_TABLE", "TABLE_OPTION"}
    if not heavy:
        return
    lock_hint = getattr(alter, "lock_hint", "")
    if lock_hint:
        return
    res.emit(rule, "表", {"table": alter.name, "hint": str(rule.p("hint", "ALGORITHM=INPLACE, LOCK=NONE"))})


# ==========================================================================
# DX 破坏性 / SC 规模
# ==========================================================================


def check_always(res, rule) -> None:
    """无条件命中（DROP TABLE / TRUNCATE / DROP DATABASE / DROP INDEX 用）。"""
    ctx = _ctx(res)
    names = ctx.get("always_targets")
    if not names:
        name = ctx.get("target_name") or ctx.get("table_name") or ""
        names = [name]
    for name in names:
        data = {"name": name, "table": name, "db": name}
        label = f"表 `{name}`" if rule.id in ("DX-01", "DX-02", "DX-03") else f"索引 `{name}`"
        if rule.id == "DX-03":
            label = f"库 `{name}`"
        res.emit(rule, label, data)
        asks = {
            "DX-01": ("Q-DESTRUCTIVE", f"DROP TABLE {name} 属物理删除，本通道驳回；请说明归档/迁移方案"),
            "DX-02": ("Q-DESTRUCTIVE", f"TRUNCATE {name} 属物理删除，本通道驳回"),
            "DX-03": ("Q-DESTRUCTIVE", f"DROP DATABASE {name} 一律驳回"),
            "DX-04": ("Q-INDEX-DEL", f"确认要删除索引 {name} 吗？请说明删除原因与影响评估"),
        }
        if rule.id in asks:
            qid, fallback = asks[rule.id]
            res.ask(qid, rule.id, {"name": name}, blocking=True, fallback=fallback)
    if rule.id in ("DX-01", "DX-02", "DX-03"):
        res.fatal = f"{rule.id}：{rule.title} 不允许自动改写"


def check_merge_alter(res, rule) -> None:
    """AL-02 需要跨语句视图，由 reviewer 在批次层执行，这里不重复判定。"""
    return


def check_big_table(res, rule) -> None:
    fact = _fact(res)
    touching = bool(_ctx(res).get("touching_rows"))
    std_rows = rule.p_int("rows", _th(res, "big_table_rows", 5_000_000))
    std_gb = rule.p_int("size_gb", _th(res, "big_table_size_gb", 3))
    if fact is None or fact.exists is None:
        if touching:
            res.emit(rule, "表", {"rows": std_rows, "size_gb": std_gb}, case="unknown")
            res.ask("Q-CTX-ROWS", rule.id,
                    fallback="请传 env + database + table（或直接给出表行数），以便判断大表标准与执行窗口")
        return
    if not fact.exists:
        return
    rows = fact.table_rows or 0
    size_gb = fact.size_gb()
    if touching and (rows >= std_rows or size_gb >= std_gb):
        res.emit(rule, "表", {"rows": rows, "size_gb": size_gb,
                              "std_rows": std_rows, "std_gb": std_gb}, case="big")


def check_modify_window(res, rule) -> None:
    fact = _fact(res)
    if fact is None or not fact.exists:
        return
    rows = fact.table_rows or 0
    limit = rule.p_int("rows", _th(res, "modify_column_rows", 8_000_000))
    if rows < limit:
        return
    if not any(f.rule_id == "AL-03" and f.level != INFO for f in res.findings):
        return
    res.emit(rule, "表", {"rows": rows, "max_rows": limit,
                          "window": str(rule.p("window", "21:00~00:00"))})


# ==========================================================================
# 检查注册表（文档里 `- **检查**：xxx` 引用这里的名字）
# ==========================================================================

CHECKS: dict[str, dict] = {
    "forbid_ctas": {
        "func": check_forbid_ctas, "fields": set(),
        "cases": set(), "scopes": {"create_table"},
    },
    "required_columns": {
        "func": check_required_columns, "fields": {"name", "type", "changed"},
        "cases": {"missing", "mismatch"}, "scopes": {"create_table"},
    },
    "required_indexes": {
        "func": check_required_indexes, "fields": {"name", "label", "columns"},
        "cases": {"missing", "mismatch"}, "scopes": {"create_table"},
    },
    "naming": {
        "func": check_naming, "fields": {"name", "kind", "fixed", "max_len", "items"},
        "cases": {"uppercase", "reserved", "length", "charset", "illegal", "duplicate",
                  "no_business"},
        "scopes": {"create_table"},
    },
    "comments": {
        "func": check_comments, "fields": {"name", "comment", "columns"},
        "cases": {"table_missing", "column_missing"}, "scopes": {"create_table"},
    },
    "forbidden_types": {
        "func": check_forbidden_types,
        "fields": {"name", "type", "old", "replace_with", "note"},
        "cases": set(), "scopes": {"create_table"},
    },
    "datetime_types": {
        "func": check_datetime_types,
        "fields": {"name", "old", "new", "allowed", "default", "replace_default"},
        "cases": {"type", "default_value"}, "scopes": {"create_table"},
    },
    "not_null": {
        "func": check_not_null, "fields": {"name", "changed"},
        "cases": set(), "scopes": {"create_table"},
    },
    "engine_charset": {
        "func": check_engine_charset, "fields": {"engine", "charset", "changed"},
        "cases": set(), "scopes": {"create_table"},
    },
    "table_type_naming": {
        "func": check_table_type_naming, "fields": {"name", "date", "months", "rules"},
        "cases": {"bad_date", "no_months", "suspect"}, "scopes": {"create_table"},
    },
    "max_columns": {
        "func": check_max_columns, "fields": {"count", "max"},
        "cases": set(), "scopes": {"create_table"},
    },
    "type_width": {
        "func": check_type_width,
        "fields": {"name", "old", "new", "length", "max", "hint"},
        "cases": {"int_length", "char_wide", "varchar_hint"}, "scopes": {"create_table"},
    },
    "db_name": {
        "func": check_db_name, "fields": {"db", "prefix", "max_len"},
        "cases": set(), "scopes": {"create_table", "alter_table"},
    },
    "table_exists": {
        "func": check_table_exists, "fields": {"table", "rows"},
        "cases": set(), "scopes": {"create_table"},
    },
    "foreign_key": {
        "func": check_foreign_key, "fields": {"name"},
        "cases": set(), "scopes": {"create_table", "alter_table", "create_index"},
    },
    "index_column_exists": {
        "func": check_index_column_exists, "fields": {"name", "columns", "table"},
        "cases": {"missing_col", "table_missing", "index_missing"},
        "scopes": {"create_table", "alter_table", "create_index", "drop_index"},
    },
    "index_big_text": {
        "func": check_index_big_text, "fields": {"name", "columns"},
        "cases": set(), "scopes": {"create_table", "alter_table", "create_index"},
    },
    "index_naming": {
        "func": check_index_naming, "fields": {"name", "want"},
        "cases": set(), "scopes": {"create_table", "alter_table", "create_index"},
    },
    "varchar_prefix": {
        "func": check_varchar_prefix,
        "fields": {"name", "column", "length", "max"},
        "cases": {"unique_bad", "prefixed"},
        "scopes": {"create_table", "alter_table", "create_index"},
    },
    "index_limits": {
        "func": check_index_limits,
        "fields": {"name", "count", "max"},
        "cases": {"count", "cols", "unique_cols", "duplicate"},
        "scopes": {"create_table", "alter_table", "create_index"},
    },
    "after_first": {
        "func": check_after_first, "fields": {"column", "clause"},
        "cases": set(), "scopes": {"alter_table"},
    },
    "merge_alter": {
        "func": check_merge_alter, "fields": {"table", "count", "index"},
        "cases": {"merged", "merged_into"}, "scopes": {"alter_table"},
    },
    "add_column": {
        "func": check_add_column,
        "fields": {"column", "changed", "type"},
        "cases": {"hit", "exists", "big_object"}, "scopes": {"alter_table"},
    },
    "forbid_rename": {
        "func": check_forbid_rename,
        "fields": {"column", "old", "new", "table", "rows"},
        "cases": {"rename_col", "rename_index", "rename_table", "drop_col",
                  "drop_col_exists", "charset", "charset_convert"},
        "scopes": {"alter_table"},
    },
    "modify_type": {
        "func": check_modify_type,
        "fields": {"column", "new", "old", "table"},
        "cases": {"consistent", "mismatch", "unknown", "no_column", "table_missing"},
        "scopes": {"alter_table"},
    },
    "varchar_instant": {
        "func": check_varchar_instant,
        "fields": {"column", "length", "old", "new", "bytes"},
        "cases": {"add_cross", "modify_cross", "unknown"}, "scopes": {"alter_table"},
    },
    "algorithm_hint": {
        "func": check_algorithm_hint, "fields": {"table", "hint"},
        "cases": set(), "scopes": {"alter_table"},
    },
    "always": {
        "func": check_always, "fields": {"name", "table", "db"},
        "cases": set(),
        "scopes": {"drop_table", "truncate", "drop_database", "drop_index"},
    },
    "big_table": {
        "func": check_big_table,
        "fields": {"rows", "size_gb", "std_rows", "std_gb"},
        "cases": {"big", "unknown"},
        "scopes": {"create_table", "alter_table", "create_index"},
    },
    "modify_window": {
        "func": check_modify_window,
        "fields": {"rows", "max_rows", "window"},
        "cases": set(), "scopes": {"alter_table"},
    },
}


def check_specs() -> dict[str, dict]:
    """给 spec.validate() 用的检查能力清单。"""
    return {name: {"fields": set(spec["fields"]) | {"target"},
                   "cases": set(spec["cases"])}
            for name, spec in CHECKS.items()}


def run_scope(res, ruleset, scope: str, ctx: dict) -> None:
    """按文档顺序跑一个语句范围内全部启用的规则。

    v1.4.0 修订：单条检查自身抛异常时**不再只打日志**（那等于静默漏审），
    而是落一条 ⏸ 未判定结论（ENGINE-03），让"这条没审到"对研发与审计都可见。
    """
    res.extra["ctx"] = ctx
    ctx.setdefault("scope", scope)
    res.ruleset = ruleset
    for rule in ruleset.enabled_rules(scope):
        spec = CHECKS.get(rule.check)
        if spec is None:
            continue
        try:
            spec["func"](res, rule)
        except Exception as exc:            # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("检查 %s（%s）执行异常", rule.id, rule.check)
            res.add("ENGINE-03", UNKNOWN, f"规则 {rule.id}",
                    f"规则 {rule.id}（{rule.title}）判定时异常，本条未判定："
                    f"{type(exc).__name__}: {exc}",
                    "请把这条 SQL 与报错发给 DBA；本结论不可当作该规则已通过")
