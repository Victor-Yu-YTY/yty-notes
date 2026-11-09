# sql-executor-mcp v1.0.3（正式机 10.0.0.218 · 端口 8040）

SQL 执行前置判定与执行服务：进来的 SQL 只回答两件事——**此刻在这个环境能不能执行**、
**执行完怎么退**。判定规则全在 `standards/executor.md`，改规则不用改代码、也不用重启。

> 与 sql-review-mcp 完全解耦：本服务不判规范（命名／类型／注释／索引写法都归审核服务），
> 也不调用审核服务；它只判「落地可行性」并负责落库与回滚预案。

## 本版要点

| 项 | 值 |
|---|---|
| 版本 | `1.0.3` |
| 端口 | **8040**（8000=dba-mcp；8020=sql-review-mcp；8040=本服务） |
| MCP 路径 | `/mcp`，**客户端 URL 必须带结尾斜杠**：`http://<host>:8040/mcp/` |
| 鉴权头 | `X-SQL-EXECUTOR-MCP-TOKEN` |
| 判定标准 | `standards/executor.md`（唯一事实源，按 mtime 热加载） |
| 规则规模 | **44 条**（block 31 / warn 13）+ 16 项阈值 + 4 个环境 |

## 目录

```
config/
  settings.yaml        服务配置（端口 8040 / 路径 / 令牌头 / 日志）
  targets.yaml         dev/test/stage 执行账号连接（库级隔离交给账号 GRANT）
standards/
  executor.md          ★ 判定标准（阈值 + 环境 + 44 条检查项）
  README.md            ★ 改规则指南（加/改/停用规则、表达式语法、改完怎么验）
src/sql_executor_mcp/
  spec.py              标准 md 解析/校验/编译（规则 → 条件树）
  engine.py            求值 + mtime 热加载
  parser.py            5 类语句解析（纯标准库）
  backend.py           MySQLProvider + StubProvider（只读事实）
  facts.py             事实注册表（S/G/O/D 四类，70 余项）
  preflight.py         判定主流程
  session.py           执行会话（MySQLSession + ScriptedSession）
  snapshot.py          SHOW CREATE TABLE 拆成列/索引两本账
  rollback.py          回滚 SQL 生成（只生成、不执行）
  undo.py              undo 日志（逐条 flush + fsync，写前落盘）
  store.py             执行记录与审计流水
  executor.py          执行编排（再校验 + 事务 + 双道行数守卫）
  server.py            MCP 入口（5 个工具 + 两层鉴权 + /healthz）
scripts/
  selfcheck.py         离线自检（不连库）
  probe_targets.py     目标环境账号预检（只读，不写数据）
tests/cases/basic.yaml 判定用例集
logs/                  运行期生成（执行记录 / undo / 审计）
```

## 5 个工具

| 工具 | 说明 | 是否执行 |
|---|---|---|
| `preflight` | 判定，返回 `READY/BLOCKED` + 44 条检查明细 + 事实快照 | 否 |
| `execute` | 判定 + 执行，`dry_run` 三档 | **`dry_run=off` 时写** |
| `preview_rollback` | 判定的同时预演回滚形态（DDL 有完整结论） | 否 |
| `get_execution` | 查历史执行结果、回滚 SQL、undo 明细 | 否 |
| `list_checks` | 列出当前生效的检查项、阈值、环境与标准版本 | 否 |

`/healthz`（无需令牌）返回：`version`、`mode`、`standard_version`、`rules_version`、
`rules_count`、`block_count`、`warn_count`、`standard_sha256`、`backend`、`auth`。

## 判定模型

- **BLOCKED**：任一 `block` 级检查命中
- **READY**：全部 `block` 未命中；`warn` 只作风险提示，不影响结论
- 事实取不到时 **fail-closed**（默认按命中处理），只有显式标 `缺失：skip` 的例外
  （如 `EX-A07` 磁盘空间 —— MySQL 没有内置剩余磁盘查询；`EX-U04` 由 `EX-U05` 专门负责「取不到」）

规则怎么改、加规则怎么写、表达式语法、改完怎么验，**全部见 `standards/README.md`**。

## 开放的写类型（就 5 类，代码层硬编码）

`create_table`、`alter_table`、`create_index`、`insert`、`update`。

`DELETE` / `REPLACE` / `TRUNCATE` / `DROP` / `GRANT` / 视图 / 存储过程 / 触发器 / `LOAD DATA`
一律不在范围（类型白名单 + 首关键字黑名单双层拦截）。

## 执行机制

- **写前落盘**：undo 先写 `logs/undo-<id>.jsonl` 并 `fsync`，**再**下发业务语句
- **事务**：DML 显式事务，任一环节异常或超限 → 整批 `ROLLBACK`；DDL 逐条独立提交、首条失败即停
- **双道行数守卫**：执行前 before-image 行数 + 执行后 `ROW_COUNT()`
- **回滚 SQL 只生成不执行**：执行账号没有 DELETE/DROP 权限，回滚交 DBA 人工执行

| 语句 | 回滚 SQL | 可靠性 |
|---|---|---|
| CREATE TABLE | `DROP TABLE` | 高，若已写入数据会连数据一起删 |
| ALTER ADD COLUMN | `DROP COLUMN` | 高，该列若已有数据会丢失 |
| ALTER MODIFY / CHANGE | 用 `SHOW CREATE TABLE` 快照改回去 | 高 |
| ALTER DROP COLUMN | 用快照 `ADD COLUMN` | 结构可重建，**数据只能从备份恢复** |
| ALTER ADD / DROP INDEX、CREATE INDEX | `DROP INDEX` / 用快照重建 | 高 |
| INSERT（显式主键） | `DELETE … WHERE pk IN (…)` | 高，值取自原文字面量 |
| INSERT（自增主键） | `DELETE … WHERE pk BETWEEN a AND b` | 中，区间不连续时提示人工核对 |
| UPDATE | 逐行反向 `UPDATE` | 高，期间被改第二次会覆盖后一次修改 |

`dry_run` 三档：`off` 真执行 / `plan` 只判定不连库 / `rollback` 仅 DML，事务内真跑再回滚（**DDL 直接拒**）。

## 库级隔离交给账号

`config/targets.yaml` 里**没有库白名单**，代码层也不判库名。库级隔离由执行账号的 GRANT 承担：

```sql
GRANT SELECT, INSERT, UPDATE, CREATE, ALTER, INDEX ON `demo_project`.* TO 'exec_user'@'%';
```

授权必须是 `` ON `具体库`.* ``，**不能 `ON *.*`** —— 给了全局授权，executor 就能写这台实例上任何库，
代码拦不住。所以 `probe_targets.py` 是**必跑项**：

```bash
python scripts/probe_targets.py --env dev    # 只读，不写任何数据
```

## 快速开始（本机）

```bash
pip install -r requirements.txt

# 自检：标准校验 + 解析断言 + 权限解析 + 用例集 + 执行路径，全部离线
python scripts/selfcheck.py

# 起服务（stdio）
PYTHONPATH=src python -m sql_executor_mcp.server

# 起服务（HTTP，本机联调可绑 127.0.0.1 且不配令牌）
PYTHONPATH=src python -m sql_executor_mcp.server --transport http --host 127.0.0.1

# 起服务（对外：必须配令牌，否则拒绝启动）
PYTHONPATH=src EXECUTOR_MCP_TOKEN=<令牌> python -m sql_executor_mcp.server \
  --transport http --host 0.0.0.0 --port 8040

# 健康检查（无需令牌）
curl -s http://127.0.0.1:8040/healthz
```

判定/执行也可以不走 MCP 直接用 CLI 验：

```bash
# 只判定（不执行）
PYTHONPATH=src python -m sql_executor_mcp.server \
  --demo "CREATE TABLE t_new (ID BIGINT NOT NULL PRIMARY KEY) ENGINE=InnoDB" --demo-db demo_project

# 演练：默认 dry_run=plan，需显式 off 才真写
PYTHONPATH=src python -m sql_executor_mcp.server \
  --demo-exec "UPDATE t SET NAME='x' WHERE ID=1" --demo-db demo_project
```

`--demo-exec` 退出码：`0` 已提交、`3` 判定不过、`4` 未提交。

## 已知边界

- **不保证 SQL 经过审核**：与 sql-review-mcp 解耦，不做串联
- **不接受事务控制语句**：`START TRANSACTION` / `COMMIT` 会被判为未开放类型（`EX-G01`），事务由本服务自己开
- **DDL 与 DML 不得同批**；单次 ≤20 条语句、≤20KB
- **日志留存**未内置清理，需要外部（如 logrotate）兜底
