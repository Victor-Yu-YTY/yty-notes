"""共享类型：审核结果、事实上下文、保留字与命名工具。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 结论级别
# --------------------------------------------------------------------------
ERROR = "error"        # ❌ 违规，必须整改
WARN = "warn"          # ⚠️ 需确认
INFO = "info"          # ✅ 提示
UNKNOWN = "unknown"    # ⏸ 未判定（缺事实）

LEVEL_LABEL = {
    ERROR: "❌ 违规",
    WARN: "⚠️ 需确认",
    INFO: "✅ 提示",
    UNKNOWN: "⏸ 未判定",
}

# MySQL 8.0 保留字（命名禁用）
RESERVED_WORDS = {
    "ACCESSIBLE", "ADD", "ALL", "ALTER", "ANALYZE", "AND", "AS", "ASC", "ASENSITIVE",
    "BEFORE", "BETWEEN", "BIGINT", "BINARY", "BLOB", "BOTH", "BY", "CALL", "CASCADE",
    "CASE", "CHANGE", "CHAR", "CHARACTER", "CHECK", "COLLATE", "COLUMN", "CONDITION",
    "CONSTRAINT", "CONTINUE", "CONVERT", "CREATE", "CROSS", "CUBE", "CUME_DIST",
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_USER", "CURSOR",
    "DATABASE", "DATABASES", "DAY_HOUR", "DAY_MICROSECOND", "DAY_MINUTE", "DAY_SECOND",
    "DEC", "DECIMAL", "DECLARE", "DEFAULT", "DELAYED", "DELETE", "DENSE_RANK", "DESC",
    "DESCRIBE", "DETERMINISTIC", "DISTINCT", "DISTINCTROW", "DIV", "DOUBLE", "DROP",
    "DUAL", "EACH", "ELSE", "ELSEIF", "EMPTY", "ENCLOSED", "ESCAPED", "EXCEPT",
    "EXISTS", "EXIT", "EXPLAIN", "FALSE", "FETCH", "FIRST_VALUE", "FLOAT", "FLOAT4",
    "FLOAT8", "FOR", "FORCE", "FOREIGN", "FROM", "FULLTEXT", "FUNCTION", "GENERATED",
    "GET", "GRANT", "GROUP", "GROUPING", "GROUPS", "HAVING", "HIGH_PRIORITY",
    "HOUR_MICROSECOND", "HOUR_MINUTE", "HOUR_SECOND", "IF", "IGNORE", "IN", "INDEX",
    "INFILE", "INNER", "INOUT", "INSENSITIVE", "INSERT", "INT", "INT1", "INT2", "INT3",
    "INT4", "INT8", "INTEGER", "INTERVAL", "INTO", "IO_AFTER_GTIDS", "IO_BEFORE_GTIDS",
    "IS", "ITERATE", "JOIN", "JSON_TABLE", "KEY", "KEYS", "KILL", "LAG", "LAST_VALUE",
    "LATERAL", "LEAD", "LEADING", "LEAVE", "LEFT", "LIKE", "LIMIT", "LINEAR", "LINES",
    "LOAD", "LOCALTIME", "LOCALTIMESTAMP", "LOCK", "LONG", "LONGBLOB", "LONGTEXT",
    "LOOP", "LOW_PRIORITY", "MASTER_BIND", "MASTER_SSL_VERIFY_SERVER_CERT", "MATCH",
    "MAXVALUE", "MEDIUMBLOB", "MEDIUMINT", "MEDIUMTEXT", "MIDDLEINT",
    "MINUTE_MICROSECOND", "MINUTE_SECOND", "MOD", "MODIFIES", "NATURAL", "NOT",
    "NO_WRITE_TO_BINLOG", "NTH_VALUE", "NTILE", "NULL", "NUMERIC", "OF", "ON",
    "OPTIMIZE", "OPTIMIZER_COSTS", "OPTION", "OPTIONALLY", "OR", "ORDER", "OUT",
    "OUTER", "OUTFILE", "OVER", "PARTITION", "PERCENT_RANK", "PRECISION", "PRIMARY",
    "PROCEDURE", "PURGE", "RANGE", "RANK", "READ", "READS", "READ_WRITE", "REAL",
    "RECURSIVE", "REFERENCES", "REGEXP", "RELEASE", "RENAME", "REPEAT", "REPLACE",
    "REQUIRE", "RESIGNAL", "RESTRICT", "RETURN", "REVOKE", "RIGHT", "RLIKE", "ROW",
    "ROWS", "ROW_NUMBER", "SCHEMA", "SCHEMAS", "SECOND_MICROSECOND", "SELECT",
    "SENSITIVE", "SEPARATOR", "SET", "SHOW", "SIGNAL", "SMALLINT", "SPATIAL",
    "SPECIFIC", "SQL", "SQLEXCEPTION", "SQLSTATE", "SQLWARNING", "SQL_BIG_RESULT",
    "SQL_CALC_FOUND_ROWS", "SQL_SMALL_RESULT", "SSL", "STARTING", "STORED",
    "STRAIGHT_JOIN", "SYSTEM", "TABLE", "TERMINATED", "THEN", "TINYBLOB", "TINYINT",
    "TINYTEXT", "TO", "TRAILING", "TRIGGER", "TRUE", "UNDO", "UNION", "UNIQUE",
    "UNLOCK", "UNSIGNED", "UPDATE", "USAGE", "USE", "USING", "UTC_DATE", "UTC_TIME",
    "UTC_TIMESTAMP", "VALUES", "VARBINARY", "VARCHAR", "VARCHARACTER", "VARYING",
    "VIRTUAL", "WHEN", "WHERE", "WHILE", "WINDOW", "WITH", "WRITE", "XOR",
    "YEAR_MONTH", "ZEROFILL",
}

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass
class Thresholds:
    big_table_rows: int = 5_000_000
    big_table_size_gb: int = 3
    modify_column_rows: int = 8_000_000
    varchar_instant_bytes: int = 255
    max_columns: int = 30
    max_indexes: int = 5
    max_index_cols: int = 5
    max_unique_cols: int = 3
    varchar_prefix_max: int = 8
    varchar_prefix_threshold: int = 64


# --------------------------------------------------------------------------
# 事实上下文（由 metadata 从 dev/test/stage 只读账号取，或由调用方传入）
# --------------------------------------------------------------------------


@dataclass
class TableFact:
    database: str = ""
    table: str = ""
    exists: bool | None = None          # None = 未知
    columns: dict[str, str] = field(default_factory=dict)   # 列名(大写) → 类型定义原文
    nullable: dict[str, bool] = field(default_factory=dict)  # 列名(大写) → 是否允许 NULL
    indexes: dict[str, list[str]] = field(default_factory=dict)  # 索引名 → 列名列表
    table_rows: int | None = None
    data_bytes: int | None = None
    engine: str = ""
    source: str = "none"                # mysql:<env> / provided / none
    note: str = ""

    def col_type(self, name: str) -> str | None:
        return self.columns.get(name.upper())

    def has_index(self, name: str) -> bool:
        return name.upper() in {k.upper() for k in self.indexes}

    def size_gb(self) -> float:
        return round((self.data_bytes or 0) / (1024 ** 3), 2)


# --------------------------------------------------------------------------
# 审核结果
# --------------------------------------------------------------------------


@dataclass
class Finding:
    rule_id: str
    level: str
    target: str
    message: str
    fix: str = ""
    auto_fixed: bool = False
    assumed: bool = False
    statement_index: int = 0
    statement_type: str = ""

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "level": self.level,
            "level_label": LEVEL_LABEL.get(self.level, self.level),
            "target": self.target,
            "message": self.message,
            "fix": self.fix,
            "auto_fixed": self.auto_fixed,
            "assumed": self.assumed,
            "statement_index": self.statement_index,
            "statement_type": self.statement_type,
        }


@dataclass
class Question:
    qid: str
    rule_id: str
    ask: str
    why: str
    blocking: bool = False
    how: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.qid, "rule_id": self.rule_id, "ask": self.ask,
            "why": self.why, "blocking": self.blocking, "how": self.how,
        }


@dataclass
class ReviewResult:
    """单条语句的审核结果。

    v1.4 起：级别 / 说明 / 整改 / 反问话术全部来自 standards/*.md + config/questions.yaml，
    代码里的检查函数只负责「判定 + 填数据字段 + 指明命中情形」。
    """
    statement_index: int = 0
    statement_type: str = ""
    sql_input: str = ""
    sql_final: str = ""
    findings: list[Finding] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    fatal: str = ""
    facts: TableFact | None = None
    table: object | None = None      # model.Table（CREATE TABLE 解析结果）
    alter: object | None = None      # alter.AlterTable
    ruleset: object | None = None    # spec.RuleSet（判定依据全在文档）
    thresholds: dict = field(default_factory=dict)   # 文档阈值 + settings 兜底
    qcatalog: dict = field(default_factory=dict)      # config/questions.yaml 反问目录
    extra: dict = field(default_factory=dict)

    def add(self, rule_id, level, target, message, fix="", auto_fixed=False, assumed=False):
        self.findings.append(Finding(
            rule_id=rule_id, level=level, target=target, message=message, fix=fix,
            auto_fixed=auto_fixed, assumed=assumed,
            statement_index=self.statement_index, statement_type=self.statement_type))

    # ---- 文档驱动：渲染并落一条结论 ----

    def rule(self, rule_id: str):
        """取当前规范里的规则对象；不存在或已停用 → None。"""
        rs = self.ruleset
        if rs is None:
            return None
        r = rs.get(rule_id)
        if r is None or not r.enabled:
            return None
        return r

    def emit(self, rule, target: str, fields: dict | None = None, case: str | None = None,
             auto_fixed: bool = False, assumed: bool = False,
             level: str | None = None, message: str | None = None,
             fix: str | None = None) -> None:
        """按文档的级别/说明/整改/情形落一条结论。

        ``case`` 命中 ``- **情形**：`` 行时覆盖级别与文案；
        ``**自动改写**：false`` 的规则即使代码改写了，也只当建议（auto_fixed 落 False）。
        """
        from .spec import render        # 延迟导入，避免循环引用

        rc = rule.case_of(case) if case else None
        lv = level or (rc.level if rc and rc.level else rule.level)
        msg = message if message is not None else (rc.message if rc and rc.message else rule.message)
        fx = fix if fix is not None else (rc.fix if rc and rc.fix else rule.fix)
        self.add(rule.id, lv, target,
                 render(msg, fields, self.thresholds) or rule.title,
                 render(fx, fields, self.thresholds),
                 auto_fixed=bool(auto_fixed and rule.auto_fix), assumed=assumed)

    def ask(self, qid: str, rule_id: str, fields: dict | None = None,
            blocking: bool = False, fallback: str = "", why: str = "",
            how: str = "") -> None:
        """登记反问；话术优先取 config/questions.yaml（支持 {占位符}）。

        闸门：渲染后若仍残留 ``{占位符}``（目录话术与调用方字段不匹配），
        改用调用方的 fallback 文本——绝不把花括号直接发给研发。
        """
        if any(q.qid == qid for q in self.questions):
            return
        from .spec import PLACEHOLDER_RE, render

        item = (self.qcatalog or {}).get(qid) or {}

        def _render(template: str) -> str:
            txt = render(template or "", fields, self.thresholds)
            return "" if (txt and PLACEHOLDER_RE.search(txt)) else txt

        text = _render(item.get("ask")) or fallback or qid
        why_text = _render(item.get("why")) or why or text
        how_text = _render(item.get("how")) or how
        self.questions.append(Question(qid, rule_id, text, why_text,
                                       bool(blocking or item.get("blocking")), how_text))

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)

    def has_error(self) -> bool:
        return self.count(ERROR) > 0

    def unresolved_error(self) -> bool:
        return any(f.level == ERROR and not f.auto_fixed for f in self.findings)

    def has_blocking(self) -> bool:
        return any(q.blocking for q in self.questions)

    def verdict(self) -> str:
        """pass / fix_required / need_more_info / rejected

        注意：⏸ 未判定（缺事实）一律算 need_more_info —— 不能把"没查"当成"通过"。
        """
        if self.fatal:
            return "rejected"
        if self.unresolved_error() or self.has_blocking() or self.count(UNKNOWN) > 0:
            return "need_more_info"
        if any(f.auto_fixed for f in self.findings):
            return "fix_required"
        return "pass"


def norm_default(expr: str | None) -> str:
    return (expr or "").strip().strip("'\"").upper()


def arg_int(col, idx: int = 0) -> int | None:
    try:
        return int(col.args[idx])
    except (IndexError, ValueError):
        return None
