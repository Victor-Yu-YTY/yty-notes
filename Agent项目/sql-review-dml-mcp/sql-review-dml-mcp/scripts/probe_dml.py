"""DML 解析实测：把一组语句丢进 dml.parse_dml，打印事实取值。

用途：写/改 standards/dml.md 前先确认「事实算得对」，避免规则基于错的事实。
运行：PYTHONPATH=src python scripts/probe_dml.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sql_review_dml.dml import is_transaction_control, parse_dml  # noqa: E402

CASES: list[tuple[str, str, str]] = [
    ("insert", "多值 INSERT",
     "INSERT INTO demo_biz.T_ORDER (ID, TITLE, REMARK) VALUES (1,'a',NULL),(2,'b','x'),(3,'c','y')"),
    ("insert", "不写列名",
     "INSERT INTO demo_biz.T_ORDER VALUES (1,'a','b')"),
    ("insert", "INSERT ... SELECT",
     "INSERT INTO demo_biz.T_ORDER (ID) SELECT ID FROM demo_biz.T_ORDER_BAK"),
    ("insert", "INSERT ... SET + IGNORE",
     "INSERT IGNORE INTO T_ORDER SET ID = 1, CREATE_TIME = NOW()"),
    ("insert", "ON DUPLICATE KEY",
     "INSERT INTO T_ORDER (ID, TITLE) VALUES (1,'a') ON DUPLICATE KEY UPDATE TITLE = 'b'"),
    ("update", "单表 + WHERE + ORDER BY + LIMIT",
     "UPDATE T_ORDER SET TITLE = 'x', MODIFY_TIME = NOW() WHERE ID = 1 ORDER BY ID LIMIT 10"),
    ("update", "多表 JOIN",
     "UPDATE T_ORDER o JOIN T_USER u ON o.USER_ID = u.ID SET o.TITLE = u.NAME WHERE u.ID = 1"),
    ("update", "无 WHERE",
     "UPDATE T_ORDER SET STATUS = 0"),
    ("update", "WHERE 函数包裹 + 前置 LIKE",
     "UPDATE T_ORDER SET A = 1 WHERE DATE(CREATE_TIME) = '2026-01-01' AND TITLE LIKE '%abc'"),
    ("update", "子查询引用同表",
     "UPDATE T_ORDER SET A = 1 WHERE ID IN (SELECT ID FROM T_ORDER WHERE B = 2)"),
    ("update", "多表 + LIMIT（MySQL 语法非法）",
     "UPDATE T_ORDER o JOIN T_USER u ON o.USER_ID = u.ID SET o.TITLE = 'x' LIMIT 5"),
    ("update", "OR 条件",
     "UPDATE T_ORDER SET A = 1 WHERE ID = 1 OR USER_ID = 2"),
    ("delete", "带 WHERE 与 LIMIT",
     "DELETE FROM T_ORDER WHERE ID = 1 LIMIT 1"),
    ("delete", "无 WHERE",
     "DELETE FROM T_ORDER"),
    ("replace", "REPLACE INTO",
     "REPLACE INTO T_ORDER (ID, TITLE) VALUES (1, 'a')"),
]

FIELDS = [
    "kind", "db", "table", "multi_table", "has_where", "where_columns", "where_complex",
    "where_has_id", "has_order_by", "has_limit", "limit_value", "insert_columns",
    "insert_rows", "insert_has_null", "insert_select", "on_duplicate_key", "ignore_modifier",
    "set_targets", "has_subquery", "subquery_target_self", "nondeterministic_calls",
    "func_wrapped_columns", "leading_like_columns", "unsure",
]


def main() -> int:
    bad = 0
    for kind, name, sql in CASES:
        st = parse_dml(sql, kind)
        print(f"\n=== [{kind}] {name}")
        print(f"    {sql}")
        shown = []
        for f in FIELDS:
            v = getattr(st, f)
            if v in ("", None, [], {}, False):
                continue
            shown.append(f"{f}={v!r}")
        print("    " + ("; ".join(shown) if shown else "（全部为空/False）"))
        if st.unsure:
            bad += 1
            print(f"    ⚠️ unsure: {st.note}")

    print("\n=== 事务控制识别")
    for sql in ("BEGIN", "START TRANSACTION", "COMMIT", "ROLLBACK",
                "START TRANSACTION;", "INSERT INTO T VALUES (1)"):
        print(f"    {sql!r:38} -> {is_transaction_control(sql)}")

    print(f"\n合计 {len(CASES)} 条用例，解析不确定 {bad} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
