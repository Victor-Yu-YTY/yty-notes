# Redis Read MCP 功能说明

> 技术标识 `redis-read-mcp`（包名 `redis-ro-mcp`） · 版本 v1.0.0 · 正式机 `<服务器IP>` · 端口 8050 · 网关注册名 `Redis Read`

## 1. 一句话定位

给 Agent 一套**线下三环境 Redis 的只读眼睛**：键发现、缓存内容排查、TTL 核对、大键采样扫描，**11 个操作，不含任何写操作**。

目标实例是 **Redis 5.0.6（无密码、无 ACL）+ 前置 Predixy 1.0.5 代理**（端口 25900、db0）。工具面只登记读命令，连 `execute` / `eval` 这种通用执行口子都没有。

## 2. 服务边界（做什么 / 不做什么）

- 只读：`SCAN` 枚举键（**绝不使用 `KEYS`**）、模式收集、键类型 / TTL / 类型化长度
- 只读：五种数据结构的区间读取（string 用 `GETRANGE` 分段、hash 按 field、list `LRANGE`、set 成员、zset 按排名或按分数）
- 只读：大键采样排序 Top N
- **不做**：任何写命令（`SET` / `DEL` / `EXPIRE` / `HSET` / `LPUSH` … 共 64 条写命令在防线里）
- **不做**：`EVAL` / `EVALSHA` / 通用命令执行
- **不做**：slowlog、键总数、精确内存——这些命令被 Predixy 拒绝（见 §4.1）
- **不做**：其他逻辑库，固定 db0

## 3. 功能清单（11 个工具）

工具第一个参数一律是 `env`（`dev` / `test` / `stage`），`list_environments` 除外。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `list_environments` | — | 三环境连通性、PING、Predixy 版本、SCAN 抽样 |
| `scan_keys` | `env, pattern='*', cursor=0, count=100` | SCAN 游标翻页枚举键，**绝不使用 KEYS** |
| `find_keys` | `env, pattern, limit=50, with_detail=True` | 模式收集一批键 + 类型 / TTL / 类型化长度 |
| `key_detail` | `env, key` | 存在性、类型、TTL、类型化长度 |
| `read_string` | `env, key, offset=0, length=2000` | `GETRANGE` 分段读，大 value 不拉爆 |
| `read_hash` | `env, key, field=''` | 前 N 个字段（`HLEN` + `HKEYS` + 逐字段 `HGET`）或单字段 |
| `read_list` | `env, key, start=0, stop=9` | `LRANGE` 区间读，支持负数下标 |
| `read_set` | `env, key` | 成员（排序、截断）+ 基数 |
| `read_zset` | `env, key, start=0, stop=9, with_scores=True` | 按**排名**区间读 |
| `read_zset_by_score` | `env, key, min_score='-inf', max_score='+inf', limit=50` | 按**分数**区间读（支持 `(5` 开区间） |
| `bigkey_scan` | `env, pattern='*', sample=500, top=20` | 采样 + 类型化长度排序 Top N |

统一返回信封，成功与失败两种形态：

```json
{"ok": true, "source": "redis:mirror", "elapsed_ms": 12.3, "truncated": false,
 "data": {"env": "stage", "env_actual": "mirror", "key": "...", "...": "..."}}
```

```json
{"ok": false, "error": {"code": "...", "message": "...", "hint": "..."}}
```

审计沿用与其他 MCP 相同的 JSONL schema：`database` 记为 `db0`（库序号），`collection` 记为被访问的 key——这样五个 MCP 的审计可以放进同一套 `jq` 分析。

## 4. 只读边界：为什么只能落在代码里

Redis 侧与 MySQL / MongoDB 有一个本质差别，必须先讲清楚：

| 条件 | 后果 |
| --- | --- |
| Redis 5.0.6 | ACL 是 6.0+ 特性，**建不了只读用户** |
| 实例无密码且不许加 | 数据库层**没有任何写拒绝能力** |
| 官方 `redis/mcp-redis` | 无服务端只读开关，写工具直通无密码实例，风险不可接受 |

所以本服务的只读保证**完全由服务端代码承担**，共三层：

| 层 | 措施 |
| --- | --- |
| 接口层 | 工具面只登记读操作，**没有** `execute` / `eval` / 通用命令执行入口（源码里连 `execute_command` 都没有，`verify.sh` 会断言） |
| 语义层 | 只调用 `allowed_commands` 白名单里的读命令；`forbidden_commands`（64 条写命令）在 `redis_guard.py` 里，同时是静态自检的断言源 |
| 资源层 | SCAN `count` ≤500、成员 ≤100、`find_keys` ≤500、采样 ≤2000、Top N ≤100、SCAN 轮数 ≤60、单字段 ≤2000 字符、响应 ≤1 MiB、socket 超时 5s |

> ⚠️ **最终边界是网络位置 + 网关准入令牌**：任何能直连 25900 的人都可以写库。本服务的只读承诺只覆盖"经本服务调用"这一条路径。

### 4.1 Predixy 白名单实测（决定工具面）

| Predixy 允许 | Predixy 拒绝 |
| --- | --- |
| `PING` / `INFO` / `CONFIG GET`（单参数） | `DBSIZE` |
| `SCAN` / `RANDOMKEY` / `TYPE` / `TTL` / `EXISTS` | `OBJECT` / `MEMORY` |
| `STRLEN` / `GETRANGE` | `SLOWLOG` / `CLIENT`（含 redis-py 的 `CLIENT` 握手） |
| `LRANGE` / `LLEN` / `HGETALL` / `HGET` / `HLEN` / `HKEYS` | `CLUSTER` |
| `SMEMBERS` / `SCARD` / `ZRANGE` / `ZCARD` / `ZRANGEBYSCORE` | |

连带的设计决策：

- **bigkey 用类型化长度近似**（string 按字节、集合按元素数），因为 `MEMORY USAGE` 不可用
- **不提供 slowlog / 键总数工具**，代理直接拒绝
- `INFO` 返回的是 **Predixy 自身信息**（Name / Version / RedisMode），后端 Redis 版本经代理不可见
- 命令被代理拒绝时降级返回 `PROXY_DENIED` + 可用命令提示，不抛堆栈
- 白名单复测入口在 `scripts/checks/precheck.py`，是部署硬门禁的一部分

## 5. 设计要点

### 5.1 单服务多环境 + 环境别名

一个进程覆盖 `dev` / `test` / `stage`。对外统一叫 `dev` / `test` / `stage`，其中 **`stage` 的实际实例是内部 `mirror`**，`mirror` / `sim` 作为别名继续可用。响应回显 `env`（规范名）、`env_actual`（实际实例）、`env_alias`（传别名时才出现）。

### 5.2 redis-py / Predixy 兼容性（两个硬坑，删掉就连不上）

| 参数 | 值 | 原因 |
| --- | --- | --- |
| `protocol` | **2** | redis-py 8.x 默认发 RESP3 `HELLO` 握手，Redis 5.0.6 不认识，直接 `unknown command 'hello'` |
| `lib_name` | **""** | redis-py 8.x 连接时发 `CLIENT SETINFO`，Predixy 白名单拒绝 `CLIENT`，报 `unknown command 'client'` |

这两项在每个环境的 `environments.yaml` 里都显式写了，`scripts/tools/selftest.py` 会逐环境断言，改错即自检失败。

### 5.3 分层安全设计

- **网络**：独立端口 8050，部署在网关之后；systemd 单元 + 部署脚本做端口/路径白名单校验
- **鉴权**：只有一层准入令牌 `X-REDIS-RO-MCP-TOKEN`（兼容旧头 `X-REDIS-MCP-TOKEN`），缺失/错误 → HTTP 403，早于 MCP 协议层；令牌为空时服务**拒绝启动**
- **审计**：JSONL 落 `audit-redis-YYYY-MM-DD.jsonl`，保留 30 天、7 天后压缩；默认只落 `query_sha256` / `query_len`，不落 key 明文
- **凭据**：当前三环境无密码，`REDIS_*_PASS` 用 `${VAR:-}` 可选写法（缺失展开成空串）；日后某环境启用 `requirepass` / ACL，只要注入对应变量即可，**配置与代码都不用改**

### 5.4 调用方身份留痕

网关透传的 `X-Agent-Id` / `X-Forwarded-For` 写入每行审计：`agent_id` / `client_ip` / `socket_ip` / `*_source` / `identity_trusted`。只有直连方在 `identity.trusted_proxies` 内才采信透传头，否则记直连 IP 并标 `identity_trusted=false`。**只追溯、不参与授权**。

### 5.5 与 MySQL / MongoDB 体系同构

与 `dba-read-mcp` / `sql-review-*` / `sql-executor-mcp` / `mongodb-ro-mcp` 共用同一套鉴权、身份透传、审计与部署方式，运维只记一套命令。差异只在客户端与只读防线（`redis_guard.py` 的命令白/黑名单）。

## 6. 典型调用流程

```
提问（"stage 里订单缓存是不是没设过期"）
   │
   ▼
Agent 调 list_environments（确认 stage 连通、Predixy 版本）
   │
   ├─► find_keys（pattern）            圈出候选键 + TTL 一览
   ├─► key_detail（key）               确认类型与 TTL（-1 = 永不过期）
   ├─► read_string / read_hash / …     看具体内容（分段、截断）
   └─► bigkey_scan（pattern）          排查大键热点
   │
   ▼
结论 + 证据（哪些键无 TTL、哪些是 bigkey、体积多少）
```

## 7. 目录结构与关键文件

```
redis-read-mcp/
└── redis-ro-mcp/
    ├── src/redis_ro_mcp/       代码：只放可被 import 的实现
    │   ├── server.py           ★ 11 个工具 + 令牌闸门 + /healthz + 入口
    │   ├── redis_guard.py      ★ 只读防线（命令白/黑名单 / 入参校验 / 输出限额）
    │   ├── config.py           配置加载 + ${VAR} 展开 + 启动期硬校验 + 环境别名
    │   ├── pool.py             连接管理（一环境一客户端，固定 protocol=2 + lib_name=""）
    │   ├── audit.py            JSONL 审计 + 身份字段 + 留存压缩
    │   ├── identity.py         网关透传身份解析（可信代理防伪造）
    │   └── auth.py             令牌常量时间比较 / 头名选取
    ├── config/                 environments.yaml（运行期，无密钥）、deploy.conf（部署期）
    ├── deploy/                 deploy.sh（一键部署）、redis-ro-mcp.service（systemd 模板）
    ├── scripts/checks/         precheck.py（三环境 + 白名单实测）、verify.sh（只读复核）★ 硬门禁
    ├── scripts/tools/          selftest.py（工具面自检）、probe_mcp.py（协议层探测）
    └── README.md  CHANGELOG.md  VERSION  requirements.txt
```

## 8. 自检与运行

```bash
# 起服务（唯一入口）
PYTHONPATH=src python -m redis_ro_mcp.server --transport http --port 8050

# 健康检查（免令牌，含 identity 与命令表回显）
curl -s http://127.0.0.1:8050/healthz | python3 -m json.tool

# 三环境预检（含 Predixy 白名单实测）
set -a; . /etc/redis-ro-mcp/redis-ro-mcp.env; set +a
PYTHONPATH=src python scripts/checks/precheck.py --config config/environments.yaml

# 部署后只读复核（令牌三态 + 审计 + 源码静态断言）
bash scripts/checks/verify.sh

# 工具面自检 / 协议层探测（不连库也能跑 --offline）
PYTHONPATH=src python scripts/tools/selftest.py --offline
PYTHONPATH=src python scripts/tools/probe_mcp.py
```

客户端配置（网关后面）：

```json
{"url": "http://<服务器IP>:8050/mcp/",
 "headers": {"X-REDIS-RO-MCP-TOKEN": "<部署时生成的令牌>"}}
```

网关与 AgentID 侧上线前必须登记：

| 项 | 值 | 说明 |
| --- | --- | --- |
| 能力标识 | `redis_ro` | 与 Skill 标识一致（小写下划线） |
| 上游地址 | `http://<服务器IP>:8050/mcp/` | `/mcp/` 结尾斜杠不可省 |
| 授信编组 | `redis_scope`（`env` ∈ `dev`/`test`/`stage`） | 口径与 `mysql_scope` / `mongo_scope` 对齐 |
| 服务显示名 | `Redis Read` | 与 `/healthz` 的 `display_name` 一致 |

> 能力与编组不登记，网关侧调用一律 `404`，与 MCP 本身无关。

## 9. 配置速查

| 想改什么 | 改哪里 |
| --- | --- |
| 三环境地址 / 端口 / 库序号 / 别名 | `config/environments.yaml` → `environments.*` |
| 端口 / 鉴权头名 / 输出限额 / 超时 | `config/environments.yaml` → `server.*`、`guard.*` |
| 允许与禁用的命令清单 | `config/environments.yaml` → `guard.allowed_commands` / `forbidden_commands` |
| 审计留存、压缩、是否落 key 摘要 | `config/environments.yaml` → `audit.*` |
| 可信代理、是否记录身份 | `config/environments.yaml` → `identity.*` |
| 安装目录 / 密钥目录 / 备份份数 | `config/deploy.conf` |

## 10. 已知边界

- **只读边界在工具面**：实例无密码无 ACL，数据库层兜不住；最终边界是「只绑内网 + 网关令牌 + 代码白名单」。若未来实例升到 Redis 6+，建议补 ACL 只读用户做双保险
- **无 slowlog / 键总数 / 精确内存**：`DBSIZE`、`SLOWLOG`、`MEMORY USAGE`、`OBJECT` 全被 Predixy 拒绝，bigkey 只能按类型化长度近似排序
- **键总数未知**：`SCAN` 可渐进枚举但成本高，不提供计数工具（`find_keys` / `bigkey_scan` 都有轮数上限）
- **后端 Redis 版本不可见**：代理的 `INFO` 只暴露 Predixy 自身
- **`HSCAN` / `SSCAN` / `ZSCAN` 不可用**：不在 Predixy 白名单，大 hash 只能按 field 精确取，不能游标翻字段
- **审计默认不落 key 明文**：需要摘要时把 `audit.record_query_text` 改 `true`（落 ≤500 字符清洗摘要）
- **未做服务端限流**：网关侧有超时与配额，本服务只有 socket 超时与各类条数上限
- **`mirror` ↔ `stage` 是映射不是改名**：内部实例名不变，对外契约只有三档；响应里同时给 `env` 与 `env_actual` 避免误读
