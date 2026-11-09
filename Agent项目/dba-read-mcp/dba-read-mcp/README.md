# db-read-mcp v1.0.0（只读元数据与查询服务）

线下三环境（`dev` / `test` / `stage`，MySQL 8.0.32）的**只读**元数据与查询 MCP 服务，
9 个操作，**不含任何写操作**。对外经 `mcp-gateway` 提供能力。

> 本版为**整理版**：只做目录分层与版本号归一（`0.2.4` → `1.0.0`），
> **功能与服务契约零变化**：9 个工具、一层准入令牌鉴权、端口、审计、导出全部原样。详见 `CHANGELOG.md`。

## 1. 目录结构

```
db-read-mcp/
├── src/dba_mcp/                代码（10 个模块，逻辑未改）
│   ├── server.py               9 个工具 + 准入令牌中间件 + 导出
│   ├── auth.py                 身份抓取（审计）+ 令牌常量时间比较
│   ├── config.py               启动期硬校验：令牌为空拒绝启动
│   ├── sql_guard.py            只读防线（写语句拦截）
│   ├── pool.py  cache.py  metadata.py  audit.py  export.py
├── config/
│   └── environments.yaml       监听/缓存/审计/导出/三环境连接（项目内配置，**不含密钥**）
├── deploy/
│   ├── deploy.sh               一键部署（清假鉴权 + 部署 + 8 项自检）
│   └── dba-mcp.service         systemd 模板（占位符由 deploy.sh 渲染）
├── scripts/
│   ├── checks/                 部署必跑
│   │   ├── precheck.py         三环境连通 / 账号 / 权限预检
│   │   └── verify.sh           只读复核（假鉴权是否真下掉 + 令牌三态）
│   └── tools/                  人工按需
│       └── agent_sim.py        模拟网关侧调用（9 个工具 + 反向鉴权检查）
├── logs/  cache/  exports/     运行期目录（deploy.sh 创建；不入版本库）
├── README.md  CHANGELOG.md  VERSION  requirements.txt  .gitignore
├── .gitattributes              强制 *.sh/*.py/*.md 用 LF（防部署报 bad interpreter）
└── OWNERS                      评审责任人登记
```

**分层判据**：`config/` 是运行期配置（改了要重启）；`deploy/` 只被部署工具消费；
`scripts/checks/` 跑不过不能上线；`scripts/tools/` 按需执行；`logs/ cache/ exports/` 是运行期状态，不进包。

## 2. 服务契约（本版未变）

| 项 | 值 |
|---|---|
| 端口 / MCP 路径 | `8000` / `/mcp/`（结尾斜杠必须有） |
| 鉴权 | **只有一层**：准入令牌 `X-DB-READ-MCP-TOKEN`（第二层"用户凭证校验"已在 0.2.4 移除） |
| 令牌存放 | `/etc/dba-mcp/dba-mcp.env`（600 root:root）；令牌为空时服务**拒绝启动** |
| `/healthz` | 免令牌，返回 `ok` / `service` / `version` 等 |
| 工具（9 个） | `list_environments`、`list_databases`、`list_tables`、`describe_table`、`get_create_table`、`search_column`、`run_select_query`、`explain_query`、`refresh_metadata` |
| 审计身份 | 记录网关注入的 `X-Agent-Id` / `X-Agent-Name` / `X-Request-Id`，**仅追溯、不参与授权** |

## 3. 快速开始

```bash
pip install -r requirements.txt

# 部署前预检（三环境连通 / 账号 / 权限）
python3 scripts/checks/precheck.py --config config/environments.yaml
python3 scripts/checks/precheck.py --config config/environments.yaml --env test --json

# 起服务
PYTHONPATH=src python3 -m dba_mcp.server                          # stdio
PYTHONPATH=src python3 -m dba_mcp.server --transport http --port 8000

# 部署后复核（只读，出 PASS/FAIL）
bash scripts/checks/verify.sh

# 模拟网关侧调用（9 个工具 + 反向鉴权检查）
PYTHONPATH=src .venv/bin/python scripts/tools/agent_sim.py --auth-only
PYTHONPATH=src .venv/bin/python scripts/tools/agent_sim.py --all
```

## 4. 改什么、改哪里

| 想改 | 改哪里 | 是否重启 |
|---|---|---|
| 端口 / 超时 / 缓存 / 审计 / 导出 | `config/environments.yaml` | 要 |
| 三环境连接（host/user/密码引用） | `config/environments.yaml` 的 `environments` 段 | 要 |
| 只读防线的拦截规则 | `src/dba_mcp/sql_guard.py` | 要 + 必须补断言 |
| 部署目录 / 基础 Python | `deploy/deploy.sh` 顶部变量 | 重跑 `deploy.sh` |

**密钥一律走环境变量**：`/etc/dba-mcp/dba-mcp.env` 由 systemd `EnvironmentFile` 注入，配置里只写 `${VAR}` 引用。

## 5. 整理前后对照（功能等价）

| 整理前（v0.2.4） | 整理后（v1.0.0） | 说明 |
|---|---|---|
| `src/`、`config/` | 同名不变 | 仅 `__init__.py` 的版本号与 `server.py` 文档串里的版本标签变了 |
| `scripts/precheck.py`、`scripts/verify.sh` | `scripts/checks/` | 部署必跑；`precheck.py` 的 `sys.path` 相对层数 +1 |
| `scripts/agent_sim.py` | `scripts/tools/` | 人工按需 |
| `deploy/` | 不变 | `deploy.sh` 只同步 2 处脚本路径，并新增"复制前清空 `$DEST/scripts`"（防旧平铺脚本残留） |
| `logs/ cache/ exports/` | 不变 | 运行期路径未变（由 `deploy.sh` 创建） |
| 无 `.gitattributes` / `OWNERS` | 新增 | 只影响版本库与交付阅读，不影响运行 |
| `VERSION` 0.2.4 | **1.0.0** | 同步 `__version__`、systemd Description、部署标语 |

`ExecStart` 仍是 `-m dba_mcp.server`，端口 8000、令牌头、9 个工具、审计与导出、`/healthz` 字段按原样未变（仅 `version` 显示 1.0.0）。
