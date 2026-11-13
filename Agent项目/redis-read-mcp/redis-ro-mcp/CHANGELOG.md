# CHANGELOG

## v1.0.0 — 2026-11（可部署版）

首个**可部署**版本：由本机 stdio 草稿（`dba-redis-ro-mcp/redis-ro-mcp-v1.0.0`）改造为
218 上可经网关调用的 HTTP 服务，并按《MCP 编写规约》重排仓库结构。
功能面与只读语义与原草稿一致（**11 个工具不变**）。

### 新增（从草稿到可上线）

- **HTTP 形态**：`--transport http --host --port`，MCP 路径 `/mcp`（端口 8050）；
  stdio 仍可用于本机调试。
- **唯一一层鉴权**：准入令牌 `X-REDIS-RO-MCP-TOKEN`（兼容旧头 `X-REDIS-MCP-TOKEN`），
  缺失/错误 → HTTP 403 且**早于 MCP 协议层**；令牌为空时**拒绝启动**（`require_token_on_start`）。
- **调用方身份透传**（对齐 sql-review-ddl/dml v1.0.1、mongodb-ro-mcp）：
  网关透传 `X-Agent-Id` / `X-Forwarded-For`，解析为
  `agent_id` / `client_ip` / `socket_ip` / `*_source` / `identity_trusted` 落进**每行审计**；
  只有直连方在 `identity.trusted_proxies` 内才采信透传头，否则标 `identity_trusted=false`
  并留 `agent_id_source="untrusted-peer"`（防伪造）。身份**只审计、不参与授权**。
- **审计日志**：`logs/audit-redis-YYYY-MM-DD.jsonl`，保留 30 天、7 天后转 `.gz`；
  每个工具出口、鉴权拒绝（`auth_denied`）、协议层拒绝（`http_error`）、启动（`startup`）
  各写一行；未直接放行的结论都带 `reject{code,stage,text,evidence}`。
  字段沿用同一份 JSONL schema：`database`=`db0`、`collection`=被访问的 key。
- **统一返回信封**：`{ok, source, elapsed_ms, truncated, data}` /
  `{ok:false, error:{code,message,hint}}`；错误码表集中在 `audit.REJECT_CODES`。
- **只读防线模块化**：新增 `redis_guard.py`（命令白名单 21 条 / 写命令黑名单 64 条 /
  Predixy 拒绝清单 6 条 + key、pattern、区间、分数、整数入参校验 + 输出两层截断）。
- **部署物**：`config/deploy.conf`（路径与参数的唯一来源）、`deploy/deploy.sh`
  （邻居回归 → 端口检查 → 备份 → rm+cp → 配置一致性校验 → 密钥沿用/生成 →
  venv → precheck 硬门禁 → 渲染 unit → 5 项自检 + 部署后邻居回归）、
  `deploy/redis-ro-mcp.service`（占位符渲染版，**LF**）。
- **校验脚本二分**：`scripts/checks/`（`precheck.py` 三环境连通/代理版本/只读命令/
  白名单实测，`verify.sh` 令牌三态 + 审计 + 源码静态断言，**部署必跑**）与
  `scripts/tools/`（`selftest.py` 工具面自检、`probe_mcp.py` 协议层探测，**人工按需**）。
- `VERSION` / `__init__.py` / `/healthz.version` 三处版本一致（代码优先读 `VERSION`）。

### 变更（相对草稿）

| 项 | 草稿 | 现在 |
|---|---|---|
| 框架 | `from mcp.server.mcpserver import MCPServer`、`mcp==2.3.0` | `from fastmcp import FastMCP`、`fastmcp==2.11.2` + `mcp==1.10.1`（与同机其它 MCP 同版本线） |
| 配置 | 只有环境变量 `REDIS_HOSTS`（JSON 字符串） | `config/environments.yaml` 运行期配置 + `${VAR}` / `${VAR:-}` 注入；三环境逐项可读 |
| 环境命名 | 内部 `dev/test/mirror` 直接对外 | 对外规范名 `dev/test/stage`（`stage` 实际是内部 `mirror`，`mirror`/`sim` 作别名）；响应回显 `env`/`env_actual`（传别名另附 `env_alias`），审计两个都记 |
| 启动校验 | 首次调用才报"缺 REDIS_HOSTS" | 启动期硬校验：令牌空 / 无环境 / 环境缺 host 或端口非法 → 拒绝启动 |
| 只读防线 | 靠"没有通用入口"+ 自检 grep 写方法 | 显式命令白名单 + 写命令黑名单 + Predixy 拒绝清单，全部走配置（改规则不改代码） |
| 入参校验 | 仅部分钳位（limit/count 用 min/max） | key/pattern/区间/分数/整数统一校验并返回结构化错误码（不再靠 `int()` 抛异常） |
| 输出截断 | 按字符数截断 | 先按字段字符数缩水，再按**字节**兜底（`clamp_bytes`，CJK 不会超限） |
| 运维定位 | 无日志 | 每行审计带 `client_ip` / `agent_id` / `env` / `env_actual` / `key` 指纹 |
| 部署 | 仅 stdio 客户端配置 + 备用 systemd 模板 | `deploy.sh` 一键部署 + precheck 硬门禁 + 自检 + 邻居回归 |
| 换行符 | `.gitattributes` 缺失，`.service` 实测是 CRLF（CR=38） | `.gitattributes` 强制 LF，`*.service` 也在清单内 |

### 不变（只读语义与工具契约）

- 11 个工具名与入参契约不变：`list_environments`、`scan_keys`、`find_keys`、`key_detail`、
  `read_string`、`read_hash`、`read_list`、`read_set`、`read_zset`、`read_zset_by_score`、
  `bigkey_scan`。
- 三环境连接特征不变：Predixy 1.0.5 / 端口 25900 / db0 / 无密码。
- `protocol=2` 与 `lib_name=""` 两个兼容参数**固定保留**（Redis 5 无 `HELLO`、
  Predixy 拒 `CLIENT`）。
- 键枚举一律 `SCAN`（绝不 `KEYS`）；大 string 用 `GETRANGE` 分段读；
  `MEMORY USAGE` 不可用，bigkey 用类型化长度近似。

### 决策记录

- **框架为什么换 FastMCP**：草稿用 `mcp==2.3.0` 的 `MCPServer`，而同机
  dba-mcp / sql-review-* / mongodb-ro-mcp 全部是 `fastmcp==2.11.2` + `mcp==1.10.1`。
  要加 HTTP 形态、ASGI 中间件（令牌闸门 + 身份抓取），统一到这条版本线最省事，
  离线轮子也已经在本机存在。**功能面零变化**。
- **`stage` ↔ `mirror` 为什么用别名而不是硬改名**：MySQL/MongoDB 体系与网关编组
  对外只有 `dev/test/stage`，Agent/Skill 按这三档写；Redis 内部历来叫 `mirror`。
  做法是「规范名 `stage` + `actual: mirror` + 别名 `mirror`/`sim`」：对外契约只有三档，
  内部实例名照旧，两边都不会拼错；响应与审计同时给出 `env` 与 `env_actual`。
- **端口取 8050**：8000=dba-mcp（MySQL）、8010=sql-review-ddl、8020=旧 sql-review、
  8030=sql-review-dml、8040=mongodb-ro，8050 是本服务。
- **为什么不做服务端限流**：网关侧已有超时与配额；本服务把每类返回量都钳住
  （count/members/limit/sample/top/rounds + 字节上限）就够，避免自建一套与被调方重复的限流。
- **为什么保留 `config|get` 在白名单却不单独给工具**：`CONFIG GET <单参数>` 被 Predixy
  允许，运维会用到（如 `maxmemory`），本次先作为"允许命令"登记在配置里，
  端口/白名单核对用；是否封装成独立工具留待下一版按需决定（避免 v1.0.0 就扩工具面）。
- **审计默认不落 key 明文**：与 sql-review-* 的"SQL 只落 sha256"、mongodb-ro 的
  "filter 只落 sha256"同口径；排查时可开 `audit.record_query_text`。

### 待办（不在本版范围）

- 上传包（`.tar.gz`）、部署说明（`部署说明-redis-2.218.md`）、网关对齐文档、
  `skill/redis-ro/SKILL.md` —— 按《MCP 编写规约》§五，文档不随包分发，另行交付。
- 网关侧登记 `redis_ro` 能力 + AgentID 侧登记 `redis_scope` 编组（当前均不存在）。
- 可选：把 `CONFIG GET` 封装成 `config_get` 工具；Redis 6+ 升级后补 ACL 只读用户。
