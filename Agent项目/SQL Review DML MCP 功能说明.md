# SQL Review DML MCP 功能说明

> 技术标识 `sql-review-dml-mcp` · 版本 v1.0.1 · 正式机 <服务器IP> · 端口 8030 · 网关注册名 `SQL Review DML`

## 1. 一句话定位

研发提交工单之前，让 Agent 先把 **DML（INSERT / UPDATE）** 按 DBA 口径过一遍：**规范符合性 + 风险判定 + 整改建议**。

它**只审核、不执行、不写库**：不填工单、不接工单系统、不做审批流转。DML 的落库执行由独立的 SQL Executor 服务负责。

## 2. 服务边界（做什么 / 不做什么）

- 只审 `INSERT` / `UPDATE`（DML 侧核心）
- **只报错不代劳**：`sql_final` 恒等于 `sql_input`——DML 不做任何自动改写，只指出问题
- `DELETE` / `REPLACE INTO` 一律驳回（DM-12 fatal），不在本服务放行范围
- 只读 dev / test / stage 三环境元数据（与 `dba-mcp` 同一套 `<只读账号>`），用于判定依赖事实的规则
- 查不到事实时明确标 ⏸ 未判定，**不猜、不跳过**
- 收到的 DDL（CREATE/ALTER/DROP 等）会被拒绝并提示走 `SQL Review DDL` 服务；SELECT 提示走 `dba-mcp`

## 3. 功能清单（4 个工具）

| 工具 | 功能 |
| --- | --- |
| `review_dml` | **核心**：审核 `INSERT` / `UPDATE`（只报错不改写）；`DELETE` / `REPLACE` 一律驳回 |
| `get_table_structure` | 查目标表结构、索引、行数、容量（dev/test/stage 只读） |
| `check_table_conflict` | 提交前查重：同名表 / 相似表 |
| `get_rules` | 输出 DML 规则清单（含 2 条允许项）＋ 反问话术；`scope=dml\|all` |

## 4. 判定结论（三态，DML 永不出现 `fix_required`）

| verdict | 含义 |
| --- | --- |
| `pass` | ✅ 全部符合规范，可以提交 |
| `need_more_info` | ⏸ 缺表结构等事实，或存在必须人工整改的项（见 `missing_facts` / `findings`） |
| `rejected` | ❌ 本单不通过（不可自动修复的违规，如 `DELETE` / `REPLACE`） |

未放行的结论均携带 `reject{code, stage, text, evidence}`，可直接统计拒绝原因。

## 5. 设计要点

### 5.1 规则全文档化（改规则不改代码、不重启）

- 判定规则唯一来源：`standards/dml.md`（**22 条规则 + 2 条允许项**，同构骨架：级别 / 说明 / 整改 / 情形文案 / 阈值 / 参数 / 适用范围 / 停用开关）
- 提供 `standards/README.md` 作为改规则指南：调级别（❌/⚠️）、下线规则（`停用：true`）、改阈值（如 `max_insert_rows`）、改话术、加"同类规则"（复用已有事实/阈值，零代码）、改适用范围、写正反样例用例（`check_specs.py` 逐条真跑）
- 服务按文件 mtime **热加载**：改完 `dml.md` 下次调用即生效，不用改代码、不用重启
- 反问话术在 `config/questions.yaml`（实时读取，本服务只披露 DML 规则相关话术）；`standards/mysql.md` 为通用总纲，仅供人读/追溯，不参与判定

### 5.2 只报错不代劳（与 DDL 侧的本质差异）

DML 语义敏感（改写了就可能改变业务逻辑），因此**绝不自动改写**：`sql_final` 恒等于 `sql_input`。服务只给出准确的违规点与整治建议，由研发自己改。这也意味着 DML 结论只有 pass / need_more_info / rejected 三态，不存在 `fix_required`。

### 5.3 事实驱动、事实缺失不猜

依赖真实表信息的规则（列是否存在、索引能否命中 WHERE、写入行数是否超限、目标表是否存在等）通过只读元数据判定；取不到时明确标 `need_more_info` 并列出 `missing_facts`，而不是放行或臆断。

### 5.4 分层安全设计

- **网络**：独立端口 8030，部署在网关之后；systemd 单元 + 部署脚本做路径/端口白名单校验
- **鉴权**：服务准入令牌头 `X-SQL-REVIEW-DML-MCP-TOKEN`，缺失/错误 → HTTP 403；与 DDL 侧各一套，任一泄露不影响另一侧
- **审计**：每行审计只落 SQL 的 `sha256` 与长度，**绝不落明文**；保留 30 天、7 天后压缩；文件名前缀 `audit-dml-`（与 DDL 区分）
- **凭据**：数据库密码一律 `${环境变量}` 引用，由 systemd `EnvironmentFile` 注入，复用 `dba-mcp` 三环境只读账号，不新增凭据

### 5.5 调用方身份留痕（v1.0.1 新增）

- 网关透传的「调用方机器 IP + AgentID」写入每行审计：`client_ip` / `agent_id` / `socket_ip` / `*_source` / `identity_trusted`
- 可追溯"哪台机器、哪个 Agent 调了哪个工具、结论如何"
- **防伪造**：只有直连方在 `identity.trusted_proxies`（默认 `<网关IP>`、回环）内才采信透传头，否则只记直连 IP 并标 `identity_trusted=false`
- **只记日志，不参与任何审核判定**；`identity.record: false` 可退回旧行为

### 5.6 拆分式架构（由 sql-review-mcp v1.4.0 拆分）

判定引擎、规则文档、审计与自检脚本与 v1.4.0 完全一致，只裁剪工具面：DML 侧独立进程/端口/规则文档/审计/令牌，与 DDL 侧**物理隔离**。DML 走 8030、DDL 走 8010，发错服务会被明确拒绝并提示去处，不静默放行。拆分前"DDL 与 DML 不要同批（DDL 会隐式提交打断事务）"只是建议，拆分后成为**物理隔离**（DM-16 规则仍然拦截混批）。

## 6. 典型调用流程

```
研发/工单 SQL（INSERT / UPDATE）
   │
   ▼
Agent 调 review_dml（必要时先 get_table_structure 取表结构）
   │
   ▼
服务解析（dml.py 词法级解析 → S/F/D/B 四类事实；dmfacts.py 事实注册表 + 派生）
   │
   ├─► 查只读元数据补事实（表结构 / 索引 / 行数 / 查重）
   │
   ├─► engine.py 三值逻辑判定 + dml.md 规则
   │
   └─► 聚合三态结论：verdict / findings / missing_facts / reject
   │
   ▼
Agent 按整改意见修改 SQL 再复核 → pass 后提工单 → 执行交由 SQL Executor
```

## 7. 目录结构与关键文件

```
sql-review-dml-mcp/
├── config/     settings.yaml（端口/路径/只读账号/阈值/审计/identity 配置）、
│               deploy.conf（部署期路径唯一来源：SCOPE=dml / PORT=8030 / 令牌变量名）、
│               questions.yaml（反问话术）、rules.override.yaml（本服务不生效，见文件内说明）、
│               STANDARD_VERSION
├── standards/  ★ 规则唯一来源：dml.md（22 条 + 2 允许项）、mysql.md（总纲，不参与判定）、
│               README.md（改规则指南）、CHANGELOG.md
├── src/sql_review_dml/
│   ├── dml.py        ★ DML 词法级解析（INSERT/UPDATE/DELETE/REPLACE → 事实）
│   ├── dmfacts.py    ★ DML 事实注册表（S/F/D/B 四类 + 派生）
│   ├── engine.py     ★ DML 判定表达式引擎（三值逻辑）
│   ├── spec.py       ★ 统一规范解析（md → 规则集）+ 校验 + 热加载 + 模板渲染
│   ├── reviewer.py   多语句分派 + 聚合三态结论
│   ├── metadata.py   dev/test/stage 只读元数据
│   ├── audit.py      ★ 审计：拒绝码表 / 写入 / 30 天留存 / 读取
│   ├── tokens.py / model.py / alter.py / ddlchecks.py   词法与 DDL 解析基础设施（DDL 代码随包保留，本服务不暴露 DDL 工具）
│   └── server.py     ★ 4 个工具 + 令牌闸门 + /healthz
├── scripts/    check_specs.py（规范预检）、selftest.py（引擎自检，DML 侧 25 项）、
│               probe_dml.py（DML 解析产物实测）、audit_selftest.py（审计自检 63 项）、
│               audit_report.py、mcp_client_test.py
└── deploy/     deploy.sh、sql-review-dml-mcp.service
```

## 8. 自检与运行

```bash
PYTHONPATH=src python3 scripts/check_specs.py    # 规范预检 + 样例回归
PYTHONPATH=src python3 scripts/selftest.py       # 引擎自检（期望 25/25）
PYTHONPATH=src python3 scripts/audit_selftest.py # 审计自检（63 项，含身份透传）

# 起服务（HTTP，正式机）
PYTHONPATH=src .venv/bin/python -m sql_review_dml.server --transport http --port 8030
# 体检规范（不启动）
PYTHONPATH=src .venv/bin/python -m sql_review_dml.server --check-standard
# 健康检查
curl -s http://127.0.0.1:8030/healthz   # scope / standard_version / rules_count / rules_version
```

客户端配置（网关后面）：

```json
{"url": "http://<服务器IP>:8030/mcp/",
 "headers": {"X-SQL-REVIEW-DML-MCP-TOKEN": "<部署时生成的令牌>"}}
```

## 9. 配置速查

| 想改什么 | 改哪里 |
| --- | --- |
| 规则级别 / 文案 / 阈值 / 参数 / 停用 / 适用范围 | `standards/dml.md`（改完下次调用即生效） |
| 反问话术 | `config/questions.yaml`（实时读取） |
| 安装目录 / 密钥目录 / 端口 / 令牌变量名 / 备份份数 | `config/deploy.conf` |
| 日志/数据目录、审计留存与压缩、文件名前缀 | `config/settings.yaml` → `paths.*`、`audit.*` |
| 单次语句数上限、SQL 字节上限、日志级别 | `config/settings.yaml` → `limits.*`、`log.level` |
| 写入/影响行数等阈值兜底（文档阈值优先） | `config/settings.yaml` → `thresholds.*` |
| 调用方身份：请求头 / 可信代理 / 是否记录 | `config/settings.yaml` → `identity.*` |

## 10. 已知边界

- 只审 INSERT / UPDATE，不执行、不写库；落库执行请走 SQL Executor（端口 8040）
- `DELETE` / `REPLACE INTO` 一律驳回，不提供改写
- 不判 DDL；建表/变更请走 SQL Review DDL（端口 8010）
- SELECT 类查询不在本服务职能内，请走 `dba-mcp`
- 单次请求 ≤ 20 条语句、SQL ≤ 20000 字节
- 依赖事实的规则在元数据不可达时只能给出 ⏸ 结论，无法自动判定
