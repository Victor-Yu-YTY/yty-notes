# MySQL 全表扫描诊断与索引设计实战记录

> 表名/库名/IP/账号均已脱敏为通用示例名；数值保留真实量级。
> 适用：MySQL 8.0 + InnoDB，百万~千万级业务表。版本 v3.0

## 目录

- [0 索引原理](#0-索引原理)
- [1 三分钟定位法](#1-三分钟定位法)
- [2 全表扫描的八大类型](#2-全表扫描的八大类型)
- [3 联合索引设计规范](#3-联合索引设计规范)
- [4 用不到最左字段时如何用上联合索引](#4-用不到最左字段时如何用上联合索引)
- [5 索引的触发条件](#5-索引的触发条件)
- [6 何时生效何时失效](#6-何时生效何时失效)
- [7 两个顺序陷阱](#7-两个顺序陷阱)
- [8 索引解决不了的情况](#8-索引解决不了的情况)
- [9 常见误区](#9-常见误区)
- [附录 诊断SQL速查](#附录-诊断sql速查)

---

## 0 索引原理

**全表扫描**：数据按 16KB 的页存放，没索引时只能从第一页读到最后一页。1000 万行 ≈ 6.5 万个页。

**索引**：把某几列**排好序**，再建一棵多叉查找树（B+ 树）。像查字典先看拼音目录，直接翻到那一页。

| 对比（1000 万行表） | 全表扫描 | 走索引 |
|---|---|---|
| 读页数 | 约 65,000 | 3~4（+回表） |
| 扫描行数 | 10,000,000 | 几十~几百 |
| 数据量翻倍 | 耗时线性翻倍 | 树多一层，几乎不变 |
| 免排序 | 不能 | 能（天然有序） |

**回表**：主键索引叶子存整行；二级索引叶子存 `(索引列值, 主键值)`。用二级索引定位后还要回主键树取整行 = 一次随机 IO。**随机 IO 比顺序 IO 慢一到两个数量级** —— 这就是"筛不掉多少行时索引反而不如全扫"的原因。

**覆盖索引**：SELECT 要的列都在索引里 → 不回表，`EXPLAIN` 显示 `Using index`。

**记住三件事**：
1. 索引快是因为**有序** → 能定位、能免排序
2. 不是越多越好 → 回表有成本，命中率高时不如全扫
3. 有序不只服务 WHERE → ORDER BY / GROUP BY / JOIN 也能吃到（见第 5 节）

---

## 1 三分钟定位法

```sql
-- ① 谁最贵
SELECT ROUND(SUM_TIMER_WAIT/1e12,1) AS 累计秒, COUNT_STAR AS 次数,
       ROUND(SUM_TIMER_WAIT/1e9/NULLIF(COUNT_STAR,0),1) AS 平均毫秒,
       ROUND(SUM_ROWS_EXAMINED/NULLIF(COUNT_STAR,0))    AS 每次扫描行数,
       SUM_NO_INDEX_USED AS 未走索引次数,
       SUBSTRING(DIGEST_TEXT,1,200) AS 语句
FROM performance_schema.events_statements_summary_by_digest
WHERE COUNT_STAR > 100
  AND DIGEST_TEXT NOT LIKE '%information_schema%'
  AND DIGEST_TEXT NOT LIKE '%performance_schema%'
ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;

-- ② 判据：扫描行数 / 返回行数 > 100 → 索引候选

-- ③ EXPLAIN SELECT ...;
```

| 字段 | 危险信号 | 含义 |
|---|---|---|
| `type` | `ALL` | 全表扫描 |
| `type` | `index` | 扫整条索引（接近全扫） |
| `key` | `NULL` | 没走索引 |
| `rows` | ≈ 表总行数 | 预计扫全表 |
| `Extra` | `Using filesort` | 额外排序 |
| `Extra` | `Using temporary` | 用临时表 |
| `possible_keys` 有值但 `key=NULL` | **优化器主动放弃** | 见 2.7 |

---

## 2 全表扫描的八大类型

### 2.1 增量拉取：时间列无索引

```sql
SELECT <20+列> FROM t_bid_record
WHERE __MODIFY_TIME__ > ? AND __MODIFY_TIME__ <= ?
ORDER BY id ASC LIMIT ?;
```

- **违反**：筛选列无索引；排序列与筛选列无法复用同一索引 → 优化器改扫主键
- **方案**：`ADD INDEX IDX_MODIFY_TIME (__MODIFY_TIME__)`
- **效果**：580 万行表，**21,534 ms → 5.2 ms**（约 4,000 倍），扫描 611 万 → 0

> `ORDER BY` 没走索引不要紧，`WHERE` 筛出的行很少，末尾补一次小排序远比扫全表便宜。

### 2.2 等值列无索引，目标是少数派

```sql
SELECT <13列> FROM t_task_follow
WHERE TASK_EXPIRE_TIME < NOW() AND TASK_STATUS = ?;
```

- **违反**：两个条件列都无索引；现有索引首列是 `DEALER_ID`，条件里没有
- **方案**：`ADD INDEX IDX_STATUS_EXPIRE (TASK_STATUS, TASK_EXPIRE_TIME)`
- **效果**：350 万行表，**49.7 秒 → 1 秒内**；扫描 349 万 → 约 10 万（目标状态仅占 3%）

### 2.3 最左前缀失效

```sql
-- 现有索引 (DEALER_ID, CREATE_TIME)
SELECT ... FROM t_event_push
WHERE PUSH_STATUS = ? AND CREATE_TIME < ?
ORDER BY CREATE_TIME LIMIT ?;
```

- **违反**：WHERE 中没有索引首列 `DEALER_ID` → 整条索引作废
- **方案**：`ADD INDEX IDX_STATUS_TIME (PUSH_STATUS, CREATE_TIME)`
- **效果**：1,300 万行表，扫描 **1,145 万 → 500 行**，`filesort` 消失

> 若不能改索引，见第 4 节六种解法。

### 2.4 条件列与索引列不一致（影子列）

```sql
-- 表上有 IDX_CREATE_TIME(CREATE_TIME)、MODIFY_TIME(MODIFY_TIME)
-- 查询却用 __MODIFY_TIME__（BI 同步专用影子列）
SELECT ... FROM t_business_check
WHERE __MODIFY_TIME__ > ? AND __MODIFY_TIME__ <= ?
ORDER BY id ASC LIMIT ?;
```

- **违反**：索引列必须与条件列**完全一致**，语义相近的不同列不共享索引
- **方案**：`ADD INDEX IDX_MODIFY_TIME_BI (__MODIFY_TIME__)`
- **排查**：表上有相似名字的索引却仍全扫 → 先核对条件用的到底是哪一列

### 2.5 队列表零二级索引

```sql
-- 只有 PRIMARY(id)
SELECT ... FROM t_outbox_target WHERE status = ? AND updated_at <= ?;
```

- **违反**：轮询表 `(状态, 时间)` 未成对建联合索引；`ORDER BY` 列未与等值列同索引
- **方案**：`ADD INDEX idx_status_updated (status, updated_at)`
- **效果**：186 万行表，两条 SQL 合计 20,248 秒/15h，每次扫 328 万行 → 毫秒级

### 2.6 多条件无匹配索引

```sql
SELECT <13列> FROM t_invitation
WHERE INVITE_STATUS=? AND ISFETCH=? AND CITY_ID=?
  AND SOURCE_ID IN (...) AND CHANNEL_ID IN (...) LIMIT ?;
```

- **违反**：多条件等值查询需要**一条联合索引覆盖全部等值列**；表上 30 个索引、总量是数据 4 倍，却没有这组组合
- **方案**：`ADD INDEX (INVITE_STATUS, ISFETCH, CITY_ID, SOURCE_ID, CHANNEL_ID)`
- **效果**：每次扫 1.2 万行只返 1 行、116ms → 预计 5ms 内（每秒 3~6 次调用）

### 2.7 优化器主动放弃索引

```
EXPLAIN → type=ALL | possible_keys=IDX_TIME_STATUS | key=NULL
```

**索引**：`(TASK_EXPIRE_TIME, TASK_STATUS)`（时间在前）
**查询**：`WHERE TASK_EXPIRE_TIME < NOW() AND TASK_STATUS = ?`

- **违反**：前导列必须是高选择性"定位条件"。`< NOW()` 命中全表 **95%+**，扫索引 ≈ 扫全表，还要额外回表
- **验证**：采样 40 万行 —— 最早 20 万行已过期占 100%，最新 20 万行占 96.9%
- **方案**：删掉，重建为 `(TASK_STATUS, TASK_EXPIRE_TIME)`
- **确认白建**：

```sql
SELECT INDEX_NAME, COUNT_STAR AS 使用次数 FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE OBJECT_SCHEMA='your_db' AND OBJECT_NAME='your_table';
-- 使用次数=0 且已过一个业务周期 → 从未被使用
```

### 2.8 更新语句选择性极差

```sql
UPDATE t_task_register SET city_id = ? WHERE admin_id = ?;
```

- **现状**：`admin_id` 有索引且真在用，但全表仅 **27 个不同值**，单次改约 **105 万行**，平均 **198 秒**
- **结论**：不是索引问题 → 收窄条件 / `LIMIT` 分批 / 复核业务语义

---

## 3 联合索引设计规范

### 3.1 三条硬规则

| 规则 | 说明 |
|---|---|
| **最左前缀** | `(A,B,C)` 被使用的前提是条件含 `A` / `A+B` / `A+B+C` |
| **等值在前，范围在后** | `=`、`IN` 放前；`>`、`<`、`BETWEEN`、`LIKE 'x%'` 放后 |
| **排序对齐** | 要消除 filesort，`ORDER BY` 列须紧跟等值列之后且方向一致 |

### 3.2 字段排序决策流程

```
① 全部等值 / IN 列 —— 能定位，放最前
② 范围条件列 —— 只能筛选，放其后
③ ORDER BY 列 —— 与范围列相同可顺带免排序
④ 必要时补列做覆盖索引（慎用宽列）
```

### 3.3 选择性临界值（判断能否做前导列）

| 条件命中率 | 效果 |
|---|---|
| < 5% | 极佳 |
| 5% ~ 20% | 良好 |
| 20% ~ 30% | 边缘，可能被放弃 |
| **> 30%** | **大概率全表扫，索引白建** |

### 3.4 索引成本

| 成本 | 量级 |
|---|---|
| 空间 | datetime 单列 ≈ 17~18 字节/行；1000 万行约 170 MB |
| 写入 | 每多一个索引，INSERT/UPDATE 多一次维护 |
| 数量 | 单表二级索引建议 ≤ 5~8 个 |

**冗余判定**：`(A)` 是 `(A,B)` 的最左前缀 → `(A)` 可删。

```sql
SELECT TABLE_NAME, INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols
FROM information_schema.STATISTICS WHERE TABLE_SCHEMA='your_db'
GROUP BY TABLE_NAME, INDEX_NAME;
```

---

## 4 用不到最左字段时如何用上联合索引

**原因**：索引 `(a,b,c)` 先按 a 排、再按 b、再按 c。只有 `WHERE b=?` 时，b 的值散落在不同 a 段且不连续，无法二分定位。就像电话簿按「姓+名」排，你只知道"名"。

**关键前提**：判定只看"条件里有没有这一列"，形式不限 —— `=`、`IN`、`BETWEEN`、`>`、`IS NULL` 都算。

| 方案 | 做法 | 成本 | 前提 / 风险 |
|---|---|---|---|
| **1 调整索引顺序** | `(a,b,c)` → `(b,c,a)` | 空间 0 | 需评估原 `WHERE a=?` 的查询；首选 |
| **2 新建以该列开头的索引** | `ADD INDEX (b,c)` | 中 | 仅长期高频时值得；若 `(b,c)` 是 `(b,c,a)` 前缀，只建长的 |
| **3 给 SQL 补最左列** | `WHERE a IN (1,2,3) AND b=?` | 0 | 最左列可枚举且值少；**语义必须等价**；定时任务改造最常用 |
| **4 Index Skip Scan** | 8.0.13+ 优化器自动改写 | 0 | 最左列基数极低才有效，**不保证生效**，不能用于排序 |
| **5 手工 UNION ALL** | 按 a 的取值分别查再合并 | 0 | 取值固定且少；`UNION` 会多一次去重排序 |
| **6 覆盖索引兜底** | 把 SELECT 列全放进索引 | 大 | 索引变宽、写入成本长期上升 |

**验证 skip scan 是否生效**：`EXPLAIN` 的 `Extra` 出现 `Using index for skip scan`。

**选型顺序**：先 3（改 SQL 最便宜）→ 再 1（顺序可能只是历史遗留）→ 再 2 → 最后 4/5/6。

---

## 5 索引的触发条件

**索引有两种能力**：WHERE 用「定位」（少读行），ORDER BY / GROUP BY 用「有序」（少排序）。很多人只盯 WHERE，白白多做一次排序。

| 语句成分 | 能否用索引 | 靠哪种能力 |
|---|---|---|
| WHERE 等值 / IN | ✅ | 定位 |
| WHERE 范围 | ✅（命中率低才划算） | 定位 |
| **ORDER BY** | ✅ | 有序（免 filesort） |
| **GROUP BY** | ✅ | 有序（免 temporary） |
| DISTINCT | ✅ | 有序 |
| **JOIN 关联列** | ✅ | 定位（JOIN 优化核心） |
| MIN / MAX | ✅ | 有序（直接取端点） |
| LIMIT | ✅ | 有序（提前终止） |
| SELECT 列 | ✅ | 覆盖（免回表） |
| HAVING | ❌ | 聚合后才过滤 |

### 5.1 ORDER BY 免排序的四个条件

排序列是索引前缀或紧跟等值列之后；方向一致（8.0 可建降序索引）；列上无函数运算；没被 `OR` 打断。

以索引 `(a,b,c)` 为例：

| 查询 | 免排序 | 原因 |
|---|---|---|
| `WHERE a=? ORDER BY b` | ✅ | b 紧跟等值列 |
| `WHERE a=? ORDER BY b,c` | ✅ | 连续前缀 |
| `WHERE a=? ORDER BY c` | ❌ | 跳过 b |
| `ORDER BY b` | ❌ | a 不在条件里，索引整体按 (a,b) 有序 |
| `WHERE a IN (1,2) ORDER BY b` | ⚠️ 常仍 filesort | 多个 a 值各自的 b 需归并 |
| `WHERE a>? ORDER BY b` | ❌ | a 是范围，之后无法定位/排序 |

### 5.2 JOIN

- 驱动表（`EXPLAIN` 第一行）可以没索引，它只扫一次
- **被驱动表（第二行）的关联列必须有索引**，否则驱动表每行触发一次全扫
- 两表关联列**字符集 / collation 不一致** → 索引直接失效

### 5.3 反直觉点

WHERE 用不上索引时，`ORDER BY + LIMIT` 也可能走索引，但若 WHERE 筛不掉多少行，会变成"扫全索引 + 逐行判断"，**可能比全表扫更慢**。

> 判断依据永远是 `EXPLAIN` 的 `rows`（预计扫描行数），而不是"有没有用上索引"。

---

## 6 何时生效何时失效

### 6.1 会走索引

| 情形 | 示例（索引 `(A,B,C)`） |
|---|---|
| 全等值 | `WHERE A=? AND B=? AND C=?` |
| 前缀等值 | `WHERE A=?`；`WHERE A=? AND B=?` |
| 等值 + 范围 | `WHERE A=? AND B=? AND C>?` |
| 等值 + 排序 | `WHERE A=? ORDER BY B` |
| 等值 + 范围 + 同列排序 | `WHERE A=? AND B>? ORDER BY B` |
| 覆盖查询 | `SELECT A,B FROM t WHERE A=?` |
| 多值 IN 前缀 | `WHERE A IN (...) AND B=?` |

### 6.2 失效情形

| # | 情形 | 正确做法 |
|---|---|---|
| 1 | 跳过最左列 `WHERE B=?` | 见第 4 节 |
| 2 | 列上运算 `WHERE DATE(t)=?` | 改范围 `t>=? AND t<?` |
| 3 | 列上函数 `WHERE UPPER(n)=?` | 函数索引（8.0.13+）或改写 |
| 4 | 隐式类型转换 `WHERE varchar_col=123` | 参数类型与列一致 |
| 5 | JOIN 字符集 / collation 不一致 | 统一字符集与排序规则 |
| 6 | 前置通配 `LIKE '%abc'` | 改后缀匹配或全文索引 |
| 7 | `OR` 连接不同列 | 拆 `UNION ALL` 或建 `(A,D)` |
| 8 | `!=` / `NOT IN` / `NOT LIKE` | 改正向条件 |
| 9 | **范围列之后的列** `A=? AND B>? AND C=?` | 把 C 放 B 前：`(A,C,B)` |
| 10 | **前导列命中率 > 30%** | 换高选择性列做前导 |
| 11 | 统计信息过期 | `ANALYZE TABLE` |
| 12 | 查询列多、回表成本高 | 覆盖索引或拆查询 |
| 13 | `IS NOT NULL`（命中大比例） | 视分布，必要时加标记列 |
| 14 | 排序方向冲突 `ORDER BY A DESC, B ASC` | 建降序索引 |

---

## 7 两个顺序陷阱

### 7.1 等值 vs 范围

| 索引 | `WHERE status=? AND expire<NOW()` |
|---|---|
| `(status, expire)` | ✅ 先定位再过滤 |
| `(expire, status)` | ❌ 先扫 95% 的时间范围 = 扫全表 |

### 7.2 时间 vs 状态：不是"时间在前就更好"

| 场景 | 推荐 | 理由 |
|---|---|---|
| 含 `ORDER BY 时间列 + LIMIT` | 时间在前**也可用** | 索引天然有序，可顺着走并提前终止 |
| 只有 `时间范围 + 状态等值` | **状态在前** | 时间范围命中率常 >90%，无法定位 |
| 目标状态占 3%（少数派） | **状态在前** | 直接定位到 3% |
| 目标状态占 97%（多数派） | 都无效 | 本就捞大部分数据，需改 SQL |

**用数据决策，别猜**：

```sql
SELECT TASK_STATUS, COUNT(*), SUM(时间列 < NOW())
FROM t_task_follow WHERE id BETWEEN ? AND ?   -- 抽样
GROUP BY TASK_STATUS;
```

> 实测：两张相似的表，时间在前在一张上生效（1,145 万 → 500 行），另一张完全失效（`used=0`）。同一套写法不能无脑套用。

---

## 8 索引解决不了的情况

| 类型 | 判据 | 正解 |
|---|---|---|
| 单次更新/删除行数巨大 | 影响行数占表比例高 | 收窄条件、分批 LIMIT |
| 条件列区分度极低且目标占多数 | 目标值 > 30% | 改 SQL 加时间窗 / LIMIT |
| 纯写入热点 | INSERT 耗时长、扫描行 0 | 批量插入、合并事务 |
| 高频元数据查询 | 每秒级查 processlist 等 | 降频、缓存 |
| 缓存/内存不足 | buffer pool 命中率已 >99.5% | **加池无效**，从连接数、单连接缓冲、慢 SQL 入手 |
| 临时表落盘 | `Using temporary` + `Created_tmp_disk_tables` 高 | 优化 SQL 或调大 `tmp_table_size` |

---

## 9 常见误区

| 误区 | 事实 |
|---|---|
| 索引多就没问题 | 案例中某表 30 个索引（总量是数据 4 倍），目标查询仍全表扫 |
| 时间字段放前面更通用 | 命中率高时前导列失效，优化器直接放弃 |
| 加了索引就会变快 | 必须验证 `used>0` 且 `key` 不为 `NULL` |
| `possible_keys` 有值就是走了 | 看 `key`，`NULL` 表示候选未采用 |
| 看累计平均耗时判断效果 | 含历史慢执行，会得出"没用"的错误结论，必须取两次差值 |
| OR 一定用不上索引 | 组成 `(A,D)` 或走 index merge 仍可能用上 |
| buffer pool 越大越快 | 命中率 99.8% 时再加收益 <0.1%，却吃满剩余内存 |
| 慢 SQL 优化完内存就宽松了 | 查询内存是瞬时的；常驻内存由**连接数 × 单连接缓冲**决定 |
| 定期重启能解决内存问题 | 只是重置计时器；某实例半年内因内存耗尽被动重启 26 次 |
| 看到 WHERE 就能用索引 | 运算、函数、隐式转换、类型不一致都会失效 |
| ORDER BY 一定需要 filesort | 与索引有序性对齐时可完全免排序 |

---

## 附录 诊断SQL速查

```sql
-- 未走索引的语句排行
SELECT SUM_NO_INDEX_USED, COUNT_STAR, ROUND(SUM_TIMER_WAIT/1e12,1) AS 秒,
       SUBSTRING(DIGEST_TEXT,1,150)
FROM performance_schema.events_statements_summary_by_digest
WHERE SUM_NO_INDEX_USED > 0 ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;

-- 表级读写量排行
SELECT OBJECT_SCHEMA, OBJECT_NAME, COUNT_READ, COUNT_WRITE
FROM performance_schema.table_io_waits_summary_by_table
ORDER BY COUNT_READ DESC LIMIT 20;

-- 从未被使用的索引（可清理）
SELECT OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, COUNT_STAR
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE INDEX_NAME IS NOT NULL AND COUNT_STAR = 0;

-- 改造前后对比：间隔 5~10 分钟采两次算差值
SELECT COUNT_STAR, SUM_TIMER_WAIT, SUM_ROWS_EXAMINED
FROM performance_schema.events_statements_summary_by_digest
WHERE DIGEST_TEXT LIKE '%your_table%';

-- 锁等待
SELECT * FROM performance_schema.data_lock_waits;

-- 当前连接数（无 PROCESS 权限时）
SELECT COUNT(*) FROM performance_schema.threads WHERE TYPE='FOREGROUND';
SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status
WHERE VARIABLE_NAME IN ('Threads_connected','Threads_running','Max_used_connections');
```

---

*基于真实生产排查记录整理，已脱敏*
