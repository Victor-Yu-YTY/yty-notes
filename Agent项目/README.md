# MySQL / MongoDB / Redis MCP 工具集（脱敏存档）

六个面向数据库的 MCP（Model Context Protocol）服务实现，按职责分工：**只读查询 → 规范审查 → 写操作执行**。

> 本仓库为**脱敏存档版**：代码结构与规则逻辑完整保留，仅将公司标识、内网地址、
> 账号名、业务库表名替换为 `demo` / `example.com` / `10.0.0.x` 等占位值。
> 因此开箱不可直接连库运行，需自行补全 `config/` 中的真实主机与凭据。

## MySQL 线：查询 → 审查 → 执行

| 目录 | 职责 | 工具面 | 关键设计 |
| --- | --- | --- | --- |
| `dba-read-mcp/` | 只读元数据与查询 | 9 个 | 三层只读防护（补 LIMIT / 语句超时 / 只读事务）、SQLite 元数据缓存、结果导出带签名下载链接 |
| `sql-review-ddl-mcp/` | DDL 提交前审查 | 8 个 | 规则全文档化（`standards/ddl.md` 35 条），改 md 即改规则、不用改代码不用重启；自动改写 + 反问 |
| `sql-review-dml-mcp/` | DML 提交前审查 | 4 个 | 同一引擎的 DML 侧裁剪（`standards/dml.md` 22 条 + 2 允许项），事实读取失败一律标 ⏸ 未判定 |
| `sql-executor-mcp/` | DDL / DML 实际执行 | 5 个 | 解析 → 预检 → 快照 → 执行 → 回滚，执行前落 undo 记录 |

## NoSQL 线：只读摸底

| 目录 | 职责 | 工具面 | 关键设计 |
| --- | --- | --- | --- |
| `mongodb-read-mcp/` | MongoDB 只读查询与巡检 | 13 个 | 算子**递归**黑名单（`$out` / `$merge` 藏在 `$facet`、`$lookup.pipeline` 任意深度也抓得到）、抽样字段结构、副本集与慢操作巡检 |
| `redis-read-mcp/` | Redis 只读排查 | 11 个 | 只读只能落在代码里（Redis 5.0.6 无 ACL、实例无密码），64 条写命令黑名单 + 读命令白名单，`verify.sh` 静态断言不存在 `execute_command` |

## 共同的设计取舍

- **准入令牌 + 身份透传**：`X-*-MCP-TOKEN` 请求头在协议层之前拦截；只有直连方在
  `identity.trusted_proxies` 内才采信 `X-Forwarded-For` / `X-Agent-Id`，防伪造。
- **审计只记事实**：SQL 只落 `sha256` 与长度，永不落明文；拒绝都带
  `reject{code, stage, text, evidence}`，可直接统计拒绝原因。
- **没查到 ≠ 通过**：元数据不可达或检查函数抛异常时落 `⏸ 未判定`，绝不静默放行。
- **规则文档即配置**：`standards/*.md` 是运行时真正读取的规则源，每条规则自带
  正反 `**样例**`，由 `scripts/check_specs.py` 逐条真跑回归。
- **单服务多环境**：一个进程覆盖 `dev` / `test` / `stage`，工具第一个参数一律 `env`；
  对外统一三档，`stage` 在内部映射到 `mirror` 实例，响应回显 `env` 与 `env_actual` 避免误读。
- **配置无密钥**：`config/` 只写 `${VAR}` 引用，真实口令由 systemd `EnvironmentFile`
  注入 `/etc/<服务名>/*.env`（600 root:root），入库代码不含凭据。

## 每个包的离线自检

```bash
# 规范预检 + 样例回归（SQL Review 两包；不连库）
PYTHONPATH=src python scripts/check_specs.py

# 解析器 + 用例集 + 执行路径（executor，不连库）
python scripts/selfcheck.py

# 工具面自检 / 协议层探测（MongoDB / Redis 两包，不连库）
PYTHONPATH=src python scripts/tools/selftest.py --offline
PYTHONPATH=src python scripts/tools/probe_mcp.py
```

## 文档索引

| 服务 | 功能说明 |
| --- | --- |
| `dba-read-mcp` | [线下只读MySQL MCP](dba-read-mcp/线下只读MySQL%20MCP.md) |
| `sql-review-ddl-mcp` | [SQL Review DDL MCP 功能说明](sql-review-ddl-mcp/SQL%20Review%20DDL%20MCP%20功能说明.md) |
| `sql-review-dml-mcp` | [SQL Review DML MCP 功能说明](sql-review-dml-mcp/SQL%20Review%20DML%20MCP%20功能说明.md) |
| `sql-executor-mcp` | [SQL Executor MCP 功能说明](sql-executor-mcp/SQL%20Executor%20MCP%20功能说明.md) |
| `mongodb-read-mcp` | [MongoDB Read MCP 功能说明](mongodb-read-mcp/MongoDB%20Read%20MCP%20功能说明.md) |
| `redis-read-mcp` | [Redis Read MCP 功能说明](redis-read-mcp/Redis%20Read%20MCP%20功能说明.md) |
