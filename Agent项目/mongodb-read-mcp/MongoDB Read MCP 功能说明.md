# MongoDB Read MCP 功能说明

> 技术标识 `mongodb-read-mcp`（包名 `mongodb-ro-mcp`） · 版本 v1.0.0 · 正式机 `<服务器IP>` · 端口 8040 · 网关注册名 `MongoDB Read`

## 1. 一句话定位

给 Agent 一套**线下三环境 MongoDB 的只读眼睛**：集合数据查询、结构摸底、容量与索引分析、副本集与实例巡检，**13 个工具，不含任何写操作**。

它**只读、不改、不建**：不写文档、不建索引、不改配置。任何写意图都应落到别的服务，本服务在工具面就没有入口。

## 2. 服务边界（做什么 / 不做什么）

- 只读：查询文档、聚合（禁落盘算子）、计数、执行计划
- 只读：库/集合清单、抽样字段结构、索引定义与命中统计、容量
- 只读：运行中操作、实例状态、副本集成员与复制延迟
- **不做**：任何 `insert` / `update` / `delete` / `createIndex` / `drop`，也没有 `execute_command` 这类通用命令执行口子
- **不做**：分片集群视角（仅副本集）
- `admin` / `config` / `local` 三库默认禁止数据查询；巡检三工具走内部代码路径，不开放库名参数
- 目标实例是 **MongoDB 4.0.20（wire version 7）**，因此没有 `hello` 命令、没有 4.2+ 的聚合变量（如 `$$NOW`）；`pymongo` 必须锁 **4.6.x**——4.9+ 要求 wire 8、4.19+ 要求 wire 9，装上直接连不上

## 3. 功能清单（13 个工具）

工具第一个参数一律是 `env`（`dev` / `test` / `stage`），`list_environments` 除外。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `list_environments` | — | 三环境连通性、副本集名、主节点、服务端版本、别名 |
| `list_databases` | `env` | 可见库及占用空间 |
| `list_collections` | `env, database, include_system=false` | 集合列表 + 文档数 / 大小 / 索引数 |
| `collection_schema` | `env, database, collection, sample_size=100` | 抽样字段结构（出现率、类型分布） |
| `list_indexes` | `env, database, collection` | 索引定义 + `$indexStats` 访问次数 |
| `collection_stats` | `env, database, collection` | 文档数、逻辑/物理大小、压缩率 |
| `find_documents` | `env, database, collection, filter, projection, sort, limit, skip` | 条件查询（JSON 字符串传参） |
| `aggregate` | `env, database, collection, pipeline, limit` | 只读聚合（禁 `$out` / `$merge`） |
| `count_documents` | `env, database, collection, filter` | 条件计数 |
| `explain` | `env, database, collection, filter, verbosity` | 执行计划（是否走索引、扫描量） |
| `current_operations` | `env, min_running_ms=1000, limit=20` | 运行中操作按耗时倒序 |
| `server_status` | `env` | 版本、uptime、连接数、内存、opcounters |
| `replica_status` | `env` | 成员角色、健康度、复制延迟 |

统一返回信封，成功与失败两种形态：

```json
{"ok": true, "source": "mongo", "elapsed_ms": 12.3, "truncated": false,
 "data": {"env": "stage", "env_actual": "mirror", "...": "..."}}
```

```json
{"ok": false, "error": {"code": "...", "message": "...", "hint": "..."}}
```

## 4. 只读三层防线

| 层 | 措施 |
| --- | --- |
| 接口层 | 工具面只登记读操作，**没有** `execute_command` / 通用脚本执行入口 |
| 语义层 | 管道与 filter 递归检查全部 `$` 算子，命中即拒：`$out`、`$merge`（落盘写库）、`$where`、`$function`、`$accumulator`（服务端 JS） |
| 资源层 | `maxTimeMS` 30s；单次 ≤100 条；单字段 ≤2000 字符；响应 ≤1 MiB；聚合默认 `allowDiskUse=false`；库名/集合名字符校验；`admin` / `config` / `local` 默认禁止数据查询 |

语义层的关键是**递归而不是字符串匹配**：`$out` 藏在 `$facet`、`$lookup.pipeline` 的任意深度也能被抓到；纯文本匹配会漏。

> **账号权限才是最终边界**：数据库侧必须用只读账号（`read` / `readAnyDatabase`），巡检三工具（`replica_status` / `server_status` / `current_operations`）额外需要 `admin` 库的 `clusterMonitor`（只读监控角色，不含任何写权限）。仅靠 MCP 层拦不住绕过服务端的路径。

## 5. 设计要点

### 5.1 单服务多环境 + 环境别名

一个进程覆盖 `dev` / `test` / `stage`，不必为三个环境起三个服务。对外统一叫 `dev` / `test` / `stage`，其中 **`stage` 的实际实例是内部 `mirror` 副本集**，`mirror` / `sim` 作为别名继续可用——与 MySQL 体系、网关编组、Skill 口径保持一致。

响应回显三个字段避免误读：`env`（恒为规范名）、`env_actual`（实际实例名）、`env_alias`（调用方传了别名时才出现）。审计里同样留痕。

新增环境不用改代码：在 `environments` 下加一个条目即可，环境名可自由扩展。

### 5.2 分层安全设计

- **网络**：独立端口 8040，部署在网关之后；systemd 单元 + 部署脚本做端口/路径白名单校验
- **鉴权**：只有一层准入令牌 `X-MONGODB-RO-MCP-TOKEN`（兼容旧头 `X-MONGODB-MCP-TOKEN`），缺失/错误 → HTTP 403，拦截发生在 MCP 协议层之前；令牌为空时服务**拒绝启动**
- **审计**：JSONL 落 `audit-mongo-YYYY-MM-DD.jsonl`，保留 30 天、7 天后压缩；默认只落 `query_sha256` / `query_len`，不落查询明文
- **凭据**：`config/environments.yaml` 只写 `${VAR}` 引用，真实口令由 `/etc/mongodb-ro-mcp/mongodb-ro-mcp.env`（600 root:root）经 systemd `EnvironmentFile` 注入，配置与代码入库都不含密钥

### 5.3 调用方身份留痕

- 网关透传的「调用方机器 IP + AgentID」写入每行审计：`client_ip` / `agent_id` / `socket_ip` / `identity_trusted`
- 可追溯"哪台机器、哪个 Agent 调了哪个工具、结果如何"
- **防伪造**：只有直连方在 `identity.trusted_proxies`（默认网关 IP、回环）内才采信透传头，否则只记直连 IP 并标 `identity_trusted=false`
- **只记日志，不参与任何授权或查询判定**；`identity.record: false` 可退回旧行为

### 5.4 与 MySQL 体系同构

与 `dba-read-mcp` / `sql-review-*` / `sql-executor-mcp` / `redis-ro-mcp` 共用同一套鉴权、身份透传、审计与部署方式，运维只记一套命令。区别只是数据库客户端与只读防线（`mongo_guard.py` vs SQL 侧的分层校验）。

## 6. 典型调用流程

```
提问（"stage 环境订单集合为什么慢"）
   │
   ▼
Agent 调 list_environments（确认 stage 连通、主节点）
   │
   ├─► list_collections / collection_schema   摸结构与数据量
   ├─► list_indexes / collection_stats        看索引与容量
   ├─► explain（filter）                      看是否走索引、扫描量
   ├─► current_operations / server_status     看实时压力
   └─► find_documents（limit 小、字段裁剪）   看具体样本
   │
   ▼
结论 + 证据（哪个集合、哪个索引缺失、扫描多少文档）
```

## 7. 目录结构与关键文件

```
mongodb-read-mcp/
└── mongodb-ro-mcp/
    ├── src/mongodb_ro_mcp/     代码：只放可被 import 的实现
    │   ├── server.py           ★ 13 个工具 + 令牌闸门 + /healthz + 入口
    │   ├── mongo_guard.py      ★ 只读防线（算子黑名单 / 库白名单 / 输出限额）
    │   ├── config.py           配置加载 + ${VAR} 展开 + 启动期硬校验 + 环境别名
    │   ├── pool.py             连接管理（一环境一客户端）
    │   ├── audit.py            JSONL 审计 + 身份字段 + 留存压缩
    │   ├── identity.py         网关透传身份解析（可信代理防伪造）
    │   └── auth.py             令牌常量时间比较 / 头名选取
    ├── config/                 environments.yaml（运行期，无密钥）、deploy.conf（部署期）
    ├── deploy/                 deploy.sh（一键部署）、mongodb-ro-mcp.service（systemd 模板）
    ├── scripts/checks/         precheck.py（三环境预检）、verify.sh（部署后只读复核）★ 硬门禁
    ├── scripts/tools/          selftest.py（工具面自检）、probe_mcp.py（协议层探测）
    └── README.md  CHANGELOG.md  VERSION  requirements.txt
```

分层判据：`config/` 改了要重启；`deploy/` 只被部署工具消费；`scripts/checks/` 是硬门禁（失败即中止）；`scripts/tools/` 是软告警；`logs/` 是运行期状态，不进包。

## 8. 自检与运行

```bash
# 起服务（唯一入口）
PYTHONPATH=src python -m mongodb_ro_mcp.server --transport http --port 8040

# 健康检查（免令牌，含 identity 配置回显与防线参数）
curl -s http://127.0.0.1:8040/healthz | python3 -m json.tool

# 三环境预检（连通 / 版本 / 账号 / 只读角色）
set -a; . /etc/mongodb-ro-mcp/mongodb-ro-mcp.env; set +a
PYTHONPATH=src python scripts/checks/precheck.py --config config/environments.yaml

# 部署后只读复核（令牌三态 + 审计 + 防线断言）
bash scripts/checks/verify.sh

# 工具面自检 / 协议层探测（不连库也能跑 --offline）
PYTHONPATH=src python scripts/tools/selftest.py --offline
PYTHONPATH=src python scripts/tools/probe_mcp.py
```

客户端配置（网关后面）：

```json
{"url": "http://<服务器IP>:8040/mcp/",
 "headers": {"X-MONGODB-RO-MCP-TOKEN": "<部署时生成的令牌>"}}
```

网关侧必须配置 `forward_headers: ["X-Agent-Id", "X-Forwarded-For"]`——不转发就没有调用方身份，审计里 `agent_id` 恒为空。

## 9. 配置速查

| 想改什么 | 改哪里 |
| --- | --- |
| 三环境地址 / 副本集 / 账号引用 / 别名 | `config/environments.yaml` → `environments.*` |
| 端口 / 鉴权头名 / 输出限额 / `maxTimeMS` | `config/environments.yaml` → `server.*`、`guard.*` |
| 禁用的算子清单 | `config/environments.yaml` → `guard.forbidden_operators` |
| 审计留存、压缩、是否落查询摘要 | `config/environments.yaml` → `audit.*` |
| 可信代理、是否记录身份 | `config/environments.yaml` → `identity.*` |
| 安装目录 / 密钥目录 / 备份份数 | `config/deploy.conf` |

> `config/` 下的改动都是**改完要重启**的；`deploy.conf` 只被 `deploy.sh` 消费。

## 10. 已知边界

- **MongoDB 4.0 差异**：无 `hello`（统一 `isMaster`）、无 `$$NOW` 等 4.2+ 聚合变量；`pymongo` 必须 4.6.x
- **巡检三工具需额外授权**：`replica_status` / `server_status` / `current_operations` 需 `clusterMonitor`，否则返回权限错误
- **不支持分片集群视角**：仅副本集，`sh.status()` 类视角未实现
- **`$accumulator` 一律拒绝**：它虽属只读分组算子，但走服务端 JS 执行，按只读红线统一禁掉；确需放开改 `guard.forbidden_operators`
- **审计默认不落查询明文**：需要摘要时把 `audit.record_query_text` 改 `true`（落 ≤500 字符清洗摘要）
- **未做服务端限流**：网关侧有超时与配额，本服务只有 `maxTimeMS` 与条数上限
- **只读不等于安全**：MCP 层拦不住的路径，靠数据库侧只读角色兜底——账号一定是只读角色
