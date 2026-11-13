# mongodb-ro-mcp v1.0.0（MongoDB 只读 MCP）

线下三环境 **MongoDB 4.0.20（wire version 7）** 的只读查询 MCP：集合数据查询、
结构摸底、容量与索引分析、副本集与实例巡检，**13 个操作，不含任何写操作**。
对外经 `mcp-gateway` 提供能力，部署在 **10.0.0.218:8040**。

> **单服务多环境**：一个进程覆盖 `dev` 开发 / `test` 测试 / `stage` 模拟，
> 工具第一个参数一律 `env`。
> **对外统一叫 `dev` / `test` / `stage`**，其中 `stage` 的**实际实例是内部 `mirror` 副本集**
> （`mirror` / `sim` 作为别名继续可用）——与 MySQL 体系、网关编组、Skill 口径一致；
> 响应回显 `env`（规范名）、`env_actual`（实际实例），传别名时另附 `env_alias`。

## 1. 目录结构（按《MCP 编写规约》）

```
mongodb-ro-mcp/
├── src/mongodb_ro_mcp/          代码：只放可被 import 的实现
│   ├── server.py                13 个工具 + 准入令牌闸门 + /healthz + 入口
│   ├── config.py                配置加载 + ${VAR} 展开 + 启动期硬校验 + 环境别名
│   ├── identity.py              网关透传身份解析（可信代理防伪造）
│   ├── audit.py                 JSONL 审计 + 身份字段 + 留存压缩
│   ├── auth.py                  令牌常量时间比较 / 头名选取
│   ├── mongo_guard.py           只读防线（算子黑名单 / 库白名单 / 输出限额）
│   ├── pool.py                  连接管理（一环境一客户端）
│   └── __init__.py              版本（优先读 VERSION）
├── config/
│   ├── environments.yaml        运行期配置：监听 / 鉴权 / 防线 / 审计 / 身份 / 三环境（**无密钥**）
│   └── deploy.conf              部署期路径与参数（deploy.sh 的唯一来源）
├── deploy/
│   ├── deploy.sh                一键部署（邻居回归 + 预检硬门禁 + 5 项自检）
│   └── mongodb-ro-mcp.service   systemd 模板（占位符由 deploy.sh 渲染）
├── scripts/
│   ├── checks/                  部署必跑，跑不过不许上线
│   │   ├── precheck.py          三环境连通 / 版本 / 账号 / 只读角色预检
│   │   └── verify.sh            部署后只读复核（令牌三态 + 审计 + 防线断言）
│   └── tools/                   人工按需
│       ├── selftest.py          工具面自检（别名 / 防线 / 输出控制 / 审计字段）
│       └── probe_mcp.py         协议层探测（stdio 起进程走 initialize/tools）
├── logs/                        运行期审计目录（deploy.sh 创建，不入版本库）
├── README.md  CHANGELOG.md  VERSION  requirements.txt
├── .gitattributes  .gitignore  OWNERS
```

**分层判据**：`config/` 改了要重启；`deploy/` 只被部署工具消费；`scripts/checks/` 是
硬门禁（失败即中止）；`scripts/tools/` 是软告警；`logs/` 是运行期状态，不进包。

## 2. 服务契约

| 项 | 值 |
|---|---|
| 端口 / MCP 路径 | `8040` / `/mcp`（客户端 url 必须是 `/mcp/`） |
| 入口 | `python -m mongodb_ro_mcp.server --transport http --port 8040`（唯一入口） |
| 鉴权 | **只有一层**：准入令牌 `X-MONGODB-RO-MCP-TOKEN`（兼容旧头 `X-MONGODB-MCP-TOKEN`），缺失/错误 → HTTP 403，早于 MCP 协议层 |
| 令牌存放 | `/etc/mongodb-ro-mcp/mongodb-ro-mcp.env`（600 root:root）；令牌为空时服务**拒绝启动** |
| `/healthz` | 免令牌，返回 `ok` / `version` / 环境清单 / 鉴权头名 / **identity 配置回显** / 防线参数 |
| 审计 | `<安装目录>/logs/audit-mongo-YYYY-MM-DD.jsonl`，保留 30 天、7 天后压缩 |
| 审计身份 | 记录网关注入的 `X-Agent-Id` / `X-Forwarded-For` → `agent_id` / `client_ip` / `socket_ip` / `identity_trusted`，**仅追溯、不参与授权** |

## 3. 工具清单（13 个）

| 工具 | 参数 | 用途 |
|---|---|---|
| `list_environments` | — | 三环境连通性、副本集名、主节点、服务端版本、别名 |
| `list_databases` | `env` | 可见库及占用空间 |
| `list_collections` | `env, database, include_system=false` | 集合列表 + 文档数 / 大小 / 索引数 |
| `collection_schema` | `env, database, collection, sample_size=100` | 抽样字段结构（出现率、类型分布） |
| `list_indexes` | `env, database, collection` | 索引定义 + `$indexStats` 访问次数 |
| `collection_stats` | `env, database, collection` | 文档数、逻辑/物理大小、压缩率 |
| `find_documents` | `env, database, collection, filter, projection, sort, limit, skip` | 条件查询（JSON 字符串传参） |
| `aggregate` | `env, database, collection, pipeline, limit` | 只读聚合（禁 `$out`/`$merge`） |
| `count_documents` | `env, database, collection, filter` | 条件计数 |
| `explain` | `env, database, collection, filter, verbosity` | 执行计划（是否走索引、扫描量） |
| `current_operations` | `env, min_running_ms=1000, limit=20` | 运行中操作按耗时倒序 |
| `server_status` | `env` | 版本、uptime、连接数、内存、opcounters |
| `replica_status` | `env` | 成员角色、健康度、复制延迟 |

返回统一信封：

```json
{"ok": true, "source": "mongo", "elapsed_ms": 12.3, "truncated": false,
 "data": {"env": "stage", "env_actual": "mirror", "...": "..."}}
```

失败：`{"ok": false, "error": {"code": "...", "message": "...", "hint": "..."}}`

环境回显规则：`env` 恒为**规范名**（`dev`/`test`/`stage`），`env_actual` 是实际副本集环境名
（`dev`/`test`/`mirror`）；调用方传了别名（如 `mirror`）时另附 `env_alias`，审计里也留痕。

## 4. 只读三层 + 资源限额

| 层 | 措施 |
|---|---|
| 接口层 | 工具面只登记读操作，**没有** `execute_command` / 通用脚本执行入口 |
| 语义层 | 管道与 filter 递归检查全部 `$` 算子，命中即拒：`$out`、`$merge`（落盘写库）、`$where`、`$function`、`$accumulator`（服务端 JS）。递归而非字符串匹配——`$out` 藏在 `$facet` / `$lookup.pipeline` 任意深度也抓得到 |
| 资源层 | `maxTimeMS` 30s；单次 ≤100 条；单字段 ≤2000 字符；响应 ≤1 MiB；聚合默认 `allowDiskUse=false`；库名/集合名字符校验；`admin`/`config`/`local` 默认禁止数据查询（巡检三工具走内部代码路径） |

> **账号权限才是最终边界**：数据库侧必须用只读账号（`read` / `readAnyDatabase`），
> 巡检三工具额外需要 admin 库 `clusterMonitor`（只读监控角色，不含任何写权限）。
> 仅靠 MCP 层拦不住绕过服务端的路径。

## 5. 环境注册与密钥

`config/environments.yaml` 只写 `${VAR}` 引用，真实口令由
`/etc/mongodb-ro-mcp/mongodb-ro-mcp.env`（600 root:root）经 systemd `EnvironmentFile` 注入：

```bash
# 三环境各自一套时
MONGO_DEV_USER=ro_user    MONGO_DEV_PASS=...
MONGO_TEST_USER=ro_user   MONGO_TEST_PASS=...
MONGO_STAGE_USER=ro_user  MONGO_STAGE_PASS=...     # stage = 内部 mirror 实例
# 三环境同一套账号时，也可以只给下面两个，并把 yaml 里的 user/password 改成引用它们
MONGO_RO_USER=ro_user     MONGO_RO_PASS=...
# 服务准入令牌（deploy.sh 首次部署自动生成）
GATEWAY_TOKEN=<64 位十六进制>
```

环境条目两种写法（二选一）：

* **写法 A（推荐）**：`hosts` + `replica_set` + `auth_source` + `user` + `password`；
* **写法 B（应急）**：`uri: ${MONGO_URI_DEV}` 直接注入整串。

新增环境不用改代码：加一个 `environments` 条目即可（环境名可自由扩展）。

## 6. 部署（10.0.0.218）

```bash
# ① 包信息核对（上传前在本地算）
sha256sum mongodb-ro-mcp-v1.0.0-2.218.tar.gz

# ② 上传解包到 /tmp（jumpserver：不用 sudo，不用绝对路径 rm -rf）
cd /tmp && tar -xzf mongodb-ro-mcp-v1.0.0-2.218.tar.gz && ls /tmp/mongodb-ro-mcp/

# ③ 部署（root 直跑）
cd /tmp/mongodb-ro-mcp && bash deploy/deploy.sh
#   常用参数：--gen-token / --gateway-token <hex> / --python <path>
#             --no-precheck（验收记录要写原因） / --rebuild-venv / --mongo-user ro_user
```

`deploy.sh` 按 9 步走：**部署前邻居回归**（8000/8010/8030 的 `/healthz`）→ 端口检查
（本服务占用按升级放行、别人占用中止）→ 目录与备份（`$DEST/backup/<时间戳>/`，
只留 5 份）→ **`rm -rf` 后 `cp -r`**（防旧文件残留）→ 配置与两层一致性校验
（端口/审计目录）→ 密钥沿用或生成令牌 → 独立 venv + 依赖探活 →
**precheck 硬门禁** → 渲染 unit 重启；最后 5 项自检（`/healthz` 200、无令牌 403、
错令牌 403、正确令牌 initialize 成功、审计文件已生成）+ **部署后邻居回归**，
任一项不过则以非 0 退出。

## 7. 接网关（MCP Gateway）要点

```json
{
  "mongodb_ro": {
    "display_name": "MongoDB Read",
    "url": "http://10.0.0.218:8040/mcp/",
    "transport": "streamable-http",
    "headers": { "X-MONGODB-RO-MCP-TOKEN": "<与 /etc/mongodb-ro-mcp/mongodb-ro-mcp.env 一致>" },
    "forward_headers": ["X-Agent-Id", "X-Forwarded-For"]
  }
}
```

**`forward_headers` 是身份静默的关键**：不转发 `X-Agent-Id` → 审计里 `agent_id` 恒为空；
不转发 `X-Forwarded-For`（多跳保留最左一跳）→ `client_ip` 退化成网关 IP。
临时凭证（`AgentID-Temporary-Credential`）**不向下游透传**，网关↔上游只用上面的令牌头。

## 8. 验证与日常运维

```bash
DEST=/opt/mcpservers/mongodb-ro-mcp

# ① 健康检查（含 identity 配置回显，确认转发是否生效）
curl -s http://127.0.0.1:8040/healthz | python3 -m json.tool

# ② 只读复核（PASS/FAIL，可直接贴验收记录）
bash $DEST/scripts/checks/verify.sh

# ③ 三环境预检
set -a; . /etc/mongodb-ro-mcp/mongodb-ro-mcp.env; set +a
PYTHONPATH=$DEST/src $DEST/.venv/bin/python $DEST/scripts/checks/precheck.py \
  --config $DEST/config/environments.yaml

# ④ 工具面自检 / 协议层探测（不连库也能跑 --offline）
PYTHONPATH=$DEST/src $DEST/.venv/bin/python $DEST/scripts/tools/selftest.py --offline
PYTHONPATH=$DEST/src $DEST/.venv/bin/python $DEST/scripts/tools/probe_mcp.py

# ⑤ 谁在调（网关透传身份核对）
jq -r '[.ts,.agent_id,.client_ip,.socket_ip,.identity_trusted,.tool,.outcome] | @tsv' \
  $DEST/logs/audit-mongo-$(date +%F).jsonl | tail -10
jq -r 'select(.agent_id!="") | [.agent_id,.client_ip] | @tsv' \
  $DEST/logs/audit-mongo-*.jsonl | sort | uniq -c | sort -rn | head
```

| 动作 | 命令 |
|---|---|
| 状态 / 日志 | `systemctl status mongodb-ro-mcp` ／ `journalctl -u mongodb-ro-mcp -f` |
| 改端口/限额/留存 | 改 `$DEST/config/environments.yaml` → `systemctl restart mongodb-ro-mcp` |
| 依赖损坏 | `bash deploy/deploy.sh --rebuild-venv` |
| 回滚 | `tar xzf $DEST/backup/<时间戳>/…` 或 `cp -a` 回 $DEST 后 restart |
| 停服务 | `systemctl disable --now mongodb-ro-mcp` |

## 9. 已知限制

| 项 | 说明 |
|---|---|
| MongoDB 4.0 差异 | 无 `hello` 命令（统一 `isMaster`）、无 `$$NOW` 等 4.2+ 聚合变量；`pymongo` 必须 4.6.x（4.9+ 要求 wire 8、4.19+ 要求 wire 9，装了直接连不上） |
| 巡检三工具需授权 | `replica_status` / `server_status` / `current_operations` 需 admin 库 `clusterMonitor`：`db.grantRolesToUser("<账号>", [{role:"clusterMonitor", db:"admin"}])` |
| 不支持分片集群视角 | 仅副本集；`sh.status()` 类视角未实现 |
| `$accumulator` 一律拒绝 | 它虽是只读分组算子，但走服务端 JS 执行，按只读红线统一禁掉；需要时改 `guard.forbidden_operators` |
| 审计默认不落查询明文 | 只落 `query_sha256` / `query_len`；要看摘要把 `audit.record_query_text` 改 true（落 ≤500 字符清洗摘要） |
| 未做服务端限流 | 网关侧有超时与配额；本服务只有 `maxTimeMS` 与条数上限 |
