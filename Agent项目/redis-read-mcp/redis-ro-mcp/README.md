# redis-ro-mcp v1.0.0（Redis 只读 MCP）

线下三环境 **Redis 5.0.6（无密码、无 ACL）+ 前置 Predixy 1.0.5 代理**（端口 25900、
db0）的只读查询 MCP：键发现、缓存内容排查、TTL 核对、大键采样扫描，**11 个操作，
不含任何写操作**。对外经 `mcp-gateway` 提供能力，部署在 **10.0.0.218:8050**。

> **单服务多环境**：一个进程覆盖 `dev` 开发 / `test` 测试 / `stage` 模拟，
> 工具第一个参数一律 `env`。
> **对外统一叫 `dev` / `test` / `stage`**，其中 `stage` 的**实际实例是内部 `mirror`**
> （`mirror` / `sim` 作为别名继续可用）——与 MySQL 体系、网关编组、Skill 口径一致；
> 响应回显 `env`（规范名）、`env_actual`（实际实例），传别名时另附 `env_alias`。
>
> 本包与 `mongodb-ro-mcp` / `db-read-mcp` / `sql-review-*` 同构：同一套鉴权、身份透传、
> 审计与部署方式，运维只需要记一套命令。

## 1. 目录结构（按《MCP 编写规约》）

```
redis-ro-mcp/
├── src/redis_ro_mcp/            代码：只放可被 import 的实现
│   ├── server.py                11 个工具 + 准入令牌闸门 + /healthz + 入口
│   ├── config.py                配置加载 + ${VAR} 展开 + 启动期硬校验 + 环境别名
│   ├── identity.py              网关透传身份解析（可信代理防伪造）
│   ├── audit.py                 JSONL 审计 + 身份字段 + 留存压缩
│   ├── auth.py                  令牌常量时间比较 / 头名选取
│   ├── redis_guard.py           只读防线（命令白/黑名单 / 入参校验 / 输出限额）
│   ├── pool.py                  连接管理（一环境一客户端，固定 protocol=2 + lib_name=""）
│   └── __init__.py              版本（优先读 VERSION）
├── config/
│   ├── environments.yaml        运行期配置：监听 / 鉴权 / 防线 / 审计 / 身份 / 三环境（**无密钥**）
│   └── deploy.conf              部署期路径与参数（deploy.sh 的唯一来源）
├── deploy/
│   ├── deploy.sh                一键部署（邻居回归 + 预检硬门禁 + 5 项自检）
│   └── redis-ro-mcp.service     systemd 模板（占位符由 deploy.sh 渲染）
├── scripts/
│   ├── checks/                  部署必跑，跑不过不许上线
│   │   ├── precheck.py          三环境连通 / Predixy 版本 / 只读命令 / 白名单实测
│   │   └── verify.sh            部署后只读复核（令牌三态 + 审计 + 源码静态断言）
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
| 端口 / MCP 路径 | `8050` / `/mcp`（客户端 url 必须是 `/mcp/`） |
| 入口 | `python -m redis_ro_mcp.server --transport http --port 8050`（唯一入口） |
| 鉴权 | **只有一层**：准入令牌 `X-REDIS-RO-MCP-TOKEN`（兼容旧头 `X-REDIS-MCP-TOKEN`），缺失/错误 → HTTP 403，早于 MCP 协议层 |
| 令牌存放 | `/etc/redis-ro-mcp/redis-ro-mcp.env`（600 root:root）；令牌为空时服务**拒绝启动** |
| `/healthz` | 免令牌，返回 `ok` / `version` / 三环境地址与库序号 / 鉴权头名 / **identity 配置回显** / 防线与命令表；**不含任何口令** |
| 审计 | `<安装目录>/logs/audit-redis-YYYY-MM-DD.jsonl`，保留 30 天、7 天后压缩 |
| 审计身份 | 记录网关注入的 `X-Agent-Id` / `X-Forwarded-For` → `agent_id` / `client_ip` / `socket_ip` / `*_source` / `identity_trusted`，**仅追溯、不参与授权** |
| 审计字段映射 | `database` = `db0`（库序号）、`collection` = 被访问的 key（沿用同一份 JSONL schema，便于与其它 MCP 一起用 `jq` 分析） |

## 3. 工具清单（11 个）

| 工具 | 参数 | 用途 |
|---|---|---|
| `list_environments` | — | 三环境连通性、PING、Predixy 版本、SCAN 抽样 |
| `scan_keys` | `env, pattern='*', cursor=0, count=100` | SCAN 游标翻页枚举键，**绝不使用 KEYS** |
| `find_keys` | `env, pattern, limit=50, with_detail=True` | 模式收集一批键 + 类型/TTL/类型化长度 |
| `key_detail` | `env, key` | 存在性、类型、TTL、类型化长度 |
| `read_string` | `env, key, offset=0, length=2000` | `GETRANGE` 分段读，大 value 不拉爆 |
| `read_hash` | `env, key, field=''` | 前 N 个字段（`HLEN`+`HKEYS`+逐字段 `HGET`）或单字段 |
| `read_list` | `env, key, start=0, stop=9` | `LRANGE` 区间读，支持负数下标 |
| `read_set` | `env, key` | 成员（排序、截断）+ 基数 |
| `read_zset` | `env, key, start=0, stop=9, with_scores=True` | 按**排名**区间读 |
| `read_zset_by_score` | `env, key, min_score='-inf', max_score='+inf', limit=50` | 按**分数**区间读（支持 `(5` 开区间） |
| `bigkey_scan` | `env, pattern='*', sample=500, top=20` | 采样 + 类型化长度排序 Top N |

返回统一信封：

```json
{"ok": true, "source": "redis:mirror", "elapsed_ms": 12.3, "truncated": false,
 "data": {"env": "stage", "env_actual": "mirror", "key": "...", "...": "..."}}
```

失败：`{"ok": false, "error": {"code": "...", "message": "...", "hint": "..."}}`

环境回显规则：`env` 恒为**规范名**（`dev`/`test`/`stage`），`env_actual` 是实际环境名
（`dev`/`test`/`mirror`）；调用方传了别名（如 `mirror`）时另附 `env_alias`，审计里也留痕。

## 4. 只读边界：为什么只能落在代码里

Redis 侧与 MySQL/MongoDB 有一个本质差别，必须先讲清楚：

| 条件 | 后果 |
|---|---|
| Redis 5.0.6 | ACL 是 6.0+ 特性，**建不了只读用户** |
| 实例无密码且不许加 | 数据库层**没有任何写拒绝能力** |
| 官方 `redis/mcp-redis` | 无服务端只读开关，写工具直通无密码实例，风险不可接受 |

所以本服务的只读保证**完全由服务端代码承担**，共三层：

| 层 | 措施 |
|---|---|
| 接口层 | 工具面只登记读操作，**没有** `execute` / `eval` / 通用命令执行入口（源码里连 `execute_command` 都没有，`verify.sh` 会断言） |
| 语义层 | 只调用 `allowed_commands` 白名单里的读命令；`forbidden_commands`（64 条写命令）在 `redis_guard.py` 里，同时是静态自检的断言源 |
| 资源层 | SCAN `count` ≤500、成员 ≤100、`find_keys` ≤500、采样 ≤2000、Top N ≤100、SCAN 轮数 ≤60、单字段 ≤2000 字符、响应 ≤1 MiB、socket 超时 5s |

> ⚠️ **最终边界是网络位置 + 网关准入令牌**：任何能直连 `25900` 的人都可以写库。
> 详见 §10「已知限制」第 1 条。

### 4.1 Predixy 白名单（2026-11-10 三环境实测，决定工具面）

| Predixy 允许 | Predixy 拒绝 |
|---|---|
| `PING` / `INFO` / `CONFIG GET`（单参数） | `DBSIZE` |
| `SCAN` / `RANDOMKEY` / `TYPE` / `TTL` / `EXISTS` | `OBJECT` / `MEMORY` |
| `STRLEN` / `GETRANGE` | `SLOWLOG` / `CLIENT`（含 redis-py 的 `CLIENT` 握手） |
| `LRANGE` / `LLEN` / `HGETALL` / `HGET` / `HLEN` / `HKEYS` | `CLUSTER` |
| `SMEMBERS` / `SCARD` / `ZRANGE` / `ZCARD` / `ZRANGEBYSCORE` | |

连带设计决策：

- **bigkey 用类型化长度近似**（string 按字节、集合按元素数），`MEMORY USAGE` 不可用；
- **不提供 slowlog / 键总数工具**，代理拒绝；
- `INFO` 返回的是 **Predixy 自身信息**（Name/Version/RedisMode），后端 Redis 版本经代理不可见；
- 命令被代理拒绝时降级返回 `PROXY_DENIED` + 可用命令提示，不抛堆栈；
- 白名单复测入口：`scripts/checks/precheck.py`（部署硬门禁的一部分）。

## 5. redis-py / Predixy 兼容性（两个硬坑，删掉就连不上）

| 参数 | 值 | 原因 |
|---|---|---|
| `protocol` | **2** | redis-py 8.x 默认发 RESP3 `HELLO` 握手，Redis 5.0.6 不认识，直接 `unknown command 'hello'` |
| `lib_name` | **""** | redis-py 8.x 连接时发 `CLIENT SETINFO`，Predixy 白名单拒绝 `CLIENT`，报 `unknown command 'client'` |

这两项在 `config/environments.yaml` 的**每个环境**下都显式写了（`protocol: 2` / `lib_name: ""`），
`scripts/tools/selftest.py` 会逐环境断言，改错即自检失败。

## 6. 环境注册与密钥

`config/environments.yaml` 只写 `${VAR}` 引用，真实口令由
`/etc/redis-ro-mcp/redis-ro-mcp.env`（600 root:root）经 systemd `EnvironmentFile` 注入：

```bash
# 服务准入令牌（deploy.sh 首次部署自动生成，之后沿用）
GATEWAY_TOKEN=<64 位十六进制>

# 三环境当前 **无密码**，下面三个变量留空即可（配置里写的是 ${VAR:-}，缺失展开成空串）
REDIS_DEV_PASS=
REDIS_TEST_PASS=
REDIS_STAGE_PASS=          # stage = 内部 mirror 实例

# 部署脚本写入的运行时变量
REDIS_MCP_CONFIG=/opt/mcpservers/redis-ro-mcp/config/environments.yaml
REDIS_MCP_TRANSPORT=http
REDIS_MCP_LOG_LEVEL=INFO
```

两点说明：

- `${VAR}` 是**必填**（缺失即拒绝启动），`${VAR:-}` 是**可选**（缺失展开成空串）。
  当前无密码，所以三环境口令都用可选写法；日后某个环境启用 `requirepass`/ACL，
  只要在 env 文件里注入对应变量即可，**配置与代码都不用改**。
- 新增环境不用改代码：在 `environments` 下加一个条目即可（环境名可自由扩展）。

## 7. 部署（10.0.0.218:8050）

```bash
# ① 包信息核对（上传前在本地算）
sha256sum redis-ro-mcp-v1.0.0-2.218.tar.gz

# ② 上传解包到 /tmp（jumpserver：不用 sudo，不用绝对路径 rm -rf）
cd /tmp && tar -xzf redis-ro-mcp-v1.0.0-2.218.tar.gz && ls /tmp/redis-ro-mcp/

# ③ 部署（root 直跑）
cd /tmp/redis-ro-mcp && bash deploy/deploy.sh
#   常用参数：--gen-token / --gateway-token <hex> / --python <path>
#             --no-precheck（验收记录要写原因） / --rebuild-venv
#             --redis-password <pw>（可选：三环境统一口令，当前不需要）
```

`deploy.sh` 按 9 步走：**部署前邻居回归**（8000/8010/8030/8040 的 `/healthz`）→ 端口检查
（本服务占用按升级放行、别人占用中止）→ 目录与备份（`$DEST/backup/<时间戳>/`，
只留 5 份）→ **`rm -rf` 后 `cp -r`**（防旧文件残留）→ 配置与两层一致性校验
（端口/审计目录）→ 密钥沿用或生成令牌 → 独立 venv + 依赖探活 →
**precheck 硬门禁** → 渲染 unit 重启；最后 5 项自检（`/healthz` 200、无令牌 403、
错令牌 403、正确令牌 initialize 成功、审计文件已生成）+ **部署后邻居回归**，
任一项不过则以非 0 退出。

## 8. 接网关（MCP Gateway）要点

```json
{
  "redis_ro": {
    "display_name": "Redis Read",
    "url": "http://10.0.0.218:8050/mcp/",
    "transport": "streamable-http",
    "headers": { "X-REDIS-RO-MCP-TOKEN": "<与 /etc/redis-ro-mcp/redis-ro-mcp.env 一致>" },
    "forward_headers": ["X-Agent-Id", "X-Forwarded-For"]
  }
}
```

**`forward_headers` 是身份静默的关键**：不转发 `X-Agent-Id` → 审计里 `agent_id` 恒为空；
不转发 `X-Forwarded-For`（多跳保留最左一跳）→ `client_ip` 退化成网关 IP。
临时凭证（`AgentID-Temporary-Credential`）**不向下游透传**，网关↔上游只用上面的令牌头。

网关与 AgentID 侧的登记要素（上线前必须齐备）：

| 项 | 建议值 | 说明 |
|---|---|---|
| 能力标识 | `redis_ro` | 与 Skill 标识一致（小写下划线） |
| 上游地址 | `http://10.0.0.218:8050/mcp/` | 网关侧配置，`/mcp/` 结尾斜杠不可省 |
| 授信编组 | `redis_scope`（`env` ∈ `dev`/`test`/`stage`） | **当前 AgentID 尚无此编组，需新登记**；编组口径与 `mysql_scope`/`mongo_scope` 对齐 |
| 服务显示名 | `Redis Read` | 与 `/healthz` 的 `display_name` 一致 |

> ⚠️ 网关控制台当前 14 项能力里**没有** `redis_ro`，`trusted-services` 里也**没有**
> `redis_scope`。这两项不登记，网关侧调用一律 `404`，与 MCP 本身无关。

## 9. 验证与日常运维

```bash
DEST=/opt/mcpservers/redis-ro-mcp

# ① 健康检查（含 identity 与命令表回显，确认转发与防线是否生效）
curl -s http://127.0.0.1:8050/healthz | python3 -m json.tool

# ② 只读复核（PASS/FAIL，可直接贴验收记录）
bash $DEST/scripts/checks/verify.sh

# ③ 三环境预检（含 Predixy 白名单实测）
set -a; . /etc/redis-ro-mcp/redis-ro-mcp.env; set +a
PYTHONPATH=$DEST/src $DEST/.venv/bin/python $DEST/scripts/checks/precheck.py \
  --config $DEST/config/environments.yaml

# ④ 工具面自检 / 协议层探测（不连库也能跑 --offline）
PYTHONPATH=$DEST/src $DEST/.venv/bin/python $DEST/scripts/tools/selftest.py --offline
PYTHONPATH=$DEST/src $DEST/.venv/bin/python $DEST/scripts/tools/probe_mcp.py

# ⑤ 谁在调（网关透传身份核对）
jq -r '[.ts,.agent_id,.client_ip,.socket_ip,.identity_trusted,.tool,.outcome] | @tsv' \
  $DEST/logs/audit-redis-$(date +%F).jsonl | tail -10
jq -r 'select(.agent_id!="") | [.agent_id,.client_ip] | @tsv' \
  $DEST/logs/audit-redis-*.jsonl | sort | uniq -c | sort -rn | head
```

| 动作 | 命令 |
|---|---|
| 状态 / 日志 | `systemctl status redis-ro-mcp` ／ `journalctl -u redis-ro-mcp -f` |
| 改端口/限额/留存 | 改 `$DEST/config/environments.yaml` → `systemctl restart redis-ro-mcp` |
| 依赖损坏 | `bash deploy/deploy.sh --rebuild-venv` |
| 回滚 | `tar xzf $DEST/backup/<时间戳>/…` 或 `cp -a` 回 $DEST 后 restart |
| 停服务 | `systemctl disable --now redis-ro-mcp` |

## 10. 已知限制

| 项 | 说明 |
|---|---|
| **只读边界在工具面** | 实例无密码无 ACL，数据库层兜不住；最终边界是「只绑内网 + 网关令牌 + 代码白名单」。若未来实例升到 Redis 6+，建议补 ACL 只读用户做双保险（届时只需注入 `${REDIS_*_PASS}`/`username`，代码不用改） |
| 无 slowlog / 键总数 / 精确内存 | `DBSIZE`、`SLOWLOG`、`MEMORY USAGE`、`OBJECT` 全被 Predixy 拒绝；bigkey 只能按类型化长度近似排序 |
| 键总数未知 | SCAN 可渐进枚举但成本高，不提供计数工具（`find_keys`/`bigkey_scan` 都有轮数上限） |
| 后端 Redis 版本不可见 | 代理的 `INFO` 只暴露 Predixy 自身 |
| `HSCAN`/`SSCAN`/`ZSCAN` 不可用 | 不在 Predixy 白名单，所以大 hash 只能按 `field` 精确取，不能游标翻字段 |
| 审计默认不落 key 明文 | 只落 `query_sha256` / `query_len`；要看摘要把 `audit.record_query_text` 改 true（落 ≤500 字符清洗摘要） |
| 未做服务端限流 | 网关侧有超时与配额；本服务只有 socket 超时与各类条数上限 |
| `mirror` ↔ `stage` 是映射不是改名 | 内部实例名不变，对外契约只有三档；响应里同时给 `env` 与 `env_actual` 避免误读 |

> 文档（网关对齐、Skill、测试话术）与上传包**不随包分发**，见《MCP 编写规约》§五。
