# CHANGELOG

## v1.0.0 — 2026-11（整理版）

只做**目录整理 + 版本号归一**，功能与服务契约零变化（9 个工具、鉴权、端口、审计、导出全部不变）。

### 变更

- **目录分层**：`scripts/` 按用途拆成 `scripts/checks/`（部署必跑：`precheck.py`、`verify.sh`）与 `scripts/tools/`（人工按需：`agent_sim.py`）；
  `deploy/` 保持部署物集中（`deploy.sh` + `dba-mcp.service`）；`config/` 只放运行期配置。
- **新增** `.gitattributes`（强制 `*.sh`/`*.py`/`*.md` 用 LF，防 Windows 编辑后部署报 `bad interpreter`）与 `OWNERS`（评审责任人）。
- **版本号** `0.2.4` → `1.0.0`（`VERSION`、`__version__`、systemd Description、部署脚本标语同步）。
- `deploy.sh`：复制脚本前先 `rm -rf $DEST/scripts`（旧平铺脚本不再保留），并同步 `precheck.py` / `verify.sh` 的新路径。

### 不变

- `src/dba_mcp/` 全部模块逻辑、`config/environments.yaml` 内容、systemd `ExecStart`、端口 8000、令牌头 `X-DB-READ-MCP-TOKEN`、
  9 个工具签名与返回结构、审计与导出行为 —— 全部原样。
- 现网 `/opt/mcpservers/dba-mcp/` 的 `logs/ cache/ exports/` 运行期目录与数据不受影响。

---

## v0.2.4 — 2026-10（下掉假鉴权）

只做一件事：去掉第二层"用户凭证校验"。监听面、导出能力、工具契约全部不动。

### 移除（Breaking）

- **第二层鉴权整层删除**：`AuthConfig.mode / verify_url / timeout / cache_ttl / scope_template`、
  `HttpTokenAuthProvider` / `StaticTokenAuthProvider` / `NoopAuthProvider` / `CachingAuthProvider` /
  `build_auth_provider()` / `extract_token()` / `has_scope()` / `_gate()` 全部移除。
- **假鉴权服务下线**：`scripts/mock_auth_server.py` 删除；部署脚本
  `systemctl disable --now dba-mcp-mock-auth` 并删除对应 unit 文件；检查 9000 无监听。

### 变更

- `auth.gateway` 子段上提为 `auth` 本层（准入令牌是唯一的鉴权配置）。
- 新增启动期硬校验：`auth.require_token_on_start: true` 且令牌为空 → `ConfigError`，拒绝启动。
- 审计记录新增 `identity` 段（`agent_id` / `agent_name` / `request_id`），仅作追溯锚点。
- `_GatewayTokenMiddleware` 改为常量时间比较 + 头名提示；拒绝审计记录命中的头名与来源 IP。
- `scripts/agent_sim.py` 改为"模拟网关侧调用"：只带 `X-DB-READ-MCP-TOKEN`，
  并新增无令牌/错令牌的 403 反向检查。
- `deploy/deploy.sh` 重写：先清假鉴权 → 部署 → 6 项自检（不过则非 0 退出）。
- 新增 `scripts/verify.sh`（只读复核）。

### 影响面（给调用方看）

| 项 | 变化 |
| --- | --- |
| 网关上游地址 | **不变**（`http://10.0.0.218:8000/mcp/`） |
| 准入令牌 | **不变**（部署脚本沿用 `/etc/dba-mcp/dba-mcp.env` 里的旧值） |
| `AUTH_TOKEN_MISSING` / `AUTH_FAILED` / `AUTH_SCOPE_DENIED` | **不再出现**（改由 HTTP 403 表达） |
| 9 个工具的参数与返回 | **不变**（`export` 字段仍在） |
| 研发直连（旧头名） | **不变**，`X-DBA-MCP-TOKEN` 继续有效 |
| 每次工具调用的外部依赖 | **少一次**：不再打本机 9000 的 mock 鉴权服务 |

## v0.2.3 — 2026-10-22

- 令牌头名改为 `X-DB-READ-MCP-TOKEN`（兼容旧名 `X-DBA-MCP-TOKEN`）。
- `auth.cache_ttl` 归零（逐请求实时校验）。
- 导出文件名前缀改为 `db-read-mcp-`。

## v0.2.2 及更早

- 初始共享部署版：第二层用户凭证校验 + 准入令牌 + 结果导出 + 联调假鉴权（9000）。
