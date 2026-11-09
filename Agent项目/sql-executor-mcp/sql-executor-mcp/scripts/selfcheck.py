"""自检：标准文件校验 + 解析器断言 + 用例集 + 执行路径（全部离线，不连库）。

规则来自 `standards/executor.md`，本脚本同时承担「改完标准必跑」的验收职责：

    python scripts/selfcheck.py
    python scripts/selfcheck.py --cases tests/cases/basic.yaml
    python scripts/selfcheck.py --standard standards/executor.md
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

try:  # 正式环境按 requirements 装 PyYAML；本机可能没有，走极简兜底
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def _yaml_load(path: Path):
    """读 YAML：优先 PyYAML；没装就用 scripts/_yaml_lite.py（只够读用例文件）。"""
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    from _yaml_lite import loads
    return loads(text)

from sql_executor_mcp import parser as P                     # noqa: E402
from sql_executor_mcp import executor as EX                 # noqa: E402
from sql_executor_mcp import rollback as RB                 # noqa: E402
from sql_executor_mcp.backend import StubProvider           # noqa: E402
from sql_executor_mcp.engine import (LEVEL_BLOCK, LEVEL_WARN,  # noqa: E402
                                     load_policy)
from sql_executor_mcp.preflight import run as run_preflight  # noqa: E402
from sql_executor_mcp.session import ScriptedSession        # noqa: E402
from sql_executor_mcp.store import ExecutionStore           # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# 规则集的期望规模：**改了标准（增删规则）就要同步改这里**，否则自检会红——
# 这是有意的，防止规则被误删/误改却没人发现。
STANDARD_EXPECT = {"rules_count": 44, "block_count": 31, "warn_count": 13}


# ---------------------------------------------------------------- 标准文件

def check_standard(standard_path: Path) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = ""):
        out.append((name, bool(cond), detail))

    policy = load_policy(str(standard_path))
    counts = policy.counts()

    ok("std.engine", policy.engine == "exec-v1", policy.engine)
    ok("std.mode", policy.mode in ("live", "shadow"), policy.mode)
    ok("std.version", isinstance(policy.version, int) and policy.version >= 1, policy.version)
    ok("std.sha256", len(policy.sha256 or "") == 64, (policy.sha256 or "")[:16])
    ok("std.rules_count", counts["rules_count"] == STANDARD_EXPECT["rules_count"],
       f"期望 {STANDARD_EXPECT['rules_count']}，实际 {counts['rules_count']}"
       "（改规则请同步更新 STANDARD_EXPECT）")
    ok("std.block_count", counts["block_count"] == STANDARD_EXPECT["block_count"],
       f"期望 {STANDARD_EXPECT['block_count']}，实际 {counts['block_count']}")
    ok("std.warn_count", counts["warn_count"] == STANDARD_EXPECT["warn_count"],
       f"期望 {STANDARD_EXPECT['warn_count']}，实际 {counts['warn_count']}")

    ids = [c.id for c in policy.checks]
    ok("std.unique_ids", len(ids) == len(set(ids)), [x for x in ids if ids.count(x) > 1])

    # 环境表：开放执行的环境必须有允许类型；prod 不开放
    for env in ("dev", "test", "stage"):
        cfg = policy.env_cfg(env)
        ok(f"std.env.{env}.enabled", bool(cfg.get("enabled")), cfg)
        ok(f"std.env.{env}.types", len(cfg.get("allowed_types") or []) == 5,
           cfg.get("allowed_types"))
    ok("std.env.prod.closed", not policy.env_cfg("prod").get("enabled"), policy.env_cfg("prod"))

    # 阈值齐全（规则里被 $引用到的必须能在阈值表里找到）
    need = {"max_statements", "max_sql_bytes", "big_table_rows", "big_table_size_gb",
            "modify_window_rows", "max_insert_rows", "max_affected_rows",
            "ddl_disk_factor", "ddl_disk_floor_gb", "replica_lag_seconds",
            "long_trx_seconds", "lock_wait_timeout", "exec_timeout_seconds",
            "count_timeout_seconds", "recent_dup_window_minutes", "max_table_name_len"}
    ok("std.thresholds", need <= set(policy.thresholds), sorted(need - set(policy.thresholds)))

    # 每条规则的适用类型合法、说明非空
    bad_applies = [c.id for c in policy.checks
                   if any(t != "all" and t not in EX_VALID_TYPES for t in c.applies_to)]
    ok("std.applies", not bad_applies, bad_applies)
    empty_msg = [c.id for c in policy.checks if not (c.message or "").strip()]
    ok("std.messages", not empty_msg, empty_msg)

    # 停用字段能生效
    ok("std.enabled_flag", all(c.enabled for c in policy.checks), "存在已停用规则")

    # 解析失败要抛 PolicyError（临时写一个坏文件验证；写不了就跳过这组断言）
    bad = ROOT / ".selfcheck-bad-standard.md"
    bad_text = ("**引擎**：exec-v1\n**模式**：live\n\n## 阈值\n| 名称 | 值 |\n"
                "| --- | --- |\n| x | 1 |\n\n## 环境\n| 环境 | 开放执行 | 允许类型 | 执行窗口 |\n"
                "| --- | --- | --- | --- |\n| dev | true | insert | - |\n\n"
                "### EX-G01 坏的判定\n- **级别**：block\n- **适用**：all\n"
                "- **判定**：nosuch_fact = 1\n- **说明**：x\n")
    try:
        bad.write_text(bad_text, encoding="utf-8")
    except OSError as exc:
        ok("std.rejects_unknown_fact", False, f"无法写临时文件，跳过：{exc}")
        return out
    try:
        try:
            load_policy(str(bad))
            ok("std.rejects_unknown_fact", False, "未知事实居然通过了校验")
        except Exception as exc:  # noqa: BLE001
            ok("std.rejects_unknown_fact", "nosuch_fact" in str(exc), str(exc)[:80])
    finally:
        try:
            bad.unlink()
        except OSError:
            pass

    return out


EX_VALID_TYPES = set(P.DDL_KINDS) | set(P.DML_KINDS)


# ---------------------------------------------------------------- 解析器断言

def check_parser() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = ""):
        out.append((name, bool(cond), detail))

    r = P.parse("CREATE TABLE `db`.`t` (ID BIGINT NOT NULL AUTO_INCREMENT, "
                "NAME VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'n', "
                "PRIMARY KEY (ID), KEY `IDX_NAME` (NAME)) ENGINE=InnoDB;")
    s = r.statements[0]
    ok("ct.kind", s.kind == P.K_CREATE_TABLE, s.kind)
    ok("ct.db", s.database == "db", s.database)
    ok("ct.table", s.table == "t", s.table)
    ok("ct.cols", [c.name for c in s.columns] == ["ID", "NAME"], [c.name for c in s.columns])
    ok("ct.pk", s.primary_key == ["ID"], s.primary_key)
    ok("ct.engine", s.engine == "InnoDB", s.engine)
    ok("ct.notnull", s.columns[0].nullable is False)
    ok("ct.default", s.columns[1].has_default is True)

    r = P.parse("ALTER TABLE t ADD COLUMN C1 INT NOT NULL DEFAULT 0, "
                "ADD INDEX IDX_C1 (C1), MODIFY COLUMN C2 VARCHAR(200);")
    s = r.statements[0]
    ok("at.kind", s.kind == P.K_ALTER_TABLE)
    ok("at.ops", [o.kind for o in s.ops] ==
       [P.OP_ADD_COLUMN, P.OP_ADD_INDEX, P.OP_MODIFY_COLUMN], [o.kind for o in s.ops])
    ok("at.addcol", s.ops[0].column == "C1", s.ops[0].column)
    ok("at.idx", s.ops[1].index_name == "IDX_C1", s.ops[1].index_name)
    ok("at.idxcols", [c.upper() for c in s.ops[1].columns] == ["C1"], s.ops[1].columns)
    ok("at.modcol", s.ops[2].column == "C2", s.ops[2].column)

    r = P.parse("ALTER TABLE t ADD COLUMN C3 INT AFTER C1;")
    ok("at.after", "AFTER" in r.statements[0].ops[0].raw.upper())

    r = P.parse("CREATE UNIQUE INDEX UNIQ_A ON t (A, B(10));")
    s = r.statements[0]
    ok("ix.kind", s.kind == P.K_CREATE_INDEX, s.kind)
    ok("ix.name", s.index_name == "UNIQ_A", s.index_name)
    ok("ix.cols", [c.upper() for c in s.index_columns] == ["A", "B"], s.index_columns)

    r = P.parse("INSERT INTO t (ID, NAME) VALUES (1,'a'),(2,'b'),(3,'c');")
    s = r.statements[0]
    ok("in.rows", s.insert_rows == 3, s.insert_rows)
    ok("in.cols", [c.upper() for c in s.insert_columns] == ["ID", "NAME"], s.insert_columns)

    r = P.parse("INSERT INTO t SELECT * FROM t2;")
    ok("in.select", r.statements[0].insert_is_select is True)

    r = P.parse("UPDATE t SET A=1, B=2 WHERE ID IN (1,2) LIMIT 10;")
    s = r.statements[0]
    ok("up.kind", s.kind == P.K_UPDATE, s.kind)
    ok("up.set", [c.upper() for c in s.set_columns] == ["A", "B"], s.set_columns)
    ok("up.where", s.has_where is True)
    ok("up.limit", s.limit_value == 10, s.limit_value)
    ok("up.wheresql", "ID IN (1,2)" in (s.where_sql or ""), s.where_sql)
    ok("up.wherefull", (s.where_full or "").upper().endswith("LIMIT 10"), s.where_full)

    r = P.parse("UPDATE t1 JOIN t2 ON t1.ID=t2.ID SET t1.A=1 WHERE t1.ID>0;")
    ok("up.multi", r.statements[0].multi_table is True)

    r = P.parse("UPDATE t SET A=1;")
    ok("up.nowhere", r.statements[0].has_where is False)

    # 字符串里的分号不能被切开
    r = P.parse("INSERT INTO t (A) VALUES ('x;y');")
    ok("split.semicolon", len(r.statements) == 1, len(r.statements))

    # 注释
    r = P.parse("-- 注释\nINSERT INTO t (A) VALUES (1); /* 块注释 */ ")
    ok("split.comment", len(r.statements) == 1 and r.statements[0].insert_rows == 1)

    # 不支持的类型要能识别出名字
    ok("kind.delete", P.classify("DELETE FROM t WHERE ID=1") == "delete")
    ok("kind.truncate", P.classify("TRUNCATE TABLE t") == "truncate")
    ok("kind.drop", P.classify("DROP TABLE t") == "drop_table")

    return out


# ---------------------------------------------------------------- 权限解析

def check_privileges() -> list[tuple[str, bool, str]]:
    """SHOW GRANTS 解析（离线）。

    这是一条真实踩过的坑：把整行按逗号切，第一段会变成 "GRANT SELECT"，
    于是 SELECT 永远判缺失、EX-G07 把所有语句都拦掉。StubProvider 注入的是
    「权限集合」而不是「SHOW GRANTS 文本」，所以只有这里能兜住。
    """
    from sql_executor_mcp.backend import parse_privileges

    out: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = ""):
        out.append((name, bool(cond), detail))

    g_global = parse_privileges(
        ["GRANT SELECT, INSERT, UPDATE, CREATE, ALTER, INDEX ON *.* TO `exec_user`@`%`"])
    ok("priv.global", g_global == {"SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"},
       sorted(g_global))
    ok("priv.no_glued", all(not p.startswith("GRANT") for p in g_global), sorted(g_global))

    g_perdb = parse_privileges(
        ["GRANT SELECT, INSERT, UPDATE, CREATE, ALTER, INDEX ON `demo_app`.* TO `exec_user`@`%`"])
    ok("priv.perdb", "SELECT" in g_perdb and len(g_perdb) == 6, sorted(g_perdb))

    g_all = parse_privileges(
        ["GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION"])
    ok("priv.all_privileges", g_all == {"ALL PRIVILEGES"}, sorted(g_all))

    g_usage = parse_privileges(["GRANT USAGE ON *.* TO `x`@`%`"])
    ok("priv.usage", g_usage == {"USAGE"}, sorted(g_usage))

    g_col = parse_privileges(["GRANT SELECT (ID), UPDATE (NAME) ON `db`.`t` TO `x`@`%`"])
    ok("priv.column_level", g_col == {"SELECT", "UPDATE"}, sorted(g_col))

    g_proxy = parse_privileges(["GRANT PROXY ON ''@'' TO `x`@`%`"])
    ok("priv.proxy_ignored", g_proxy == set(), sorted(g_proxy))

    ok("priv.empty", parse_privileges([]) == set())

    return out


# ---------------------------------------------------------------- 用例

def run_cases(standard_path: Path, cases_path: Path) -> tuple[int, int, list[str]]:
    policy = load_policy(str(standard_path))
    raw = _yaml_load(cases_path) or []
    cases = raw.get("cases") if isinstance(raw, dict) else raw

    passed = failed = 0
    failures: list[str] = []

    print(f"\n{'=' * 78}\n用例集：{cases_path.name}（共 {len(cases)} 条）\n{'=' * 78}")

    for case in cases:
        cid = case["id"]
        env = case.get("env", "dev")
        cfg = policy.env_cfg(env)
        provider = StubProvider(case.get("facts") or {})

        now = None
        if case.get("now"):
            from datetime import datetime
            now = datetime.fromisoformat(case["now"])

        res = run_preflight(
            sql=case["sql"], env=env, database=case.get("database", ""),
            policy=policy, provider=provider, env_cfg=cfg or {"enabled": False},
            recent_same_sql_count=case.get("recent_same_sql_count", 0),
            now=now,
        )

        problems: list[str] = []
        want = case.get("expect_decision", "READY")
        if res.decision != want:
            problems.append(f"decision 期望 {want}，实际 {res.decision}")

        for cid_want in case.get("expect_blocked_by") or []:
            if cid_want not in res.blocked_by:
                problems.append(f"期望命中 {cid_want}，实际 {res.blocked_by or '无'}")
        for cid_want in case.get("expect_warn") or []:
            if cid_want not in res.warnings:
                problems.append(f"期望 warn {cid_want}，实际 {res.warnings or '无'}")
        for cid_ban in case.get("expect_not_blocked_by") or []:
            if cid_ban in res.blocked_by:
                problems.append(f"不应命中 {cid_ban}")

        if problems:
            failed += 1
            line = f"{RED}FAIL{RESET}  {cid:32s} {res.decision:8s} {res.blocked_by}"
            print(line)
            for p in problems:
                print(f"      {DIM}- {p}{RESET}")
            if res.errors:
                print(f"      {DIM}- errors: {res.errors}{RESET}")
            detail = {c["id"]: c.get("message") or c["title"]
                      for c in res.checks if c["hit"]}
            if detail:
                print(f"      {DIM}- 实际命中: {detail}{RESET}")
            failures.append(f"{cid}: {'; '.join(problems)}")
        else:
            passed += 1
            warn = f" warn={res.warnings}" if res.warnings else ""
            print(f"{GREEN}PASS{RESET}  {cid:32s} {res.decision:8s} {res.blocked_by or ''}{warn}")

    return passed, failed, failures


# ---------------------------------------------------------------- 执行路径（离线）

DDL_T = ("CREATE TABLE `t` (\n"
         "  `ID` bigint NOT NULL AUTO_INCREMENT,\n"
         "  `C_OLD` varchar(32) NOT NULL DEFAULT '',\n"
         "  PRIMARY KEY (`ID`)\n"
         ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


class ProbeSession(ScriptedSession):
    """在业务语句真正下发的那一刻回看一下 undo 文件，验证「写前落盘」。"""

    def __init__(self, *a, probe=None, **kw):
        super().__init__(*a, **kw)
        self.probe = probe
        self.probe_at_exec: int | None = None

    def execute(self, sql: str, args: tuple | None = None) -> int:
        import re
        if self.probe is not None and re.match(
                r"^\s*(ALTER|CREATE|INSERT|UPDATE|REPLACE)\b", sql, re.I):
            self.probe_at_exec = self.probe()
        return super().execute(sql, args)


def _undo_lines(directory: Path) -> int:
    n = 0
    for p in directory.glob("undo-*.jsonl"):
        n += len([x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()])
    return n


def _facts(**over) -> dict:
    f = {
        "table": {"exists": True, "rows": 1000, "size_gb": 0.1, "engine": "InnoDB"},
        "columns": ["ID", "NAME"], "primary_key": ["ID"],
        "types": {"ID": "bigint", "NAME": "varchar(64)"},
        "count_rows": 2, "explain_type": "ref",
    }
    f.update(over)
    return f


def check_executor(tmp_root: Path, standard_path: Path) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = ""):
        out.append((name, bool(cond), detail))

    def run(case_id: str, sql: str, facts: dict, *, queries=None, rowcounts=None,
            fail_on=None, max_rows=None, dry_run=EX.DRY_OFF, sha="", database="db",
            factory_fail=False):
        policy = load_policy(str(standard_path))
        if max_rows is not None:
            policy.thresholds["max_affected_rows"] = max_rows
        d = tmp_root / case_id
        d.mkdir(parents=True, exist_ok=True)
        store = ExecutionStore(d)
        sess = ProbeSession(queries, rowcounts or [(r"^INSERT|^UPDATE|^ALTER|^CREATE", 1)],
                            fail_on)
        sess.probe = lambda: _undo_lines(d)
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if factory_fail:
                raise RuntimeError("模拟连不上")
            return sess

        outcome = EX.execute(
            sql=sql, env="dev", database=database, policy=policy,
            provider=StubProvider(facts), session_factory=factory,
            env_cfg=policy.env_cfg("dev"), store=store, dry_run=dry_run,
            sql_sha256=sha,
        )
        outcome.session_calls = calls["n"]      # type: ignore[attr-defined]
        return outcome, sess, store, policy

    # ---- 1. UPDATE 正常路径：before-image → 逐行 undo → 提交
    o, s, store, _ = run(
        "up_ok", "UPDATE `db`.`t` SET NAME='x' WHERE ID IN (1,2);", _facts(),
        queries=[(r"FOR UPDATE", [{"ID": 1, "NAME": "a"}, {"ID": 2, "NAME": "b"}])],
        rowcounts=[(r"^UPDATE", 2)])
    ok("up.ready", o.decision == "READY", o.decision)
    ok("up.executed", o.executed and o.committed)
    ok("up.affected", o.results[0].affected_rows == 2, o.results[0].affected_rows)
    ok("up.matched", o.results[0].matched_rows == 2, o.results[0].matched_rows)
    ok("up.rb_available", o.rollback.available)
    ok("up.rb_sql", o.rollback.sql == [
        "UPDATE `db`.`t` SET `NAME`='a' WHERE `ID`=1;",
        "UPDATE `db`.`t` SET `NAME`='b' WHERE `ID`=2;"], o.rollback.sql)
    ok("up.undo_lines", _undo_lines(store.dir) == 3, _undo_lines(store.dir))
    ok("up.probe", (s.probe_at_exec or 0) >= 3, s.probe_at_exec)
    ok("up.committed_once", s.committed == 1, s.committed)
    ok("up.use_db", any(x.upper().startswith("USE `DB`") for x in s.executed()), s.executed())

    # ---- 2. TOCTOU：判定时 10 行，执行前 before-image 已 11 行 → 执行前就放弃
    o, s, _, _ = run(
        "up_toctou", "UPDATE `db`.`t` SET NAME='x' WHERE ID>0;",
        _facts(count_rows=10), max_rows=10,
        queries=[(r"FOR UPDATE", [{"ID": i, "NAME": "a"} for i in range(11)])])
    ok("up.toctou_abort", o.results[0].status == EX.S_ABORT, o.results[0].status)
    ok("up.toctou_not_executed", not o.executed and s.rolled_back == 1)
    ok("up.toctou_no_update", not s.find(r"^UPDATE `db`"), s.executed())

    # ---- 3. 执行后 ROW_COUNT 超限 → 整批回滚
    o, s, _, _ = run(
        "up_guard", "UPDATE `db`.`t` SET NAME='x' WHERE ID>0;",
        _facts(count_rows=5), max_rows=10,
        queries=[(r"FOR UPDATE", [{"ID": i, "NAME": "a"} for i in range(5)])],
        rowcounts=[(r"^UPDATE", 12)])
    ok("up.guard_status", o.results[0].status == EX.S_GUARD, o.results[0].status)
    ok("up.guard_rolled_back", not o.executed and s.rolled_back == 1)

    # ---- 4. INSERT 显式主键 → 精确 DELETE
    o, _, _, _ = run(
        "ins_pk", "INSERT INTO `db`.`t` (ID, NAME) VALUES (1,'a'),(2,'b');",
        _facts(auto_increment=["ID"]), rowcounts=[(r"^INSERT", 2)])
    ok("ins.executed", o.executed)
    ok("ins.rb_sql", o.rollback.sql == ["DELETE FROM `db`.`t` WHERE `ID` IN (1, 2);"],
       o.rollback.sql)
    ok("ins.affected", o.results[0].affected_rows == 2)

    # ---- 5. INSERT 无主键列 → 自增区间
    o, _, _, _ = run(
        "ins_ai", "INSERT INTO `db`.`t` (NAME) VALUES ('a'),('b');",
        _facts(auto_increment=["ID"]), rowcounts=[(r"^INSERT", 2)],
        queries=[(r"LAST_INSERT_ID", [{"lid": 101}]),
                 (r"auto_increment_increment", [{"inc": 1}]),
                 (r"BETWEEN 101 AND 102", [{"c": 2}])])
    ok("ins.ai_sql", o.rollback.sql == [
        "DELETE FROM `db`.`t` WHERE `ID` BETWEEN 101 AND 102;"], o.rollback.sql)
    ok("ins.ai_clean", "人工核对" not in (o.rollback.note or ""), o.rollback.note)

    # ---- 6. 自增区间不干净 → 提示人工核对
    o, _, _, _ = run(
        "ins_ai_dirty", "INSERT INTO `db`.`t` (NAME) VALUES ('a'),('b');",
        _facts(auto_increment=["ID"]), rowcounts=[(r"^INSERT", 2)],
        queries=[(r"LAST_INSERT_ID", [{"lid": 101}]),
                 (r"auto_increment_increment", [{"inc": 1}]),
                 (r"BETWEEN 101 AND 102", [{"c": 3}])])
    ok("ins.ai_dirty", "人工核对" in (o.rollback.note or ""), o.rollback.note)

    # ---- 7-11. 新增的拦截项
    for cid, sql, want in [
        ("blk_ignore", "INSERT IGNORE INTO `db`.`t` (NAME) VALUES ('a');", "EX-N06"),
        ("blk_dup", "INSERT INTO `db`.`t` (ID,NAME) VALUES (1,'a') "
                    "ON DUPLICATE KEY UPDATE NAME='a';", "EX-N07"),
        ("blk_multi", "UPDATE `db`.`t` JOIN `db`.`t2` ON `t`.ID=`t2`.ID "
                      "SET `t`.NAME='x' WHERE `t`.ID=1;", "EX-U08"),
        ("blk_rename", "ALTER TABLE `db`.`t` RENAME TO t_new;", "EX-A09"),
        ("blk_other", "ALTER TABLE `db`.`t` ALTER COLUMN NAME SET DEFAULT 'z';", "EX-A10"),
    ]:
        o, s, _, _ = run(cid, sql, _facts())
        ok(f"{cid}.blocked", want in o.blocked_by, o.blocked_by)
        ok(f"{cid}.no_session", o.session_calls == 0, o.session_calls)

    # ---- 12. ALTER ADD COLUMN：回滚 DROP COLUMN + 写前落盘顺序
    o, s, store, _ = run(
        "alter_add", "ALTER TABLE `db`.`t` ADD COLUMN C1 INT NOT NULL DEFAULT 0;",
        _facts(columns=["ID", "C_OLD"], primary_key=["ID"]),
        queries=[(r"SHOW CREATE TABLE", [{"Table": "t", "Create Table": DDL_T}]),
                 (r"information_schema\.COLUMNS", [{"c": 1}])])
    ok("alter.executed", o.executed and o.committed)
    ok("alter.rb_sql", o.rollback.sql == ["ALTER TABLE `db`.`t` DROP COLUMN `C1`;"],
       o.rollback.sql)
    ok("alter.note", "已有数据" in (o.rollback.note or ""), o.rollback.note)
    ok("alter.verify", o.results[0].verify == "ok", o.results[0].verify)
    # 写前落盘：业务语句下发那一刻 undo 文件里已经有一条记录
    ok("alter.write_ahead", s.probe_at_exec == 1, s.probe_at_exec)

    # ---- 13. ALTER DROP COLUMN：结构可重建、数据只能靠备份
    o, _, _, _ = run(
        "alter_drop", "ALTER TABLE `db`.`t` DROP COLUMN C_OLD;",
        _facts(columns=["ID", "C_OLD"], primary_key=["ID"]),
        queries=[(r"SHOW CREATE TABLE", [{"Table": "t", "Create Table": DDL_T}])])
    ok("alter.drop_sql", o.rollback.sql ==
       ["ALTER TABLE `db`.`t` ADD COLUMN `C_OLD` varchar(32) NOT NULL DEFAULT '';"],
       o.rollback.sql)
    ok("alter.drop_note", "备份" in (o.rollback.note or ""), o.rollback.note)

    # ---- 14. CREATE INDEX：回滚 DROP INDEX
    o, _, _, _ = run(
        "ix_ok", "CREATE INDEX IDX_C1 ON `db`.`t` (C1);",
        _facts(columns=["ID", "C1"], primary_key=["ID"]))
    ok("ix.rb_sql", o.rollback.sql == ["DROP INDEX `IDX_C1` ON `db`.`t`;"], o.rollback.sql)
    ok("ix.executed", o.executed)

    # ---- 15. CREATE TABLE：回滚 DROP TABLE
    o, _, _, _ = run(
        "ct_ok", "CREATE TABLE `db`.`t_new` (ID BIGINT NOT NULL, PRIMARY KEY (ID)) ENGINE=InnoDB;",
        _facts(table={"exists": False, "rows": None, "size_gb": None, "engine": None}))
    ok("ct.rb_sql", o.rollback.sql == ["DROP TABLE `db`.`t_new`;"], o.rollback.sql)

    # ---- 16. dry_run=plan 不执行
    o, s, _, _ = run("dry_plan", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
                     dry_run=EX.DRY_PLAN)
    ok("dry.plan_no_exec", not o.executed and o.session_calls == 0)
    ok("dry.plan_reason", "plan" in (o.refused_reason or ""), o.refused_reason)

    # ---- 17. dry_run=rollback 拒 DDL
    o, _, _, _ = run("dry_ddl", "CREATE INDEX IDX_C1 ON `db`.`t` (C1);",
                     _facts(columns=["ID", "C1"]), dry_run=EX.DRY_ROLLBACK)
    ok("dry.ddl_refused", "DRY_RUN_DDL" in o.blocked_by, o.blocked_by)

    # ---- 18. dry_run=rollback 跑 DML 后回滚，affected_rows 仍是真实值
    o, s, _, _ = run(
        "dry_dml", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
        queries=[(r"FOR UPDATE", [{"ID": 1, "NAME": "a"}])],
        rowcounts=[(r"^UPDATE", 1)], dry_run=EX.DRY_ROLLBACK)
    ok("dry.dml_rolled_back", not o.executed and s.rolled_back == 1)
    ok("dry.dml_real_rows", o.results[0].affected_rows == 1, o.results[0].affected_rows)
    ok("dry.dml_rb_available", o.rollback.available)

    # ---- 19. sha256 不一致 → 再校验拦下
    good = EX.sha256_of("UPDATE `db`.`t` SET NAME='x' WHERE ID=1;")
    o, _, _, _ = run("sha_bad", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
                     sha="0" * 64)
    ok("sha.mismatch_blocked", "EXEC_REVALIDATE" in o.blocked_by, o.blocked_by)
    o, _, _, _ = run("sha_ok", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
                     sha=good, queries=[(r"FOR UPDATE", [{"ID": 1, "NAME": "a"}])])
    ok("sha.match_ok", o.executed, o.blocked_by)

    # ---- 20. 判定不过时不建会话
    o, _, _, _ = run("blk_nowhere", "UPDATE `db`.`t` SET NAME='x';", _facts())
    ok("blk.nowhere", "EX-U03" in o.blocked_by, o.blocked_by)
    ok("blk.nowhere_no_session", o.session_calls == 0, o.session_calls)

    # ---- 21. 执行报错 → 回滚、不提交
    o, s, _, _ = run(
        "exec_err", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
        queries=[(r"FOR UPDATE", [{"ID": 1, "NAME": "a"}])],
        fail_on=[r"^UPDATE `db`"])
    ok("err.status", o.results[0].status == EX.S_ERROR, o.results[0].status)
    ok("err.rolled_back", not o.executed and s.rolled_back == 1)
    ok("err.message", any("模拟数据库报错" in e for e in o.errors), o.errors)

    # ---- 22. 审计落盘与取回
    rec = store.load("nope")
    ok("store.missing", rec is None)
    ok("store.traversal", not ExecutionStore.valid_id("../etc/passwd"))
    o, _, store2, _ = run(
        "store_ok", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
        queries=[(r"FOR UPDATE", [{"ID": 1, "NAME": "a"}])])
    saved = store2.load(o.execution_id)
    ok("store.saved", saved is not None and saved["execution_id"] == o.execution_id)
    ok("store.has_rollback", bool(saved and saved["rollback"]["available"]))
    ok("store.undo_digest", bool(saved and saved["undo"].get("sha256")))
    ok("store.audit_file", any(store2.dir.glob("execution-*.jsonl")))

    # ---- 23. 建会话失败 → 拦在 CONNECT，不留空 undo 文件
    o, _, store3, _ = run(
        "conn_fail", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
        factory_fail=True)
    ok("conn.blocked", o.blocked_by == ["CONNECT"], o.blocked_by)
    ok("conn.no_undo_file", not list(store3.dir.glob("undo-*")), list(store3.dir.iterdir()))
    ok("conn.not_executed", not o.executed)

    # ---- 24. 多语句批次：回滚按语句号分开，不能都串到第一条
    o, _, _, _ = run(
        "batch_ins",
        "INSERT INTO `db`.`t` (ID, NAME) VALUES (1,'a');"
        "INSERT INTO `db`.`t` (ID, NAME) VALUES (2,'b'),(3,'c');",
        _facts(auto_increment=["ID"]),
        rowcounts=[(r"VALUES \(1,'a'\)", 1), (r"^INSERT", 2)])
    ok("batch.executed", o.executed)
    ok("batch.two_plans", len(o.rollback.sql) == 2, o.rollback.sql)
    ok("batch.first", o.rollback.sql[:1] == ["DELETE FROM `db`.`t` WHERE `ID` IN (1);"],
       o.rollback.sql[:1])
    ok("batch.second", o.rollback.sql[1:2] == ["DELETE FROM `db`.`t` WHERE `ID` IN (2, 3);"],
       o.rollback.sql[1:2])

    # ---- 25. 环境级 allowed_types 必须生效（EX-G01 取环境配置，不是全局 scope）
    import copy
    pol = load_policy(str(standard_path))
    base = run_preflight(sql="UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", env="dev",
                         database="db", policy=pol, provider=StubProvider(_facts()),
                         env_cfg=pol.env_cfg("dev"))
    ok("exg01.baseline_ready", base.decision == "READY", base.blocked_by)
    narrowed = copy.deepcopy(pol)
    narrowed.environments["dev"] = dict(narrowed.environments["dev"],
                                        allowed_types=["create_table"])
    r = run_preflight(sql="UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", env="dev",
                      database="db", policy=narrowed, provider=StubProvider(_facts()),
                      env_cfg=narrowed.env_cfg("dev"))
    ok("exg01.env_level", "EX-G01" in r.blocked_by, r.blocked_by)

    # ---- 26. 执行报错整批回滚 → 回滚 SQL 必须为空（撤一个没落地的变更会误伤）
    o, s, _, _ = run(
        "rb_err", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
        queries=[(r"FOR UPDATE", [{"ID": 1, "NAME": "a"}])],
        fail_on=[r"^UPDATE"])
    ok("rb.err_not_executed", not o.executed and not o.committed)
    ok("rb.err_unavailable", not o.rollback.available, o.rollback.available)
    ok("rb.err_no_sql", o.rollback.sql == [], o.rollback.sql)
    ok("rb.err_note", "无需回滚" in (o.rollback.note or ""), o.rollback.note)

    # ---- 27. 行数超限回滚 → 同上，不能给出回滚语句
    o, _, _, _ = run(
        "rb_guard", "UPDATE `db`.`t` SET NAME='x' WHERE ID>0;",
        _facts(count_rows=5), max_rows=10,
        queries=[(r"FOR UPDATE", [{"ID": i, "NAME": "a"} for i in range(5)])],
        rowcounts=[(r"^UPDATE", 12)])
    ok("rb.guard_unavailable", not o.rollback.available, o.rollback.available)
    ok("rb.guard_no_sql", o.rollback.sql == [], o.rollback.sql)

    # ---- 28. DDL 批部分成功：只回滚成功的那条，未执行的不许出现在回滚里
    # information_schema.COLUMNS 故意不给命中：模拟「第 2 条既报错、事后也探测不到」，
    # 否则会走 _verify_ddl_fresh 的「连接异常但对象已生效」分支，用例就测不到失败路径。
    o, _, _, _ = run(
        "ddl_partial",
        "ALTER TABLE `db`.`t` ADD COLUMN C1 INT NOT NULL DEFAULT 0;"
        "ALTER TABLE `db`.`t` ADD COLUMN C2 INT NOT NULL DEFAULT 0;",
        _facts(columns=["ID"], primary_key=["ID"]),
        queries=[(r"SHOW CREATE TABLE", [{"Table": "t", "Create Table": DDL_T}])],
        fail_on=[r"C2"])
    ok("ddl_partial.first_ok", o.results[0].status == EX.S_OK, o.results[0].status)
    ok("ddl_partial.second_err", o.results[1].status == EX.S_ERROR, o.results[1].status)
    ok("ddl_partial.only_first", o.rollback.sql == ["ALTER TABLE `db`.`t` DROP COLUMN `C1`;"],
       o.rollback.sql)

    # ---- 29. 主键是函数调用的 INSERT：回滚必须报不可用，不能给个删不到东西的 DELETE
    o, _, _, _ = run(
        "ins_expr_pk", "INSERT INTO `db`.`t` (ID, NAME) VALUES (UUID(),'a');", _facts())
    ok("ins.expr_unavailable", not o.rollback.available, o.rollback.available)
    ok("ins.expr_no_sql", o.rollback.sql == [], o.rollback.sql)
    ok("ins.expr_note", "不是字面量" in (o.rollback.note or ""), o.rollback.note)
    ok("ins.expr_stmt_note", any("不是字面量" in n for n in o.results[0].notes),
       o.results[0].notes)

    # ---- 30. undo 建不起来 → 拒绝执行，且执行会话必须关掉（否则连接泄漏）
    pol30 = load_policy(str(standard_path))
    d30 = tmp_root / "undo_init_fail"
    d30.mkdir(parents=True, exist_ok=True)
    store30 = ExecutionStore(d30)
    store30.open_undo = lambda eid: (_ for _ in ()).throw(OSError("模拟磁盘写满"))
    sess30 = ProbeSession([], [(r"^UPDATE", 1)])
    closed = {"n": 0}
    _close30 = sess30.close

    def _counting_close():
        closed["n"] += 1
        _close30()

    sess30.close = _counting_close
    o30 = EX.execute(sql="UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", env="dev",
                     database="db", policy=pol30, provider=StubProvider(_facts()),
                     session_factory=lambda: sess30, env_cfg=pol30.env_cfg("dev"),
                     store=store30)
    ok("undo.blocked", o30.blocked_by == ["UNDO_INIT"], o30.blocked_by)
    ok("undo.session_closed", closed["n"] == 1, closed["n"])
    ok("undo.not_executed", not o30.executed and not sess30.find(r"^UPDATE"),
       sess30.executed())

    # ---- 31. 标识符转义（库名/表名带反引号时不能拼坏语句）
    ok("rb.q_escape", RB.q("a`b") == "`a``b`", RB.q("a`b"))
    ok("rb.qname", RB.qname("d`1", "t") == "`d``1`.`t`", RB.qname("d`1", "t"))

    # ---- 32. 事实全取不到（库连不上 / 环境没配连接）必须 fail-closed，不能抛异常
    from sql_executor_mcp.backend import Provider as _NullProvider
    pol32 = load_policy(str(standard_path))
    r32 = run_preflight(sql="UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", env="dev",
                        database="db", policy=pol32, provider=_NullProvider(),
                        env_cfg=pol32.env_cfg("dev"))
    ok("nullprov.blocked", r32.decision == "BLOCKED", r32.decision)
    ok("nullprov.has_block", bool(r32.blocked_by), r32.blocked_by)

    # ---- 33. 环境未开放时必须给出 EX-G02（不能在采事实时崩掉）
    pol33 = copy.deepcopy(pol32)
    pol33.environments["dev"] = {"enabled": False, "allowed_types": [], "window": None}
    r33 = run_preflight(sql="UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", env="dev",
                        database="db", policy=pol33, provider=_NullProvider(),
                        env_cfg=pol33.env_cfg("dev"))
    ok("disabled.env_g02", "EX-G02" in r33.blocked_by, r33.blocked_by)

    # ---- 34. 多行 INSERT 的 verify 必须按「全部主键」核对
    # 只取第一行拼 WHERE 再跟总行数比，会永远报 "expected 2, found 1"（2026-11-04 实测踩到）
    o, _, _, _ = run(
        "ins_verify", "INSERT INTO `db`.`t` (ID, NAME) VALUES (1,'a'),(2,'b');",
        _facts(auto_increment=["ID"]), rowcounts=[(r"^INSERT", 2)],
        queries=[(r"COUNT\(\*\).*IN \(1, 2\)", [{"c": 2}])])
    ok("ins.verify_multi_ok", o.results[0].verify == "ok", o.results[0].verify)

    o, _, _, _ = run(
        "ins_verify_bad", "INSERT INTO `db`.`t` (ID, NAME) VALUES (1,'a'),(2,'b');",
        _facts(auto_increment=["ID"]), rowcounts=[(r"^INSERT", 2)],
        queries=[(r"COUNT\(\*\).*IN \(1, 2\)", [{"c": 1}])])
    ok("ins.verify_multi_bad", "expected 2" in (o.results[0].verify or ""), o.results[0].verify)

    # ---- 35. 入参非法（dry_run 取值错）必须给 ARG_INVALID，不能只填 errors
    o, _, _, _ = run("arg_invalid", "UPDATE `db`.`t` SET NAME='x' WHERE ID=1;", _facts(),
                     dry_run="nope")
    ok("arg.invalid_blocked", o.blocked_by == ["ARG_INVALID"], o.blocked_by)
    ok("arg.invalid_reason", "dry_run 只能是" in (o.refused_reason or ""), o.refused_reason)
    ok("arg.invalid_no_session", o.session_calls == 0, o.session_calls)

    # ---- 36. MODIFY 不存在的列：A03 该拦，A08（COPY 耗时）不该跟着报警
    pol36 = load_policy(str(standard_path))
    r36 = run_preflight(sql="ALTER TABLE `db`.`t` MODIFY COLUMN NOPE INT;", env="dev",
                        database="db", policy=pol36,
                        provider=StubProvider(_facts(columns=["ID", "NAME"])),
                        env_cfg=pol36.env_cfg("dev"))
    ok("a03.missing_col", "EX-A03" in r36.blocked_by, r36.blocked_by)
    ok("a08.skip_when_col_missing", "EX-A08" not in r36.warnings, r36.warnings)

    return out


# ---------------------------------------------------------------- main

def _print_group(title: str, results: list[tuple[str, bool, str]], tail: str = "") -> list:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    fails = []
    for name, good, detail in results:
        if good:
            print(f"{GREEN}PASS{RESET}  {name}")
        else:
            print(f"{RED}FAIL{RESET}  {name}  {DIM}{detail}{RESET}")
            fails.append(name)
    print(f"\n{tail or title}：{len(results) - len(fails)}/{len(results)} 通过")
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sql-executor-mcp 自检")
    ap.add_argument("--standard", "--policy", dest="standard",
                    default=str(ROOT / "standards" / "executor.md"),
                    help="标准（规则）文件，默认 standards/executor.md")
    ap.add_argument("--cases", default=str(ROOT / "tests" / "cases" / "basic.yaml"))
    args = ap.parse_args(argv)
    standard_path = Path(args.standard)

    # Windows 控制台默认 GBK，中文与符号会报 UnicodeEncodeError；统一按 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    try:
        policy = load_policy(str(standard_path))
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}标准文件加载失败：{type(exc).__name__}: {exc}{RESET}")
        return 2

    print(f"标准文件：{standard_path}")
    print(f"  标准版本 {policy.standard_version or '-'} / 规则版本 {policy.version} / "
          f"引擎 {policy.engine} / 模式 {policy.mode}")
    print(f"  规则 {len(policy.checks)} 条（block {policy.counts()['block_count']} / "
          f"warn {policy.counts()['warn_count']}），sha256 {policy.sha256[:16]}…")

    s_res = check_standard(standard_path)
    s_fail = _print_group("标准文件校验", s_res)

    p_res = check_parser()
    p_fail = _print_group("解析器断言", p_res)

    g_res = check_privileges()
    g_fail = _print_group("权限解析（SHOW GRANTS）", g_res)

    try:
        passed, failed, failures = run_cases(standard_path, Path(args.cases))
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}用例集加载失败：{type(exc).__name__}: {exc}{RESET}")
        return 2

    total = passed + failed
    print(f"\n{'=' * 78}")
    print(f"用例：{passed}/{total} 通过" + (f"，{failed} 条失败" if failed else ""))
    if failures:
        print(f"\n{YELLOW}失败清单：{RESET}")
        for f in failures:
            print(f"  - {f}")
    print(f"{'=' * 78}")

    # ---- 执行路径
    tmp_root = Path(tempfile.mkdtemp(prefix="exec-selfcheck-"))
    try:
        e_res = check_executor(tmp_root, standard_path)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"\n{RED}执行路径自检崩溃：{type(exc).__name__}: {exc}{RESET}")
        traceback.print_exc()
        return 2
    e_fail = _print_group("执行路径（离线，ScriptedSession）", e_res)

    ok = not s_fail and not p_fail and not g_fail and failed == 0 and not e_fail
    print(f"\n{'=' * 78}")
    print("[OK] 全部通过" if ok else "[FAIL] 存在失败项")
    print(f"{'=' * 78}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
