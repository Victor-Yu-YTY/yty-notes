# SQL Review DDL（技术标识 `sql-review-ddl-mcp`）v1.0.1

> 由 `sql-review-mcp` **v1.4.0 拆分**而来：判定引擎、规则文档、审计与自检脚本与 v1.4.0 一致，
> 只裁剪了工具面。本包是 **DDL 侧服务**，面向正式机 10.0.0.218（端口 `8010`、独立 `.venv`、
> 复用 `dba-mcp` 三环境只读凭据），**注册在网关上，叫 `SQL Review DDL`**。
>
> 姊妹服务：**SQL Review DML**（`sql-review-dml-mcp`，端口 `8030`）——INSERT / UPDATE 审核。

研发提交工单之前，让 Agent 先把 **DDL** 过一遍 DBA 口径：**规范符合性 + 风险判定 + 整改建议**。

- 不填工单、不接工单系统、不做审批流转、**不执行、不写库**
- 只读 dev / test / stage 三环境元数据（与 `dba-mcp` 同一套只读账号），用于判定"依赖事实"的规则
- 查不到事实时明确标 ⏸ 未判定，**不猜、不跳过**
- **规则全文档化**：级别、说明、整改、情形文案、阈值、参数、适用范围、停用开关、
  反问话术**全部在 `standards/ddl.md` 与 `config/questions.yaml` 里**，改规则不用改代码、不用重启
- 审计：未放行的结论都带 `reject{code,stage,text,evidence}`；只落 SQL 哈希不落明文；
  保留 30 天、7 天后压缩；文件名前缀 `audit-ddl-`（与 DML 侧区分）
- **调用方身份留痕（v1.0.1）**：网关透传的「调用方机器 IP + AgentID」写进每行审计
  （`client_ip` / `agent_id` / `socket_ip` / `*_source` / `identity_trusted`），
  可追溯"哪台机器、哪个 Agent 调了哪个工具"；只记日志，**不参与任何审核判定**

## 1. 与拆分前（v1.4.0 单服务）的差异

| 项 | v1.4.0（单服务 8020） | 本包（DDL 侧 8010） |
| --- | --- | --- |
| 工具数 | 9 | **8**（去掉 `review_dml`） |
| 规范 | `ddl.md` + `dml.md` | **只有 `ddl.md`**（+ 总纲 `mysql.md`） |
| `get_rules` 的 `scope` | `all\|ddl\|dml` | **`ddl`（默认）/ `all`**；传 `dml` 会被明确拒绝并提示去 DML 服务 |
| `/healthz` | 同时暴露 DDL/DML 规则版本 | 只暴露 DDL（`scope: "ddl"`） |
| 令牌请求头 | `X-SQL-REVIEW-MCP-TOKEN` | `X-SQL-REVIEW-DDL-MCP-TOKEN`（与 DML 侧各一套，互不复用） |
| 审计文件前缀 | `audit-` | `audit-ddl-` |
| 审计身份字段 | 只记 `client_ip` | **新增 `agent_id` / `socket_ip` / `*_source` / `identity_trusted`**（见 §1.1） |
| 端口 / 目录 | 8020 / `/opt/mcpservers/sql-review-mcp` | 8010 / `/opt/mcpservers/sql-review-ddl-mcp` |

跨服务提示：DML / `SELECT` 类语句发给本服务会被拒，错误信息会指明「DML 请走 SQL Review DML 服务、
查询请走 dba-mcp」，不会静默放行。

## 1.1 v1.0.1 变更（相对 v1.0.0）

| 项 | 变更 |
| --- | --- |
| **调用方身份落审计** | 网关透传的机器 IP / AgentID 写入每行审计，见 §8 配置项 `identity` |
| 采信规则 | 只有**直连方**在 `identity.trusted_proxies`（默认 `10.0.0.219`、回环）内才采信透传头，否则只记直连 IP 并标 `identity_trusted=false`（防伪造） |
| `/healthz` | 新增 `identity{enabled,agent_id_headers,client_ip_headers,trusted_proxies}`，便于核对网关透传是否生效 |
| 自检 | `audit_selftest.py` 增 7 项身份用例（可信/不可信/清洗/关闭/中间件链路），76 → **83 项** |
| 兼容 | 判定行为、规则、返回结构、拒绝码**完全不变**；`identity.record: false` 可退回只记 IP 的旧行为 |

## 2. 8 个工具

| 工具 | 说明 |
| --- | --- |
| `review_ddl` | **核心**：审核任意 DDL（多语句、自动识别类型、逐条给结论） |
| `review_create_table` | 专精：建表（不合规直接给出改写后的语句） |
| `review_alter_table` | 专精：变更表（自动去 `AFTER/FIRST`、合并同表多语句） |
| `review_index_change` | 专精：`CREATE INDEX` / `DROP INDEX` |
| `review_destructive` | 专精：`DROP TABLE` / `TRUNCATE` / `DROP DATABASE`（一律驳回） |
| `get_table_structure` | 查目标表结构、索引、行数、容量（dev/test/stage 只读） |
| `check_table_conflict` | 提交前查重（同名表 / 相似表） |
| `get_rules` | DDL 规则清单（编号/级别/条款/行号/情形/参数/停用状态）；`scope=ddl\|all` |

## 3. 结论四态

| verdict | 含义 |
| --- | --- |
| `pass` | ✅ 全部符合规范，可以提交 |
| `fix_required` | ⚠️ 已自动改写（见 `sql_final`），可直接提交 |
| `need_more_info` | ⏸ 需补充事实信息，或有必须人工整改的项 |
| `rejected` | ❌ 本单不通过（破坏性操作、CTAS 等） |

## 4. 规则文档（唯一改动点）

| 文件 | 角色 | 参与判定 |
| --- | --- | --- |
| `standards/ddl.md` | DDL 判定细则（35 条规则） | ✅ |
| `standards/mysql.md` | 通用总纲 + 条款对照表 | ❌（人读/追溯） |
| `standards/README.md` | **改规则指南**（加规则/改级别/改阈值/停用/文案） | — |
| `config/questions.yaml` | 反问话术（支持 `{占位符}`，实时读取；本服务只披露 DDL 规则相关话术） | — |

**只改文档能做的事**：调级别（❌/⚠️/✅）、下线规则（`停用：true`）、改阈值、改必备字段/索引清单、
改禁用类型清单、改给研发看的话术、改反问话术、加"同类规则"（复用已有检查，零代码）、改适用范围，
以及给规则写 `**样例**` 正反用例（`check_specs.py` 会逐条真跑）。

## 5. 目录

```
sql-review-ddl-mcp/
├── VERSION / requirements.txt
├── config/     settings.yaml（端口/路径/三环境只读账号/阈值兜底/审计留存/规则来源）、
│               deploy.conf（★部署期路径唯一来源，含 SCOPE=ddl / PORT=8010 / 令牌变量名）、
│               questions.yaml（反问话术）、rules.override.yaml（应急覆盖）、STANDARD_VERSION
├── standards/  ★DDL 规则唯一来源
│   ├── ddl.md      DDL 判定细则（35 条，统一骨架）
│   ├── mysql.md    通用总纲 + 条款对照表（不参与判定）
│   ├── README.md   ★改规则指南
│   └── CHANGELOG.md
├── src/sql_review_ddl/
│   ├── tokens.py / model.py / alter.py / dml.py / dmfacts.py / engine.py
│   │                  词法与解析、DML 事实与表达式引擎（DML 侧代码随包保留，本服务不暴露 DML 工具）
│   ├── spec.py        ★统一规范解析（md → 规则集）+ 校验 + 热加载 + 模板渲染
│   ├── ddlchecks.py   ★DDL 检查原语（30 个：判定 + 改写 + 填数据字段）
│   ├── common.py      结果类型、阈值、ReviewResult
│   ├── metadata.py    dev/test/stage 只读元数据（结构/行数/查重）
│   ├── reviewer.py    多语句分派 + 聚合四态结论
│   ├── audit.py       ★审计：拒绝码表 / 写入 / 30 天留存 / 读取
│   ├── config.py      settings.yaml → Settings
│   └── server.py      ★8 个工具 + 令牌闸门 + /healthz
├── scripts/
│   ├── check_specs.py     ★规范预检（本包只校验 ddl.md + 总纲，缺 dml.md 自动跳过）
│   ├── selftest.py        ★引擎自检（按本包带的规范自动裁剪，DDL 侧 40 项）
│   ├── audit_selftest.py  审计自检（按 server 实际工具面装用例）
│   ├── audit_report.py    审计统计
│   └── mcp_client_test.py MCP 端到端（工具面 + 拒绝场景 + 审计留痕）
└── deploy/   deploy.sh（两个服务共用同一份）、sql-review-ddl-mcp.service
```

## 6. 本地自检（不需要 MCP 依赖、不需要数据库）

```bash
PYTHONPATH=src python3 scripts/check_specs.py      # 规范预检 + 样例回归（退出码 0 = 通过）
PYTHONPATH=src python3 scripts/selftest.py         # 引擎自检（期望 40/40；服务器上有 PyYAML 时 41/41）
PYTHONPATH=src python3 scripts/audit_selftest.py   # 审计自检（83 项，含身份透传，自带 fastmcp 桩）
PYTHONPATH=src python3 -m sql_review_ddl.server --check-standard   # 只体检规范，不启动
```

## 7. 运行

```bash
PYTHONPATH=src .venv/bin/python -m sql_review_ddl.server --transport http --port 8010
PYTHONPATH=src .venv/bin/python -m sql_review_ddl.server --transport stdio    # 本机/客户端直连调试
curl -s http://127.0.0.1:8010/healthz | python3 -m json.tool   # scope / standard_version / rules_count / rules_version
PYTHONPATH=src .venv/bin/python scripts/audit_report.py --since 7d
```

客户端配置（网关后面的服务，令牌通过请求头传）：

```json
{"url": "http://10.0.0.218:8010/mcp/",
 "headers": {"X-SQL-REVIEW-DDL-MCP-TOKEN": "<部署时生成的令牌>"}}
```

## 8. 配置项速查

| 想改什么 | 改哪里 |
| --- | --- |
| 规则级别 / 文案 / 阈值 / 参数 / 停用 / 适用范围 | `standards/ddl.md`（改完下次调用即生效） |
| 反问话术 | `config/questions.yaml`（实时读取） |
| 装到哪个目录、密钥放哪、端口、令牌变量名、备份保留几份 | `config/deploy.conf` |
| 日志/数据/报告目录、审计留存与压缩、文件名前缀 | `config/settings.yaml` → `paths.*`、`audit.*` |
| 单次语句数上限、SQL 字节上限、日志级别 | `config/settings.yaml` → `limits.*`、`log.level` |
| 规模阈值兜底（文档阈值优先） | `config/settings.yaml` → `thresholds.*` |
| 应急：临时改 DDL 规则的级别/参数/停用 | `config/rules.override.yaml` |
| 调用方身份：取哪些请求头 / 可信代理 / 是否记身份 | `config/settings.yaml` → `identity.*` |
