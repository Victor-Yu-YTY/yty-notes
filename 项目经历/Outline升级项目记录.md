# Outline 升级项目记录（0.87.3 → 1.12）

> 一句话回忆：**只换镜像，机器/入口/组件都不动；迁移不可逆，所以升级前必须"备份 + 演练"，回滚必须"镜像 + 两库 + 附件"整套回。**
>
> 记录生成：2026-11-10 ｜ 全篇已进行脱敏处理（IP 仅换网段前缀、域名统一 example.com、人名统一为角色）

---

## 1. 一页速览

| 项 | 内容 |
|---|---|
| 升级目标 | Outline **0.87.3 → 1.12.0**（2026-10-23 发布；官方标注 Node 26.12.1 安全修复"建议尽快升级"，这是定 1.12 的主因之一） |
| 生产机 | `10.0.2.250`，单机 docker compose（6 容器），目录 `/data/outline` |
| 对外入口 | `https://outline.example.com` → 内网 DNS → 网关 `10.0.10.253` openresty 反代 → 250:3000；**本机 traefik 已 Exited 约 5 周，与入口无关** |
| 账号体系 | Keycloak 24.0.2 OIDC 单点登录，**无本地账密**；回滚后全员需重新登录 |
| 演练机 | `10.0.2.39`（生产完整克隆，Rocky 9.5，当前运行 1.12.0） |
| 升级形态 | **组件版本一律不动**（Keycloak / Redis / PostgreSQL），只升 Outline 本体 |
| 窗口 | 2026-11-04 18:30 起；停写 30 分钟（**若按"先停写再备份"执行，窗口需覆盖备份 25-30 分钟 → 预留 60 分钟**） |
| 关键数字 | 迁移 **63 条**（59+4）约 2 分钟 ｜ 列 **430 → 548** ｜ 账本 SequelizeMeta **312** ｜ 备份产物约 **29G** |

---

## 2. 环境与角色（脱敏）

| 角色 | 地址 | 说明 |
|---|---|---|
| 生产机 | `10.0.2.250` | Outline 宿主，root 操作；双库 = outline 主库 + keycloak 库 |
| 演练机 | `10.0.2.39` | 生产完整克隆，`hc-outline-reh-01`，已升到 1.12.0，供生产期间对照 |
| 同步 CI | `10.0.2.62` | GitLab → Outline 文档同步任务；**升级第一步先停它** |
| 中转机 | `10.0.10.205` | 备份单包中点站（校验指纹后不解压直转推），规划中的异地副本落点 |
| 原生 PG 测试机 | `10.0.1.160` | DBA 侧独立验库（单实例 PG，导入对账用） |
| 网关 | `10.0.10.253` | openresty 反代 |
| PVE 宿主 | `10.0.1.200` | 生产虚拟机宿主（VMID=110） |
| 镜像仓库 | `harbor.example.com` | 只有 `0.87` 和 `latest`，**1.12 需补货** |

**6 个容器**：outline（:3000）/ keycloak（:8080）/ traefik（Exited）/ redis / pg-outline / pg-keycloak。
**关键卷**：`outline_postgres_outline_data`、`outline_postgres_keycloak_data`、`outline_redis_data`；附件宿主路径 `/data/outline/data/storage`（30G）。

---

## 3. 最终版升级方案

> 定稿依据：`010-生产升级命令单-20261104` 的 5 段命令 + `011-生产备份回滚保险` 的六道闸门。
> **最终时序以 011 为准 = 先停写再备份**（010 命令单原写"先备份后停服务"，两者冲突，011 已把该变更列为待回写项）。

### 3.1 执行时序（最终版）

```
① 停 10.0.2.62 同步 CI
② docker compose stop outline / keycloak        ← 真停写，窗口开始计时
③ pg_dump 双库 + tar 附件卷 + 账本 + 配置快照    ← 数据静止，天然原子
④ 校验（dump complete ×2 / 账本 312 行 / 五件在位 / sha256）  ← 不过不得往下
⑤ 改镜像 tag 0.87→1.12 → up -d → 盯 63 条迁移 → 对账
⑥ 十项验收 → 恢复同步 CI 回归 → 发恢复周知
```

### 3.2 步骤 0 · 窗口前预检（白天做，服务不停）

```bash
cd /data/outline
df -h / /data                        # 余量：备份+镜像约占 35G（DBA 口径要求 /data ≥60G）
docker compose pull outline          # 前提：harbor 已有 1.12
docker images | grep 1.12
```

### 3.3 步骤 1-2 · 停写 + 备份（约 25-30 分钟）

```bash
cd /data/outline
docker stop outline-outline-1 outline-keycloak-1          # 真停写
B=/root/backup/outline-$(date +%F); mkdir -p $B
set -a; . ./.env; set +a                                  # 每会话必须重设，$BK 丢失踩过坑

docker exec -e PGPASSWORD="$OUTLINE_DB_PASSWORD" outline-postgres-outline-1 \
  sh -c "pg_dump -U $OUTLINE_DB_USER $OUTLINE_DB_NAME" > $B/outline.sql          # 约 4.0G
docker exec -e PGPASSWORD="$KEYCLOAK_DB_PASSWORD" outline-postgres-keycloak-1 \
  sh -c "pg_dump -U $KEYCLOAK_DB_USER $KEYCLOAK_DB_NAME" > $B/keycloak.sql       # 约 344K
docker exec -e PGPASSWORD="$OUTLINE_DB_PASSWORD" outline-postgres-outline-1 \
  psql -U $OUTLINE_DB_USER -d $OUTLINE_DB_NAME -tAc 'SELECT name FROM "SequelizeMeta" ORDER BY name' > $B/sequelizemeta.txt
docker run --rm --volumes-from outline-outline-1 -v $B:/bak alpine \
  tar czf /bak/storage.tar.gz /var/lib/outline/data                              # 约 25G，约 20 分钟
cp docker-compose.yml .env $B/

# 校验（全过才能继续）
ls -lh $B
grep -c 'PostgreSQL database dump complete' $B/outline.sql $B/keycloak.sql       # 期望 1 1
wc -l $B/sequelizemeta.txt                                                       # 期望 312
```

**过关标准**：五件齐（两 SQL + tar + compose/.env + 账本）、`grep` 输出 `1 1`、账本 312 行。

### 3.4 步骤 3 · 换镜像（唯一不可逆点）

```bash
cd /data/outline
sed -i 's/outline:0.87/outline:1.12/' docker-compose.yml && grep -n 'outline:1.12' docker-compose.yml
docker compose up -d outline
docker compose logs -f outline
# 盯 63 条：0.87→1.11 段 59 条约 2 分钟（最慢单条 75s = deletedById 回填 19842 行）+ 1.11→1.12 段 4 条秒级
# 看到 Listening on :3000 = 完成，Ctrl+C 退出
# 出现 error：复制日志 → 停手 → 上报，不要自行重试；迁移中途严禁 kill 容器
```

### 3.5 步骤 4 · 对账验收

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep outline       # Up (healthy)
curl -s -o /dev/null -w '%{http_code}\n' http://10.0.2.250:3000        # 期望 200（端口绑在 IP 上，127.0.0.1 不通属正常）
```

**命令侧 3 项**：容器 healthy + 页面 200 + 账本 312 / 列 548。
**浏览器十项**：全员重新 OIDC 登录 → 老文档 + 附件抽样 → 新建/编辑/保存 → 表格（表头吸顶）→ Mermaid → **个人文档空间（1.12 新增）** → 搜索 → 历史版本 → 分享设置 → 无权限文档"申请访问"。

### 3.6 六道闸门（011 口径，生产的"备份可信"检查表）

| # | 闸门 | 判据 | 状态 |
|---|---|---|---|
| 1 | 备份完整性 | 双 SQL 各 1 处 dump complete + sha256 + tar 可列目录 | ✅ 彩排已验 |
| 2 | 时序原子性 | **先停写再备份**（宁可多停 10 分钟，不赌 tar 打包 20 分钟没人写） | ⚠ 生产按此改 |
| 3 | 回滚路径真实演练 | dropdb+createdb+psql 重放 → 账本 312→249 精确命中 | ✅ 11-05 实测 |
| 4 | 物理隔离（异地副本） | 四件套 scp 推 `10.0.10.205` + 双边 sha256 对账 | ⚠ 尚未落地 |
| 5 | 覆盖面完整 | 五件逐文件核名在位 | ⚠ 当天核 |
| 6 | 决策点前移 | 对账任一不过 → **当场整套回滚，不尝试修补** | ✅ 已写进命令单 |

---

## 4. 最终版回滚方案

> 定稿依据：`012-Outline-Rollback-Cmds-v0.1`（11-05 与 11-09 两轮实测背书）。
> **核心认知：迁移不可逆（账本只进不退、63 条无 down），所以回滚 = 用备份快照物理倒带，必须整套回。**

### 4.1 触发条件与红线

- 触发：十项验收任一不过 / 迁移日志有 error / 页面异常 / 库对账不达标 → **整套回滚（镜像 + 两库 + 附件），不做半套**。
- 红线：`dropdb` 不可逆 —— **必须先看到 R0 三项留档正常才能拆骨架**；重放带 `ON_ERROR_STOP=1`，见错即停、不带病继续。
- 不回滚项（测试机口径）：keycloak 库（从未变更）、storage 卷（窗口内新增附件残留无害）；**生产口径按 011：两库都回，附件按需回**。

### 4.2 五步命令骨架（R0 → T5，全程掐表留档）

> 下面这版是**测试机已验证版**（只回 outline 库 + 切镜像）。**生产照搬时**要按 010 命令单的"回滚"节改成：两库都回（outline + keycloak，dropdb/createdb + psql 重放各来一遍）+ 备份路径换成生产 `/root/backup/outline-<日期>` + 附件按需 `tar xzf` 覆盖。

```bash
cd /data/outline
# R0 回滚前留档（三项）
echo "R0-回滚前留档 $(date +%F_%T)" >> /data/outline/rollback-drill.log
F=/data/outline/.env
U=$(grep '^OUTLINE_DB_USER=' $F | cut -d= -f2- | tr -d '"')
P=$(grep '^OUTLINE_DB_PASSWORD=' $F | cut -d= -f2- | tr -d '"')
D=$(grep '^OUTLINE_DB_NAME=' $F | cut -d= -f2- | tr -d '"')
PSQL="docker exec -e PGPASSWORD=$P outline-postgres-outline-1 psql -U $U -d $D -tAc"
$PSQL 'select count(*) from "SequelizeMeta"'                                            # 1.12 态预期 312（1.11 态 308）
$PSQL "select count(*) from information_schema.columns where table_schema='public'"      # 1.12 态预期 548（1.11 态 546）
ls -lh /data/backup/ | grep outline                                                     # 最新快照与兜底四件套都在位

# T0 停服务（杜绝边倒边写）
docker stop outline-outline-1 outline-keycloak-1

# T1 拆骨架（dropdb 不可逆）
B=$(ls -d /data/backup/outline-pre-up-* | tail -1)        # 兜底换成 10-30 四件套路径
docker exec -e PGPASSWORD=$P outline-postgres-outline-1 sh -c "dropdb -U $U $D && createdb -U $U $D"

# T2-T3 重放 0.87 快照（4.0G，约 10-20 分钟）
docker exec -i -e PGPASSWORD=$P outline-postgres-outline-1 psql -U $U -d $D -v ON_ERROR_STOP=1 < $B/outline.sql

# T4 切镜像回 0.87
sed -i 's|outline:1.12|outline:0.87|g' docker-compose.yml
docker compose up -d

# T5-T6 对账
$PSQL 'select count(*) from "SequelizeMeta"'                                            # 预期 249
$PSQL "select count(*) from information_schema.columns where table_schema='public'"      # 预期 451
curl -s -o /dev/null -w '%{http_code}\n' http://10.0.2.39:3000/                          # 预期 200/302
docker compose ps
```

### 4.3 判定表（精确命中才算成功）

| # | 指标 | 验收线 | 不过怎么办 |
|---|---|---|---|
| 1 | SequelizeMeta | **249**（精确命中） | 停手，贴输出排查 |
| 2 | 列数 | **451**（0.87 快照实测值） | 同上 |
| 3 | HTTP | 200/302 | `docker logs --tail 50 outline-outline-1` |
| 4 | 容器 | Up (healthy) | 同上 |

### 4.4 附加场景

| 场景 | 处置 |
|---|---|
| 升级失败后容器 crash 循环 | 先 `docker compose stop outline` 止血，再走 T0。11-09 实测：单跳 1.12 失败会 crash 循环，但**第一条 baseline-schema 迁移即拦截，库无损** |
| 回滚源 | 默认取 `/data/backup/outline-pre-up-*` 最新目录（升级前现做快照，约 50 秒）；缺失时兜底用 10-30 四件套（29G，端到端验过两轮） |
| 回滚后要再升 1.12 | **必须两跳**：切 `:1.11` → 对账 308/546 → 切 `:1.12` → 对账 312/548 |

### 4.5 回滚后必须收尾

复查同步 CI → 对外入口 → 文档抽样可读；**通知全员重新 OIDC 登录**（Keycloak 库恢复后旧 token 全作废）；写事故记录；本次升级复盘 error 日志后择期重来。

---

## 5. 为什么"只能整套回滚"（一句话记住原理）

Outline 靠三件必须咬合的东西运转：**代码里的 model ↔ 数据库真实表结构 ↔ 迁移账本**。升级的全部风险就是"只变了一半"。

- **容器层零风险**：容器里只有程序没有数据，数据在 PG 卷和附件目录，从未挪窝。
- **不可逆点在迁移**：1.12 启动自动跑 63 条（逐条事务、失败即停可断点续跑），账本 `SequelizeMeta` **只进不退**，没有任何机制往回跑。
- **为什么不能只回镜像**：全库 548 列里唯一"旧代码认识、新库没有"的列是 `teams.collaborativeEditing`（1.11 起协同编辑转正为默认内置，开关退役被删）。0.87 代码读写 teams 表（登录加载工作区就碰）时会生成带该列的 SQL → `column does not exist` → **系统级瘫痪**。所以回滚 = 备份整套恢复，没有捷径。
- **正向为什么稳**：1.12 新列均可空或带默认值（历史数据免清洗，仅 deletedById 回填 19842 行），迁移零报错是结构决定的，不是运气。

---

## 6. 版本差异

**0.87 → 1.12 共 63 条迁移，无删表、无字段类型变更，老数据零迁移成本。**

**真·新增功能 11 项（1.11 段）**：Passkey 免密登录 ｜ 文档访问申请工作流 ｜ 分享链接订阅 ｜ 分享可命名/加图标/TOC ｜ 文档数据洞察 ｜ 外部组映射（SSO 组同步）｜ 自定义 Emoji ｜ **Client credentials 机器凭证 + MCP 入口** ｜ 废弃标记 ｜ 文档语言标记 ｜ 集合级模板管理。

**1.12 增量 6 类**：个人文档空间 ｜ MCP 三连升级（SDK v2 / OAuth Client ID Metadata / 文档变更工具）｜ Mermaid 换 Pele 引擎 ｜ 侧边栏拖拽重构 ｜ Passkey 桌面端 ｜ **Node 26.12.1 安全修复**；另修复"移动文档丢子文档继承权限(#13879)"等。

**三个要记住的口径**：
1. **MCP 默认开启**（`TeamPreference.MCP ?? true`），升级后即对 OAuth 授权客户端可用；端点 `https://<host>/mcp`（旧 SSE 传输已 404），授权跟随用户个人身份，无越权风险。
2. **协同编辑升级后自动启用**，无法再按团队关闭；多人同时编辑行为与 0.87 不同属正常。
3. 个人文档空间默认开、管理员可关；Passkey/外部组/自定义 Emoji 需管理员主动配置，不开不影响老用户。

---

## 7. 红线与踩过的坑

| 项 | 处置 |
|---|---|
| 迁移中途严禁 kill 容器 | 单条卡 60-75 秒属正常；`Listening on :3000` 才算完 |
| 见 error | 复制日志 → 停手 → 上报，不自行重试 |
| 会话变量丢失 | `$BK` 跨会话丢失曾导致备份写错位置 → 备份命令同一会话内一气呵成 |
| 测试机专属配置生产不得带回 | `FORCE_HTTPS=false`、Keycloak `proxy=none` + 裸 IP、`sslRequired=NONE`、回调白名单加 `:3000`、SMTP 注释 |
| 备份与数据同盘 | `/root/backup` 与 `/data` 同盘（盘坏即陪葬）→ 需异地副本（205） |
| 命令单时序 | 010 原为"先备份后停服务"，按 011 改为"先停写再备份"，窗口预算随之 +约 25-30 分钟 |
| 验收操作 | 写 Mermaid 用半角字符（全角会语法报错） |
| 磁盘 | 建大索引 + 回填会膨胀，预检留足余量 |
