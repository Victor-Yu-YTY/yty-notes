# SQL Review DDL MCP 功能说明

> 技术标识 `sql-review-ddl-mcp` · 版本 v1.0.1 · 正式机 <服务器IP> · 端口 8010 · 网关注册名 `SQL Review DDL`

## 1. 一句话定位

研发提交工单之前，让 Agent 先把待执行的 **DDL**（建表 / 变更表 / 索引 / 破坏性操作）按 DBA 口径过一遍，输出**规范符合性 + 风险判定 + 整改建议**。

它**只审核、不执行、不写库**：不填工单、不接工单系统、不做审批流转。DDL 的落库执行由独立的 SQL Executor 服务负责。

## 2. 服务边界（做什么 / 不做什么）

- 只审 DDL：`CREATE TABLE`、`ALTER TABLE`、`CREATE INDEX` / `DROP INDEX`、`DROP TABLE` / `TRUNCATE` / `DROP DATABASE`
- 只读 dev / test / stage 三环境元数据（与 `dba-mcp` 同一套 `<只读账号>`），用于判定依赖事实的规则
- 查不到事实时明确标 ⏸ 未判定（`need_more_info`），**不猜、不跳过**
- 收到的 DML（INSERT/UPDATE/DELETE）会被拒绝并提示走 `SQL Review DML` 服务；SELECT 提示走 `dba-mcp`
- **不分环境，一律按生产标准严格审**

## 3. 功能清单（8 个工具）

| 工具 | 功能 |
| --- | --- |
| `review_ddl` | **核心**：审核任意 DDL，支持多语句、自动识别语句类型、逐条给出结论 |
| `review_create_table` | 专精建表审核；不合规时直接给出改写后的建表语句 |
| `review_alter_table` | 专精变更表审核；自动去 `AFTER/FIRST`、合并同表多条 ALTER |
| `review_index_change` | 专精 `CREATE INDEX` / `DROP INDEX` 审核 |
| `review_destructive` | 专精破坏性操作（`DROP TABLE` / `TRUNCATE` / `DROP DATABASE`），一律驳回 |
| `get_table_structure` | 查目标表结构、索引、行数、容量（dev/test/stage 只读） |
| `check_table_conflict` | 提交前查重：同名表 / 相似表 |
| `get_rules` | 输出 DDL 规则清单（编号/级别/条款/行号/情形/参数/停用状态），`scope=ddl\|all` |

## 4. 判定结论（四态）

| verdict | 含义 |
| --- | --- |
| `pass` | ✅ 全部符合规范，可以提交 |
| `fix_required` | ⚠️ 已自动改写（见 `sql_final`），可直接提交 |
| `need_more_info` | ⏸ 需补充事实信息，或存在必须人工整改的项 |
| `rejected` | ❌ 本单不通过（破坏性操作、CTAS 等不可修复项） |

未放行的结论均携带 `reject{code, stage, text, evidence}`，可直接统计拒绝原因。

## 5. 设计要点

### 5.1 规则全文档化（改规则不改代码、不重启）

- 判定规则唯一来源：`standards/ddl.md`（**35 条**规则，统一骨架：级别 / 说明 / 整改 / 情形文案 / 阈值 / 参数 / 适用范围 / 停用开关）
- 提供 `standards/README.md` 作为改规则指南：调级别（❌/⚠️/✅）、下线规则（`停用：true`）、改阈值、改必备字段/索引清单、改禁用类型清单、改话术、加"同类规则"（复用已有检查，零代码）、写正反样例用例（`check_specs.py` 逐条真跑）
- 服务按文件 mtime **热加载**：改完 `ddl.md` 下次调用即生效，不用改代码、不用重启
- 反问话术在 `config/questions.yaml`（支持 `{占位符}`，实时读取）；`standards/mysql.md` 为通用总纲，仅供人读/追溯，不参与判定

### 5.2 自动改写（fix_required 的价值）

可自动修复的违规不改判驳回，而是直接产出合规改写版（`sql_final`）：如去掉 `AFTER/FIRST`、合并同表多条 ALTER、补五个必备字段与三个必备索引、长 VARCHAR 改前缀索引等。研发拿到 `sql_final` 即可提交。

### 5.3 事实驱动、事实缺失不猜

依赖真实表信息的规则（表是否存在、行数是否达大表标准、索引是否已存在、查重等）通过只读元数据判定；取不到时明确标 `need_more_info` 并列出 `missing_facts`，而不是放行或臆断。

### 5.4 分层安全设计

- **网络**：独立端口 8010，部署在网关之后；systemd 单元 + 部署脚本做路径/端口白名单校验
- **鉴权**：服务准入令牌头 `X-SQL-REVIEW-DDL-MCP-TOKEN`，缺失/错误 → HTTP 403；与 DML 侧各一套，任一泄露不影响另一侧
- **审计**：每行审计只落 SQL 的 `sha256` 与长度，**绝不落明文**；保留 30 天、7 天后压缩；文件名前缀 `audit-ddl-`（与 DML 区分，日志目录合并也不会串）
- **凭据**：数据库密码一律 `${环境变量}` 引用，由 systemd `EnvironmentFile` 注入，复用 `dba-mcp` 三环境只读账号，不新增凭据

### 5.5 调用方身份留痕（v1.0.1 新增）

- 网关透传的「调用方机器 IP + AgentID」写入每行审计：`client_ip` / `agent_id` / `socket_ip` / `*_source` / `identity_trusted`
- 可追溯"哪台机器、哪个 Agent 调了哪个工具、结论如何"
- **防伪造**：只有直连方在 `identity.trusted_proxies`（默认 `<网关IP>`、回环）内才采信透传头，否则只记直连 IP 并标 `identity_trusted=false`
- **只记日志，不参与任何审核判定**；`identity.record: false` 可退回旧行为
- `/healthz` 暴露身份配置摘要，便于核对网关透传是否生效

### 5.6 拆分式架构（由 sql-review-mcp v1.4.0 拆分）

判定引擎、规则文档、审计与自检脚本与 v1.4.0 完全一致，只裁剪工具面：DDL 侧独立进程/端口/规则文档/审计/令牌，与 DML 侧**物理隔离**。DDL 走 8010、DML 走 8030，发错服务会被明确拒绝并提示去处，不静默放行——这从机制上避免了"DDL 隐式提交打断 DML 事务"的混批问题。

## 6. 典型调用流程

```
研发/工单 SQL
   │
   ▼
Agent 调 review_ddl / review_create_table / review_alter_table / review_index_change / review_destructive
   │
   ▼
服务解析（词法 + 语句类型识别）
   │
   ├─► 查只读元数据补事实（get_table_structure / check_table_conflict）
   │
   ├─► ddlchecks.py 规则判定（30 个检查原语：判定 + 改写 + 填数据字段）
   │
   └─► 聚合四态结论：verdict / findings / missing_facts / sql_final / reject
   │
   ▼
Agent 按整改意见修改 SQL 再复核 → pass 后提工单 → 执行交由 SQL Executor
```

## 7. 目录结构与关键文件

```
sql-review-ddl-mcp/
├── config/     settings.yaml（端口/路径/只读账号/阈值/审计/identity 配置）、
│               deploy.conf（部署期路径唯一来源：SCOPE=ddl / PORT=8010 / 令牌变量名）、
│               questions.yaml（反问话术）、rules.override.yaml（应急覆盖）、STANDARD_VERSION
├── standards/  ★ 规则唯一来源：ddl.md（35 条判定细则）、mysql.md（总纲，不参与判定）、
│               README.md（改规则指南）、CHANGELOG.md
├── src/sql_review_ddl/
│   ├── spec.py        ★ 统一规范解析（md → 规则集）+ 校验 + 热加载 + 模板渲染
│   ├── ddlchecks.py   ★ DDL 检查原语（30 个：判定 + 改写 + 填数据字段）
│   ├── reviewer.py    多语句分派 + 聚合四态结论
│   ├── metadata.py    dev/test/stage 只读元数据
│   ├── audit.py       ★ 审计：拒绝码表 / 写入 / 30 天留存 / 读取
│   ├── engine.py      判定表达式引擎
│   ├── tokens.py / model.py / alter.py / dml.py / dmfacts.py  词法与解析基础设施
│   └── server.py      ★ 8 个工具 + 令牌闸门 + /healthz
├── scripts/    check_specs.py（规范预检）、selftest.py（引擎自检，DDL 侧 40 项）、
│               audit_selftest.py（审计自检 83 项）、audit_report.py、mcp_client_test.py
└── deploy/     deploy.sh、sql-review-ddl-mcp.service
```

## 8. 自检与运行

```bash
PYTHONPATH=src python3 scripts/check_specs.py    # 规范预检 + 样例回归
PYTHONPATH=src python3 scripts/selftest.py       # 引擎自检（期望 40/40）
PYTHONPATH=src python3 scripts/audit_selftest.py # 审计自检（83 项，含身份透传）

# 起服务（HTTP，正式机）
PYTHONPATH=src .venv/bin/python -m sql_review_ddl.server --transport http --port 8010
# 体检规范（不启动）
PYTHONPATH=src .venv/bin/python -m sql_review_ddl.server --check-standard
# 健康检查
curl -s http://127.0.0.1:8010/healthz   # scope / standard_version / rules_count / rules_version
```

客户端配置（网关后面）：

```json
{"url": "http://<服务器IP>:8010/mcp/",
 "headers": {"X-SQL-REVIEW-DDL-MCP-TOKEN": "<部署时生成的令牌>"}}
```

## 9. 配置速查

| 想改什么 | 改哪里 |
| --- | --- |
| 规则级别 / 文案 / 阈值 / 参数 / 停用 / 适用范围 | `standards/ddl.md`（改完下次调用即生效） |
| 反问话术 | `config/questions.yaml`（实时读取） |
| 安装目录 / 密钥目录 / 端口 / 令牌变量名 / 备份份数 | `config/deploy.conf` |
| 日志/数据目录、审计留存与压缩、文件名前缀 | `config/settings.yaml` → `paths.*`、`audit.*` |
| 单次语句数上限、SQL 字节上限、日志级别 | `config/settings.yaml` → `limits.*`、`log.level` |
| 规模阈值兜底（文档阈值优先） | `config/settings.yaml` → `thresholds.*` |
| 应急临时改规则 | `config/rules.override.yaml` |
| 调用方身份：请求头 / 可信代理 / 是否记录 | `config/settings.yaml` → `identity.*` |

## 10. 已知边界

- 只审 DDL，不执行、不写库；落库执行请走 SQL Executor（端口 8040）
- 不判 DML；`DELETE` / `REPLACE` 等请走 SQL Review DML（端口 8030）
- SELECT 类查询不在本服务职能内，请走 `dba-mcp`
- 单次请求 ≤ 20 条语句、SQL ≤ 20000 字节（网关侧还有同义校验）
- 依赖事实的规则在元数据不可达时只能给出 ⏸ 结论，无法自动判定
