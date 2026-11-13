# CHANGELOG

## v1.0.0 — 2026-11（可部署版）

首个**可部署**版本：由本机 stdio 草稿改造为 218 上可经网关调用的 HTTP 服务，
并按《MCP 编写规约》重排仓库结构。功能面与只读语义与原草稿一致（13 个工具）。

### 新增（从草稿到可上线）

- **HTTP 形态**：`--transport http --host --port`，MCP 路径 `/mcp`；stdio 仍可用于本机调试。
- **唯一一层鉴权**：准入令牌 `X-MONGODB-RO-MCP-TOKEN`（兼容旧头 `X-MONGODB-MCP-TOKEN`），
  缺失/错误 → HTTP 403 且**早于 MCP 协议层**；令牌为空时**拒绝启动**（`require_token_on_start`）。
- **调用方身份透传**（对齐 sql-review-ddl/dml v1.0.1）：
  网关透传 `X-Agent-Id` / `X-Forwarded-For`，解析为
  `agent_id` / `client_ip` / `socket_ip` / `*_source` / `identity_trusted` 落进**每行审计**；
  只有直连方在 `identity.trusted_proxies` 内才采信透传头，否则标 `identity_trusted=false`
  并留 `agent_id_source="untrusted-peer"`（防伪造）。身份**只审计、不参与授权**。
- **审计日志**：`logs/audit-mongo-YYYY-MM-DD.jsonl`，保留 30 天、7 天后转 `.gz`；
  每个工具出口、鉴权拒绝（`auth_denied`）、协议层拒绝（`http_error`）、启动（`startup`）
  各写一行；未直接放行的结论都带 `reject{code,stage,text,evidence}`。
- **统一返回信封**：`{ok, source, elapsed_ms, truncated, data}` /
  `{ok:false, error:{code,message,hint}}`，错误码表集中维护。
- **部署物**：`config/deploy.conf`（路径与参数的唯一来源）、`deploy/deploy.sh`
  （邻居回归 → 端口检查 → 备份 → rm+cp → 配置一致性校验 → 密钥沿用/生成 →
  venv → precheck 硬门禁 → 渲染 unit → 5 项自检 + 部署后邻居回归）、
  `deploy/mongodb-ro-mcp.service`（占位符渲染版）。
- **校验脚本二分**：`scripts/checks/`（`precheck.py` 三环境连通/版本/账号/只读角色，
  `verify.sh` 令牌三态 + 审计 + 防线断言，**部署必跑**）与
  `scripts/tools/`（`selftest.py` 工具面自检、`probe_mcp.py` 协议层探测，**人工按需**）。
- `VERSION` / `__init__.py` / `/healthz.version` 三处版本一致（代码优先读 `VERSION`）。

### 变更（相对草稿）

| 项 | 草稿 | 现在 |
|---|---|---|
| 框架 | `from mcp.server.mcpserver import MCPServer`、`mcp==2.3.0`（该模块/版本不存在，启动即 `ModuleNotFoundError`） | `from fastmcp import FastMCP`、`fastmcp==2.11.2` + `mcp==1.10.1`（与 db-read-mcp / sql-review-* 同版本线） |
| 配置 | 只有环境变量 `MONGODB_URIS` JSON | `config/environments.yaml` 运行期配置 + `${VAR}` 注入；`uri` 或 `hosts` 两种写法 |
| 环境命名 | 内部 `dev/test/mirror` 直接对外 | 对外规范名 `dev/test/stage`（`stage` 的实际实例是内部 `mirror` 副本集，`mirror`/`sim` 作别名）；响应回显 `env`/`env_actual`（传别名另附 `env_alias`），审计三个都记 |
| 启动校验 | 首次调用才报"缺 MONGODB_URIS" | 启动期硬校验：令牌空 / 无环境 / 环境缺连接信息 → 拒绝启动 |
| 库/集合名 | 不校验 | `IDENT_RE` 字符校验 + `admin/config/local` 默认禁止数据查询 |
| 输出截断 | 按字符数截 `max_bytes//2`（CJK 会超限） | 先按字段字符数缩水，再按**字节**兜底（`clamp_bytes`） |
| 运维定位 | 无日志 | 每行审计带 `client_ip` / `agent_id` / `env` / `env_actual` / `collection` / `query_sha256` |

### 修复（v1.0.0 交付后、首次上机部署时发现）

- `scripts/checks/precheck.py` 报 `TypeError: object of type 'CommandCursor' has no len()`：
  pymongo 4.x 的 `client.list_databases()` 返回 **CommandCursor（惰性游标）**，不能直接 `len()`，
  已改为 `list(client.list_databases())` 物化后再用。**注意这不影响服务本身**（`server.py` 的
  `list_databases` 工具一直是直接迭代游标的写法，没有这个问题），只影响部署前预检脚本。
- `scripts/checks/precheck.py` 增加**阶段定位**：`stage` = `connect` / `isMaster` / `serverInfo` /
  `listDatabases` / `readProbe` / `connectionStatus`，失败时输出"❌ 不可用（停在阶段 xxx）"，不用再猜。
- `deploy/deploy.sh` 步 5 的端口一致性校验误报 `端口不一致：… environments.yaml port=80408000801080208030`：
  原实现 `grep 'port:' | tr -dc '0-9'` 把 `environments.yaml` 行尾注释里的端口号（`# 8000=…、8030=…`）一并拼进了比较值。
  改为 `awk` 只取该行第 2 个字段（值）并去引号，实测在真实配置上返回 `8040`。
- `deploy/deploy.sh` 步 3 备份时序：改为**先备份、后 `mkdir`**，且以 `$DEST/src` 或 `$DEST/VERSION` 是否存在判断"是否已有旧版本"，
  避免首次安装时把刚建出来的空 `config/` 当成旧版本备份、并打印误导性的"当前 VERSION=未知"。

> 只影响部署脚本，运行时代码与服务契约零变化；已部署成功的实例无需重装。

### 不变（只读语义与工具契约）

- 13 个工具名与入参契约不变；`find_documents` / `aggregate` / `count_documents` / `explain`
  仍以 JSON 字符串传 `filter` / `pipeline`。
- 算子黑名单不变：`$out`、`$merge`、`$where`、`$function`、`$accumulator`（递归检查）。
- `limit+1` 溢出探测、单字段截断、`maxTimeMS`、聚合禁落盘等资源限制保留。
- `isMaster`（非 `hello`）、`pymongo==4.6.3`（保 wire version 7）不变。

### 决策记录

- **框架为什么换 FastMCP**：草稿里的 `mcp.server.mcpserver.MCPServer` 与 `mcp==2.3.0`
  在官方发布线上不存在（官方是 `mcp` 1.x + `mcp.server.fastmcp.FastMCP`），
  而同机其它 MCP 全部使用 `fastmcp==2.11.2`，统一到同一条版本线最省事。
- **`stage` ↔ `mirror` 为什么用别名而不是硬改名**：MySQL 体系与网关编组对外只有
  `dev/test/stage`，Agent/Skill 按这三档写；MongoDB 内部历来叫 `mirror`。做法是
  「规范名 `stage` + `actual: mirror` + 别名 `mirror`/`sim`」：对外契约只有三档，
  内部实例名照旧，两边的名字都不会拼错；响应与审计同时给出 `env` 与 `env_actual`。
- **审计默认不落查询明文**：与 sql-review-* 的"SQL 只落 sha256"同口径；
  需要排查时可开 `audit.record_query_text`。
- **端口取 8040**：8000=dba-mcp、8010=sql-review-ddl、8020=旧 sql-review、8030=sql-review-dml。
