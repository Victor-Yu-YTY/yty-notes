# 变更记录（CHANGELOG）

版本口径：`VERSION` / `__version__` / `/healthz` 的 `version` 三处一致。
端口默认 8040（`settings.yaml`、`/healthz` 一致可查）。

---

## 1.0.3 · 2026-11-04（在 1.160 实测后修复）

| # | 修复 | 影响 | 回归断言 |
|---|---|---|---|
| 1 | **多行 INSERT 的 `verify` 误报**：原来只拿第一行主键拼 `WHERE`，再和总行数比，2 行插入必然报 `expected 2, found 1` | 自检字段误报（数据本身正确） | `ins.verify_multi_ok` / `ins.verify_multi_bad` |
| 2 | **未知环境**（如 `env=uat`）原来被当成"环境已关闭"，给出 `EX-G02` | 现在返回 `blocked_by=["ENV"]` + 明确错误「环境 uat 未在标准的环境表中配置」 | 部署后 MCP 实测 |
| 3 | **入参非法**（如 `dry_run='foo'`）原来 `blocked_by` 与 `refused_reason` 都是空 | 现在 `blocked_by=["ARG_INVALID"]`，原因写进 `refused_reason` | `arg.invalid_blocked` / `arg.invalid_reason` |
| 4 | `ALTER … MODIFY COLUMN <不存在的列>` 会额外报 `warn EX-A08（DDL 走 COPY）` | 列缺失时不再评估耗时（该场景已由 `EX-A03` 拦截） | `a08.skip_when_col_missing` |

自检规模：标准 20 + 解析 34 + 权限解析 8 + 用例 41 + 执行路径 **97**。

## 1.0.2 · 2026-11-04

| # | 修复 | 说明 |
|---|---|---|
| 1 | **环境未配置连接时抛异常** | `env=prod`（配了但未开放）、`env=uat`（不存在）原来返回 `isError: 环境 … 未在 targets.yaml 配置连接信息`。现在降级为"事实取不到"，由判定层 fail-closed 给出结论 |
| 2 | **工具层兜底 `_guard`** | 5 个工具任何未预期异常都转成 `BLOCKED [INTERNAL_ERROR]`，不再把异常抛给客户端 |
| 3 | 断言补充 | 空 Provider 必须 fail-closed、环境关闭必须给 `EX-G02` |

## 1.0.1 · 2026-11-04

| # | 修复 | 说明 |
|---|---|---|
| 1 | **`SHOW GRANTS` 解析错误（致命）** | 原来整行按逗号切，第一段被切成 `"GRANT SELECT"`，导致 `SELECT` 永远判缺失，`EX-G07` 把真实库上的**每一条**语句都拦掉。现在抽成纯函数 `parse_privileges()`，正确处理 `SELECT, INSERT, …`、`ALL PRIVILEGES`、`USAGE`、列级授权、`GRANT PROXY` |
| 2 | 断言补充 | 新增「权限解析」一组 8 条离线断言，专盯"权限名被粘成 `GRANT xxx`"这个坑 |

## 1.0.0 · 2026-11-04（首个 1.160 版本）

- 判定规则从 `config/policy.yaml` 迁到 **`standards/executor.md`**（唯一事实源，mtime 热加载），新增 `spec.py` 负责解析/校验/编译；`engine.py` 只保留求值
- 端口 8040，路径 `/mcp/`，令牌头 `X-SQL-EXECUTOR-MCP-TOKEN`
- 44 条检查项（block 31 / warn 13）+ 16 项阈值 + 4 个环境
- 新增 `standards/README.md`（改规则指南）；部署脚本、systemd unit、部署速查、测试语句等交付物在正式上线时另行提供
- 自检：标准校验 + 解析断言 + 用例集 + 离线执行路径（不需要数据库）
