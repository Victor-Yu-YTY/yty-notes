# MySQL 大表归档项目记录

> **文档类型**：项目执行记录 / 运维 SOP
> **适用环境**：PROD 等 MySQL 生产集群
> **最后更新**：2026-09-21

---

## 一、项目背景与任务需求

### 1.1 背景

PROD 等生产集群中存在多张业务大表，数据持续累积至**千万~亿级**。业务上只需保留近 6 个月的数据，更早的历史数据不再参与线上查询，但**物理上仍留在生产库**，导致两个问题：

1. **磁盘持续膨胀** —— InnoDB `DELETE` 数据后**表空间不会自动缩小**，删除的行只是变成表文件内部的空闲页，空间不还给操作系统；
2. **不能直接 `DELETE`** —— 一次性删除数千万行会造成主从延迟、锁等待、binlog 暴涨，严重影响线上。

### 1.2 任务需求

| # | 需求 | 达成方式 |
|---|---|---|
| 1 | 把超出保留期的历史数据从生产库**搬走** | `pt-archiver` 边拷边删 |
| 2 | 归档数据**落到归档库**，保留可查 | 写入 `10.29.x.129` 归档实例 |
| 3 | 归档过程**在线、可控、可中断续跑** | 批次化 + `--run-time` 时间窗 + 幂等 |
| 4 | 归档完成后**把磁盘空间还给操作系统** | `pt-online-schema-change` 在线重建 |
| 5 | 长期**维持水位线**，不再堆积 | 转入 `archiver_daily.sh` 每日增量 |
| 6 | 全程**可观测、可验收** | 分阶段日志 + 企微群自动推送 |

### 1.3 关键约束

- 生产库操作只能通过**跳板机**登录运维机执行；
- Navicat 连源库**必须连从库**，禁止连主库；
- 归档窗口与备份窗口错开，必须避开 **01:00~06:00**；
- 每张表保留期不同，需按 `归档汇总.csv` 逐表确认。

---

## 二、涉及资源与整体架构

### 2.1 资源清单

| 资源 | 位置 | 说明 |
|---|---|---|
| 运维机 | `10.29.x.106`（经跳板机登录） | 所有脚本、定时任务、重建操作都在这里 |
| 源库 | PROD 集群（`db-prod-<实例标识>.<内网域名>`） | **Navicat 必须连从库**；脚本读写走可写入口 |
| 归档库 | `10.29.x.129:3306` | 归档数据落地 |
| 全量临时脚本 | `/opt/mysql_archiver/archiver_tmp.sh` | **每换一张表就改**，crontab 每日 01:00 调用 |
| 每日增量脚本 | `/opt/mysql_archiver/archiver_daily.sh` | **只追加不替换**，crontab 每日 01:30 调用 |
| 归档日志目录 | `/opt/mysql_archiver/arch_log/<库名>/` | 全量 `_PURGE_日期.log`；增量 `_日期.log` |
| 表重建工具 | `/opt/ddl-tools/scripts/mysql_pt_online_schema_change.sh` | pt-osc 封装脚本 |
| 重建日志 | `/opt/ddl-tools/log/<集群>_<库>_<表>_<时间戳>.log` | pt-osc 完整输出被重定向到这里 |
| 重建成败总表 | `/opt/ddl-tools/log/pt_online_change.log` | 脚本自动判定 Successfully/Failed |
| 每日群推送监控 | `/opt/mysql_archiver/monitor.sh` | 每天 09:30 扫当天日志推企微，**勿改** |

### 2.2 核心链路

```
每日 01:00  archiver_tmp.sh（全量，5 小时窗口，边拷边删）
                │
                │  跑 3~5 晚，直到源表待归档量归零
                ▼
         pt-online-schema-change 在线重建（回收磁盘空间 / 补索引）
                │
                ▼
每日 01:30  archiver_daily.sh 追加的增量块（维持 6 个月水位线）
                │
                ▼
        09:30 monitor.sh 扫描日志 → 企微群 ✅ 推送 = 全生命周期闭环
```

---

## 三、归档技术方案

### 3.1 全量归档：`pt-archiver` 边拷边删

**为什么不用 `DELETE`**：`DELETE` 无法保留数据、且大事务会拖垮生产。
**为什么不用 `SELECT` + `DELETE` 手动分批**：需要自己实现断点续传、幂等、限流。
`pt-archiver` 把这些都做好了：

- 按主键顺序**分批**（`--limit`）拷贝到目标库并删除源表行；
- 批间**休眠**（`--sleep`）控制对生产的压力；
- **幂等**：被打断后下次从断点继续，不会重复或遗漏；
- **时间窗**：`--run-time` 到点自动停，永不越过归档窗口。

### 3.2 重建：`pt-online-schema-change` 在线改表

**为什么必须重建**：InnoDB 删除数据后表空间文件不缩小，不重建磁盘永远不会释放。

**pt-osc 的原理**（理解后才明白为什么会"失败也要重建一遍"）：

1. 建新表 `_表名_new`（结构 = 原表 + 变更）
2. 在原表上挂 3 个触发器（`pt_osc_*_ins/_upd/_del`）捕获增量写入
3. 分批把原表数据复制到新表
4. **原子换名**：`原表 → _表名_old`，`_表名_new → 原表`
5. 删除 `_表名_old`（**空间在这一步真正还给操作系统**）
6. 删除触发器

> 关键推论：**换名成功后 `_new` 会消失；失败清理后 `_new` 也会消失。**
> 所以"`_new` 不存在"既可能是成功也可能是失败，**不能作为成败判据**。

### 3.3 情况 A / 情况 B 分支（决定 `--where` 写法与重建方式）

整条流程**唯一的逻辑分支**，取决于源表 `CREATE_TIME` 上有没有索引：

| | **情况 A** | **情况 B** |
|---|---|---|
| 前提 | `CREATE_TIME` **有**索引 | `CREATE_TIME` **无**索引 |
| `--where` | `CREATE_TIME<= date_sub(current_date(),INTERVAL N month)` | `ID <= $max_history_id` |
| 边界 | **动态**，随执行日自动前推 | **固定**，一次算出写死 |
| `max_history_id` | 不使用（永久保留该行） | **必须填**边界时间的最大 ID |
| 步骤五重建 | `"engine=innodb"`（纯重建） | `"add index IDX_CREATE_TIME(CREATE_TIME)"`（**重建 + 补索引**） |
| 额外风险 | 无 | ID 顺序需与 CREATE_TIME 大致一致，**必须做安全校验** |

**判定命令**：

```sql
SHOW INDEX FROM <库名>.<表名>;
-- 出现 IDX_CREATE_TIME（或含 CREATE_TIME 的索引）→ 情况 A
-- 没有 → 情况 B
```

**情况 B 的必要性**：重建时补的 `CREATE_TIME` 索引，是步骤六每日增量（一律用动态时间条件）能跑得动的前提。**不补索引 = 增量退化为全表扫描。**

---

## 四、标准作业流程（六步）

### 步骤〇：选表与参数确认

- [ ] 按 `归档汇总.csv` 顺序（**归档量从小到大**）选下一张表
- [ ] 确认四个参数：**库名、表名、保留期 N（月）、`CREATE_TIME` 有无索引**
- [ ] 规模摸底（**查询前先关统计缓存**）：

```sql
SET SESSION information_schema_stats_expiry = 0;

SELECT table_name, table_rows,
       ROUND(data_length/1024/1024/1024,2)  AS data_gb,
       ROUND(index_length/1024/1024/1024,2) AS index_gb,
       ROUND(data_free/1024/1024/1024,2)    AS free_gb
FROM information_schema.tables
WHERE table_schema='<库名>' AND table_name='<表名>';
```

### 步骤一：导出建表语句 → 在归档库建表

1. Navicat（**PROD 从库**）→ 表 → 右键 → 转储 SQL 文件 → **仅结构** → 存为 `<表名>.sql`
2. **【重点】必删两处**：
   - `DROP TABLE IF EXISTS \`表名\`;` **整行删除** —— 防止在归档库误删已有同名表；
   - 建表末尾的 `AUTO_INCREMENT = xxxxx` **删除** —— 归档表自增从 1 开始。
3. **【重点】字段、索引、表级字符集照抄不改**（各表字符集不同，必须与源表严格一致）
4. Navicat 连 **归档库 129**，库不存在先建库（字符集与源库一致），再执行建表
5. 验证基线（预期 0）：`SELECT COUNT(*) FROM <库名>.<表名>;`

> ⚠️ 建表语句里**不写库名前缀**，所以执行前必须确认 Navicat 连的是**归档库**、当前库选的是**目标库**。

### 步骤二：修改全量临时脚本 `archiver_tmp.sh`

**相对上一张表固定改 8 处**（详见第五章代码），改完必须验证：

```bash
cp -p /opt/mysql_archiver/archiver_tmp.sh /opt/mysql_archiver/archiver_tmp.sh.bak_<日期>
vi /opt/mysql_archiver/archiver_tmp.sh

# 验证三连
bash -n /opt/mysql_archiver/archiver_tmp.sh && echo "语法OK"
grep -c "<上一张表名>\|<上一张库名>" /opt/mysql_archiver/archiver_tmp.sh   # 必须为 0
mkdir -p /opt/mysql_archiver/arch_log/<库名>
```

### 步骤三：修改定时任务

```bash
crontab -l > /root/crontab.bak_<日期>     # 先备份
crontab -e
```

tmp 那行**只换日志路径**，时间保持 01:00：

```
00 01 * * * /bin/bash /opt/mysql_archiver/archiver_tmp.sh >> /opt/mysql_archiver/arch_log/<库名>/<表名>_cron.log 2>&1
```

```bash
crontab -l | grep archiver_tmp
diff /root/crontab.bak_<日期> <(crontab -l)     # 期望只有 1 行变化
```

### 步骤四：隔日检查（持续 3~5 晚）

```bash
# 当晚归档情况
tail -30 /opt/mysql_archiver/arch_log/<库名>/<表名>_PURGE_$(date +%Y%m%d).log

# cron 自身输出（脚本没起来时报错在这里）
cat /opt/mysql_archiver/arch_log/<库名>/<表名>_cron.log
```

```sql
-- 剩余待归档量：情况 A 用时间条件；情况 B 用 ID 边界（全表扫描，在从库跑）
SELECT COUNT(*) FROM <库名>.<表名>
WHERE CREATE_TIME <= date_sub(current_date(), INTERVAL N month);   -- 情况 A
SELECT COUNT(*) FROM <库名>.<表名> WHERE ID <= <max_history_id>;    -- 情况 B
```

**判读**：`EXIT STATUS 0` 即正常；剩余量逐晚递减，**归零即全量完成**。

### 步骤五：表重建

**执行前三项检查**：

- [ ] 选**白天低峰**，避开凌晨 01:00~06:00 归档/备份窗口
- [ ] pt-osc 新旧表并存需**约一倍表空间**，确认磁盘余量
- [ ] 记录原表行数（`MIN(ID)` / `COUNT(*)`）与表大小

```bash
echo $STY                       # 有输出说明已在 screen 内，不要嵌套
screen -S osc_<短名>            # 会话名 ≤ 20 字符

# 情况 A：纯重建
/opt/ddl-tools/scripts/mysql_pt_online_schema_change.sh PROD <库名> <表名> "engine=innodb"

# 情况 B：重建 + 补索引一步到位
/opt/ddl-tools/scripts/mysql_pt_online_schema_change.sh PROD <库名> <表名> "add index IDX_CREATE_TIME(CREATE_TIME)"

# Ctrl+A 松开按 D 脱离
```

**验收**：

```sql
-- ① 完整性：MIN(ID) 与重建前完全一致（比 COUNT 更可靠）
SELECT MIN(ID), MAX(ID), COUNT(*) FROM <库名>.<表名>;

-- ② 索引：情况 B 必须出现 IDX_CREATE_TIME
SHOW INDEX FROM <库名>.<表名>;

-- ③ 空间回收（先关统计缓存）
SET SESSION information_schema_stats_expiry = 0;
SELECT ROUND(data_free/1024/1024/1024,2) AS free_gb
FROM information_schema.tables
WHERE table_schema='<库名>' AND table_name='<表名>';   -- 应降到接近 0

-- ④ 无残留
SELECT table_name FROM information_schema.tables
WHERE table_schema='<库名>' AND table_name LIKE '%<表名>%';
SELECT trigger_name FROM information_schema.triggers
WHERE event_object_schema='<库名>' AND event_object_table='<表名>';
```

### 步骤六：追加每日增量块

`vi /opt/mysql_archiver/archiver_daily.sh`，**跳到文件末尾追加**（代码见第五章）。

```bash
bash -n /opt/mysql_archiver/archiver_daily.sh && echo "语法OK"
grep -c "<表名>" /opt/mysql_archiver/archiver_daily.sh     # 应从 0 变为 4
```

**次日 09:30** 企微群出现 `✅ <表名>_YYYYMMDD.log : 归档成功` = **全生命周期闭环**。

---

## 五、归档代码

### 5.1 全量临时脚本 `/opt/mysql_archiver/archiver_tmp.sh`

```bash
#!/bin/bash

time=`date "+%Y%m%d"`

archiver_path=`which pt-archiver`
mysql_path="/opt/mysql/bin/mysql"
user=db_archiver
password=<PASSWORD>
port=3306
host=db-prod-<实例标识>.<内网域名>
dest_host="10.29.x.129"

echo "========== 开始执行单表 (<表名>) 边拷边删作业: $(date "+%Y-%m-%d %H:%M:%S") =========="

# 归档 <表名> 表 (保留<N>个月, <边界方案说明>, 限制执行时间 18000 秒 = 5 小时)
# max_history_id: <情况A: 当前表未使用> / <情况B: 当前表使用, 重建时必须补 IDX_CREATE_TIME 索引>
max_history_id=284121300
$archiver_path --source u=$user,p=$password,h=$host,P=$port,D=<库名>,t=<表名> \
--dest u=$user,p=$password,h=$dest_host,P=$port,D=<库名>,t=<表名> \
--no-check-charset --sleep 3 --analyze=ds \
--where="<情况A: CREATE_TIME<= date_sub(current_date(),INTERVAL N month) | 情况B: ID <= $max_history_id>" \
--no-check-columns --progress=5000 --statistics --bulk-delete --txn-size=5000 --limit=5000 \
--run-time 18000 --replace \
>> /opt/mysql_archiver/arch_log/<库名>/<表名>_PURGE_$time.log

echo "========== 边拷边删作业结束: $(date "+%Y-%m-%d %H:%M:%S") =========="
```

### 5.2 每日增量块（追加到 `/opt/mysql_archiver/archiver_daily.sh` 末尾）

```bash
# <日期> 新增表：<表名> (全量归档完成, 转入每日增量, 保留N个月)
$archiver_path  --source  u=$user,p=$password,h=$host,P=$port,D=<库名>,t=<表名>  \
--dest u=$user,p=$password,h=$dest_host,P=$port,D=<库名>,t=<表名>  \
--no-check-charset  --sleep 1 --analyze=ds \
--where="CREATE_TIME<= date_sub(current_date(),INTERVAL N month)" --no-check-columns --progress=5000 --statistics  --bulk-delete --txn-size=5000 --limit=5000 --replace \
>> /opt/mysql_archiver/arch_log/<库名>/<表名>_$time.log
```

> **增量块一律用动态时间条件**，不继承情况 B 的 ID 边界方案。

### 5.3 建表语句（示例，已删除 `DROP TABLE` 与 `AUTO_INCREMENT`）

```sql
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE `<表名>` (
  `ID` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  -- ...
  `CREATE_TIME` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `MODIFY_TIME` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  -- ...
  PRIMARY KEY (`ID`) USING BTREE,
  INDEX `IDX_CREATE_TIME`(`CREATE_TIME` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = <字符集> COLLATE = <排序规则> COMMENT = '<表说明>' ROW_FORMAT = Dynamic;

SET FOREIGN_KEY_CHECKS = 1;
```

### 5.4 表重建工具 `/opt/ddl-tools/scripts/mysql_pt_online_schema_change.sh`（关键逻辑）

该工具是 pt-osc 的封装，**核心行为必须了解**：

```bash
# 参数：1=集群 2=库 3=表 4=DDL（不含 ALTER TABLE xxx 部分）
# 支持集群：BOSS / CORE / PAI / PROD / AROUND / OPS / QITONG / TONGJI / TEST

# 【关键设计】输出被重定向到独立日志文件，终端上看不到任何输出
pt_online_log='/opt/ddl-tools/log/'$db'_'$schema'_'$table'_'$log_time'.log'
/bin/pt-online-schema-change \
  --host="$host" --port=$port --user="$user" --password="$password" \
  --alter-foreign-keys-method auto --no-version-check --no-check-replication-filters \
  --recursion-method=none --print --progress time,30 \
  --max-load Threads_running=50 --critical-load Threads_running=200 \
  --chunk-time 3 --sleep 10 --max-lag 300 \
  --set-vars "innodb_lock_wait_timeout=600" --charset utf8 \
  --alter "$ddl_value" D=$schema,t=$table --execute > $pt_online_log 2>&1

# 脚本自动判定成败，写入总表
result=$(cat $pt_online_log | grep 'Successfully')
if [[ "$result" != "" ]]; then result='Successfully'; else result='Failed'; fi
echo '[ '$begin_time' ~ '$end_time' ] : '$schema','$table','$ddl_value','$result >> /opt/ddl-tools/log/pt_online_change.log
```

---

## 六、代码逐项解释

### 6.1 `pt-archiver` 参数

| 参数 | 值 | 含义 |
|---|---|---|
| `--source` | `u=,p=,h=,P=,D=,t=` | 源库连接（**可写入口**，因为要删除） |
| `--dest` | 同上 | 目标库（归档库 `10.29.x.129`） |
| `--where` | 保留条件 | **情况 A** 动态时间条件；**情况 B** `ID <= $max_history_id` |
| `--no-check-charset` | — | 跳过源/目标字符集一致性校验（各表字符集不同，必须跳过） |
| `--no-check-columns` | — | 跳过列结构校验 |
| `--sleep` | 全量 `3` / 增量 `1` | 批间休眠秒数，**控制对生产的压力**。全量更保守 |
| `--limit` `--txn-size` | `5000` | 每批 5000 行、每 5000 行提交一次 |
| `--progress` | `5000` | 每 5000 行打印一行进度 |
| `--statistics` | — | 结束时打印统计表（**判读成败的核心依据**） |
| `--bulk-delete` | — | 批量删除（用 `DELETE ... WHERE id IN (...)` 而非逐行），大幅提速 |
| `--analyze=ds` | — | 结束后对目标表/源表做 `ANALYZE`，更新统计信息 |
| `--run-time` | `18000` | **时间窗 5 小时**，到点自动停，永不越界 |
| `--replace` | — | 目标端用 `REPLACE INTO`，保证幂等（重跑不报错） |

### 6.2 日志中的关键字段

```
Started at 2026-09-20T01:00:02, ended at 2026-09-20T06:00:02
SELECT 17165000
INSERT 17160217
DELETE 17160000
Action             Count       Time        Pct
sleep               3432 10296.6999      57.20
inserting       17160217  5582.2260      31.01
bulk_deleting       3432  1291.0843       7.17
```

| 字段 | 判读方法 |
|---|---|
| `ended at` | **≈ 06:00 → 跑满窗口（还没清完）**；**明显早于 06:00 → 全量清完了** |
| `DELETE` | 当晚实际归档行数，满窗约 **1200~2500 万** |
| **`sleep 次数 × --limit`** | **应精确等于 `DELETE`** ← 最可靠的健康信号 |
| `SELECT − DELETE` | 约等于**一批**（5000）→ 正常：最后一批被窗口掐在半路，下次续上，不丢数据 |
| `INSERT ≈ DELETE` | 有细微零头正常 |
| `sleep` 占比 | 约 57%（`--sleep 3` 的自然结果） |

---


## 七、每次新增归档表的维护 SOP

> **触发时机**：需要新归档一张表时（前一张表闭环完成后）

### 7.1 一次性动作（开跑前，同一工作日内完成）

- [ ] **1. 选表**：按 `归档汇总.csv` 顺序（归档量从小到大）取下一张
- [ ] **2. 提参数**：库名、表名、保留期 N、`CREATE_TIME` 有无索引（→ 定情况 A/B）
- [ ] **3. 情况 B 专属**：算出边界最大 ID

```sql
SELECT MAX(ID) AS max_history_id
FROM <库名>.<表名>
WHERE CREATE_TIME <= date_sub(current_date(), INTERVAL N month);
```

- [ ] **4. 情况 B 专属：安全校验（必做！）**

```sql
-- ID 在边界内、但 CREATE_TIME 很新的行数，应该很小
SELECT COUNT(*) AS risky_rows
FROM <库名>.<表名>
WHERE ID <= <max_history_id>
  AND CREATE_TIME > date_sub(current_date(), INTERVAL N month);
-- 0 或很小 → 放行；几万行以上 → 停！方案会误归档数据
```

- [ ] **5. 建表**：导出结构（删 `DROP`、删 `AUTO_INCREMENT`）→ 建到**归档库 129** → 验证 `COUNT = 0`
- [ ] **6. 改 tmp 脚本 8 处** → `bash -n` + `grep -c` 旧表残留为 0
- [ ] **7. 建日志目录**：`mkdir -p /opt/mysql_archiver/arch_log/<库名>`
- [ ] **8. 改 crontab**：只换日志路径，时间保持 01:00 → `diff` 确认只 1 行变化

### 7.2 每日常规动作（01:00 自动跑，无需干预）

- [ ] **每天上午**：`tail -30 .../<表名>_PURGE_$(date +%Y%m%d).log`，看 `EXIT STATUS` 与行数
- [ ] **每天上午**：查剩余待归档量，确认逐晚递减

### 7.3 全量完成后（进入收尾）

- [ ] **9. 重建**：按情况 A/B 选命令，白天低峰 + screen
- [ ] **10. 验收**：`MIN(ID)` 一致 + 索引齐全 + `data_free` → 0 + 无残留
- [ ] **11. 追加增量块**到 `archiver_daily.sh` 末尾 → `bash -n` 校验
- [ ] **12. 闭环**：次日 09:30 企微群 ✅
- [ ] **13. 回转步骤 1**，开始下一张表

> ⚠️ **第 13 步同时解决"tmp 任务重复"问题**：tmp 脚本和 crontab 改到新表后，上一张表就只剩增量任务在跑了。

---

## 八、执行归档期间需要注意的问题

### 8.1 🔴 高危红线（错了会出生产事故）

| # | 问题 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | Navicat 连源库时**连到主库** | 脚本是"边拷边删"，在错误目标上执行=删数据 | 源库**一律连从库** |
| 2 | 建表语句里的 `DROP TABLE IF EXISTS` 没删 | 在归档库执行会**连数据一起删光**已有同名表 | 导出后**第一件事**就是删掉整行 |
| 3 | 建表语句字符集/索引改过 | 归档数据变形、索引失效 | 字段、索引、字符集**照抄不改** |
| 4 | 没确认 `CREATE_TIME` 有无索引 | 情况判断错 → 全量全表扫描拖垮从库；重建漏补索引 → 增量失效 | 动手前先 `SHOW INDEX` |
| 5 | 重建没放 screen / 撞上归档窗口 | SSH 断连中断 DDL；与 01:00 窗口争资源 | `screen` + 白天低峰 |
| 6 | 重建前没确认磁盘余量 | 磁盘写满 → **整个实例所有库都写不进去** | 确认余量 ≥ 表大小 ×1.2 |

### 8.2 🟡 判读误区（本次项目实际踩过）

| # | 误区 | 真相 |
|---|---|---|
| 7 | 用 `COUNT(*)` 判断重建是否成功 | **不行**。重建成功和失败，`COUNT` 都会随业务写入而增长。**只有 `data_free` 能判定** |
| 8 | 用 `COUNT(*)` 不变来验收重建 | **不行**。表在持续写入（实测约 0.3~0.8 行/秒），`COUNT` 必然微增。**应用 `MIN(ID)` 做完整性校验** |
| 9 | 认为 `_new` 表不存在 = 换名成功 | **不行**。换名成功和失败清理**都会**让 `_new` 消失 |
| 10 | 信任 `information_schema` 的表大小/`data_free` | ⚠️ **MySQL 8.0 有 24 小时统计缓存**（`information_schema_stats_expiry` 默认 86400）。查询前必须 `SET SESSION information_schema_stats_expiry = 0;`，否则读到的是陈旧快照 |
| 11 | 认为 `total_gb` 重建后会下降 | **不会**。`data_length + index_length` 统计的是活数据；被困住的空间体现在 **`data_free`** |
| 12 | 在 screen 里盯 pt-osc 进度 | **看不到**。工具脚本把输出重定向到 `/opt/ddl-tools/log/` 了，screen 里只有命令回显 |
| 13 | 用 `screen -ls` 会话消失判断 pt-osc 结束 | **错误**。screen 会话里是 shell，脚本跑完 shell 还在 → 会话不会消失。要 `exit` 才消失。**应看 pt-osc 进程 + 日志** |

### 8.3 🟢 操作细节

| # | 事项 | 说明 |
|---|---|---|
| 14 | screen 会话名长度 | GNU Screen 上限 **20 字符**，超长会被截断，导致 `screen -r <名字>` 失败 |
| 15 | 嵌套 screen | 已在 screen 内时先 `echo $STY` 判断，**不要套第二层**，否则 `Ctrl+A` 被外层吃掉 |
| 16 | `ps -ef` 含明文密码 | 工具脚本用 `--password=` 传参，**任何人 `ps` 都能看到**。分享诊断信息前替换成 `***` |
| 17 | 工具脚本硬编码密码 | `/opt/ddl-tools/scripts/mysql_pt_online_schema_change.sh` 内明文存储，建议权限收紧到 `600 root:root` 并评估轮换 |
| 18 | 日志命名规则 | `monitor.sh` 扫 `arch_log/**/*_当天日期.log` 推企微 —— **命名写对才自动纳入监控**。全量带 `_PURGE_`，增量不带 |
| 19 | tmp 与增量重复 | 转入增量后 tmp 那行仍指着旧表，会每晚跑两遍（无害但冗余），**换下一张表时一并改掉** |
| 20 | 情况 B 的 ID 顺序假设 | `ID <= max_history_id` 隐含"ID 顺序 ≈ CREATE_TIME 顺序"。**开跑前必须做 `risky_rows` 安全校验** |

### 8.4 ⚙️ 性能观察

- 全量单窗口实测处理 **1,670~1,760 万行 / 5 小时**（约 **950~980 行/秒**）；
- `--sleep 3` 占窗口时间 **约 57%**，`inserting` 占 31%，`bulk_deleting` 占 6~7%；
- 重建（pt-osc）实测 514 万行耗时 **16 分 30 秒**；
- 参照：7,200 万行的表约需 **4~5 晚** 完成全量。

---

## 九、附录

### 9.1 常用命令速查

```bash
# ===== 106 运维机 =====
# 查看某表全量日志（当晚）
tail -30 /opt/mysql_archiver/arch_log/<库名>/<表名>_PURGE_$(date +%Y%m%d).log

# 查看 cron 自身输出
cat /opt/mysql_archiver/arch_log/<库名>/<表名>_cron.log

# 统计已跑了几个晚上
ls -1 /opt/mysql_archiver/arch_log/<库名>/<表名>_PURGE_*.log | wc -l

# 统计累计归档行数
grep -h "^DELETE" /opt/mysql_archiver/arch_log/<库名>/<表名>_PURGE_*.log \
  | awk '{s+=$2} END {printf "累计归档: %d 行\n", s}'

# ===== 重建相关 =====
# pt-osc 进程是否在跑
ps -ef | grep pt-online-schema-change | grep -v grep

# 重建成败总表（脚本已自动判定）
tail -3 /opt/ddl-tools/log/pt_online_change.log

# 本次重建的完整日志
ls -lht /opt/ddl-tools/log/ | head -5
tail -f /opt/ddl-tools/log/<集群>_<库>_<表>_<时间戳>.log

# ===== 定时任务 =====
crontab -l | grep archiver
```

```sql
-- ===== 进度与验收 SQL =====
-- 剩余待归档量（情况 A）
SELECT COUNT(*) FROM <库名>.<表名>
WHERE CREATE_TIME <= date_sub(current_date(), INTERVAL N month);

-- 剩余待归档量（情况 B）
SELECT COUNT(*) FROM <库名>.<表名> WHERE ID <= <max_history_id>;

-- 表大小与空洞（查前务必关缓存）
SET SESSION information_schema_stats_expiry = 0;
SELECT table_name,
       ROUND(data_length/1024/1024/1024,2)  AS data_gb,
       ROUND(index_length/1024/1024/1024,2) AS index_gb,
       ROUND(data_free/1024/1024/1024,2)    AS free_gb
FROM information_schema.tables
WHERE table_schema='<库名>' AND table_name='<表名>';

-- 完整性基线
SELECT MIN(ID) AS min_id, MAX(ID) AS max_id, COUNT(*) AS cnt FROM <库名>.<表名>;

-- 残留检查
SELECT table_name FROM information_schema.tables
WHERE table_schema='<库名>' AND table_name LIKE '%<表名>%';
SELECT trigger_name FROM information_schema.triggers
WHERE event_object_schema='<库名>' AND event_object_table='<表名>';
```

### 9.2 关键路径与文件

| 路径 | 用途 |
|---|---|
| `/opt/mysql_archiver/archiver_tmp.sh` | 全量临时脚本（每张表改一次） |
| `/opt/mysql_archiver/archiver_daily.sh` | 每日增量脚本（只追加） |
| `/opt/mysql_archiver/arch_log/<库名>/` | 归档日志目录 |
| `/opt/mysql_archiver/monitor.sh` | 企微推送监控（**勿改**） |
| `/opt/ddl-tools/scripts/mysql_pt_online_schema_change.sh` | pt-osc 封装工具 |
| `/opt/ddl-tools/log/<集群>_<库>_<表>_<时间戳>.log` | pt-osc 完整输出 |
| `/opt/ddl-tools/log/pt_online_change.log` | 所有 DDL 执行成败总表 |
| `/root/crontab.bak_<日期>` | crontab 备份 |

### 9.3 术语

| 术语 | 含义 |
|---|---|
| 全量归档 | 清理历史存量数据的阶段，多晚完成 |
| 每日增量 | 长期运行，每晚归档刚跨过保留期边界的少量数据 |
| 情况 A / B | 源表 `CREATE_TIME` 有无索引，决定边界与重建方式 |
| `max_history_id` | 情况 B 用：边界时间对应的最大主键 ID |
| 闭环 | 全量 → 重建 → 增量 三步完成，企微群收到 ✅ |

---

*本文档基于 2026-09-15 ~ 09-21 的实际执行过程整理，含 `TABLE_A`（已闭环）与 `TABLE_B`（进行中）两个案例。*
