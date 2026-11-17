# Redis 学习文档（速查版 · 通用脱敏版）

> 写给 0 基础但熟悉 MySQL/MongoDB 的人。读完能：说清 Redis 是什么、数据怎么放怎么取、怎么写怎么读、什么时候该用/不该用。
> 目标：**快速回忆用**，不追求大而全。文末有「一分钟回忆卡」。
> **本文不含任何环境信息**（主机、端口、凭据、内部服务名、业务 key 前缀均为示例或占位），可直接外发。

---

## 1. 一句话认识 Redis

**Redis 是一个把数据全放内存的 key-value 数据库**，读写是微秒级的，但内存比磁盘贵，所以它天生不是"主库"。

和另外两个的根本差别，一句话概括：

| | 数据放在哪 | 怎么找数据 | 适合干什么 |
|---|---|---|---|
| **MySQL** | 磁盘（表结构固定） | SQL：`WHERE` 任意字段 + JOIN + 聚合 | 业务主数据、事务、复杂查询 |
| **MongoDB** | 磁盘（文档，无固定结构） | `filter` 条件 + 索引 + 聚合管道 | 半结构化数据、字段常变 |
| **Redis** | **内存**（key-value，无结构） | **只能按 key 精确取**（+ 少数按分值/成员查） | 缓存、计数、排行、锁、限流、会话 |

> **最关键的认知**：Redis **没有 `WHERE`**。你不能"查所有 status=1 的 key"。你只能：
> ① 知道 key 名 → 直接取；② 用 `SCAN` 按前缀模糊扫（慢、只适合排查）；③ 把要查的维度**设计进数据结构**（比如用 zset 做排行榜、用 set 做标签索引）。

---

## 2. 和 MySQL / MongoDB 的对比表

| 维度 | MySQL | MongoDB | Redis |
|---|---|---|---|
| 数据模型 | 二维表（行 + 列，schema 固定） | 集合 + 文档（JSON，schema 灵活） | **key → value**（value 有 5 种类型） |
| 最小单位 | 行 | 文档 | 一个 key 对应的整个 value |
| 查询能力 | 最强（任意条件、JOIN、聚合） | 强（条件、聚合管道） | **最弱**（按 key 取；无 JOIN、无 WHERE） |
| 写入速度 | 千级/秒 | 千~万级/秒 | **十万级/秒**（单机） |
| 事务 | 完整事务（ACID） | 4.0 起多文档事务 | **只有"命令组原子执行"，出错不回滚** |
| 持久化 | WAL/binlog，强 | journal + 落盘 | RDB 快照 / AOF 日志，**可能丢最近数据** |
| 数据量上限 | TB 级 | TB 级 | **受内存限制**（通常几十 GB 内） |
| 扩容 | 分库分表 / 中间件 | 分片集群 | Cluster 分片（16384 槽） |
| 典型用途 | 业务主数据 | 日志/半结构化/文档 | **缓存、计数器、排行榜、分布式锁、限流、Session** |

**判断口诀**：
> 要**复杂查询/事务/唯一数据源** → MySQL；**字段不固定/文档形态** → MongoDB；
> **要快、可按 key 取、丢了能重建** → Redis。

---

## 3. 键值对模型（最重要的部分）

### 3.1 key 是什么

- key 是**二进制安全的字符串**（可以是中文、可以是 `user:1:name` 这种带冒号的路径）。
- **大小写敏感**，`User:1` 和 `user:1` 是两个不同的 key。
- 长度没有硬限制，但**建议 ≤ 100 字符**，太长的 key 浪费内存、也难读。
- 命名**强烈建议用冒号分层**（社区事实标准，图形客户端也据此建树）：

```
业务名:对象:id:字段
order:93821:progress            ← 订单进度缓存
user:1001:profile               ← 用户档案
spu:888:tags                    ← 商品标签
```

### 3.2 value 是什么（5 种基本类型）

Redis 的 value **不是"一行数据"**，而是 5 种结构之一。**选错类型 = 后面所有操作都难受**。

| 类型 | 结构 | 一句话 | 典型用途 | 常用命令 |
|---|---|---|---|---|
| **string** | 一个字节串（最大 512MB） | 最基础，可当数字自增 | 缓存 JSON、计数器、分布式锁 | `SET` `GET` `INCR` `GETRANGE` `STRLEN` |
| **hash** | 一个 field→value 的小字典 | 相当于"一行记录"，能单独改字段 | 对象属性、会话信息 | `HSET` `HGET` `HGETALL` `HKEYS` `HLEN` |
| **list** | 双向链表（可当双端队列） | 有序、可重复 | 队列、最新 N 条、时间线 | `RPUSH` `LPUSH` `LRANGE` `LLEN` |
| **set** | 无序、自动去重 | 用来做"是否存在/交集" | 标签、黑名单、共同好友 | `SADD` `SMEMBERS` `SCARD` `SISMEMBER` |
| **zset**（sorted set） | set + 每个成员带一个分数，按分数自动排序 | **排行榜专用** | 排名、延时队列、按时间范围取 | `ZADD` `ZRANGE` `ZRANGEBYSCORE` `ZRANK` |

> 5.0 起还有 **stream**（消息流，类似简化版 Kafka），日常排障用得少，先知道有就行。

### 3.3 举一套例子说清"怎么建模"

业务需求：缓存一个订单

| 方案 | 怎么做 | 好不好 |
|---|---|---|
| 全塞 string | `SET order:123 '{"id":123,"status":2,...}'` | ✅ 读一次全拿到；❌ 改一个字段要整串重写 |
| 用 hash | `HSET order:123 id 123 status 2` | ✅ 可单独改 `status`；✅ 部分读；**推荐** |

业务需求：查"某商品下有哪些订单"（这正好是 Redis 不擅长的）

| 方案 | 怎么做 |
|---|---|
| ❌ 错 | 想"遍历所有订单判断商品 id" —— Redis 没有 WHERE，做不到 |
| ✅ 对 | 额外维护索引：`SADD goods:777:orders 123 456`，查的时候直接取这个 set |

业务需求：销量排行榜

| 方案 | 怎么做 |
|---|---|
| ✅ | `ZINCRBY rank:sell:202611 1 sku:888`，取前 10：`ZREVRANGE rank:sell:202611 0 9 WITHSCORES` |

---

## 4. 怎么写数据（录入数据）

### 4.1 命令行（redis-cli）——最直接的验证方式

```bash
redis-cli -h 127.0.0.1 -p 6379            # 连本机默认端口

# string
SET user:1001:name "张三"
SET session:abc "token-xyz" EX 3600       # 写完就带 1 小时过期（推荐写法，原子）
SET lock:order:123 "uuid-1" NX PX 30000   # 仅当不存在时写入 + 30 秒过期 = 分布式锁

# hash（推荐用于"一条记录"）
HSET order:123 id 123 status 2 amount 99.5
HINCRBY order:123 amount 10               # 数值字段原子自增

# list（队列）
RPUSH queue:task:1 "job-a" "job-b"        # 右侧入队
LPOP  queue:task:1                        # 左侧出队 → 先进先出

# set
SADD user:1001:tags "vip" "beijing"
SADD goods:777:orders 123 456

# zset（排行榜）
ZADD rank:sell:202611 100 sku:888 90 sku:999
ZINCRBY rank:sell:202611 1 sku:888        # 分数 +1

# 过期控制
EXPIRE user:1001:name 600                 # 10 分钟过期
TTL    user:1001:name                     # 剩余秒数（-1 永不过期，-2 已不存在）
PERSIST user:1001:name                    # 取消过期
```

**写完怎么确认写对了**（别用 `KEYS` 全扫，会阻塞实例）：

```bash
TYPE   order:123                 # → hash
HGETALL order:123                # 看内容
TTL    order:123                 # 看是否设了过期
SCAN 0 MATCH order:* COUNT 100   # 按前缀扫（游标翻页，不阻塞）
```

### 4.2 代码里怎么写（Python 为例）

```python
import redis

r = redis.Redis(host="127.0.0.1", port=6379, db=0, decode_responses=True)

r.set("user:1001:name", "张三", ex=600)           # 带过期
r.hset("order:123", mapping={"id": 123, "status": 2})
r.rpush("queue:task:1", "job-a")
r.zadd("rank:sell:202611", {"sku:888": 100})
r.expire("order:123", 3600)
```

> **两种环境适配**（连不通时先查这两条，不是代码写错了）：
> - 服务端是 **Redis 5 或更早**（不支持 RESP3）：客户端需指定 **协议 2**（redis-py：`protocol=2`），否则握手报 `unknown command 'hello'`。
> - 前面挡了**代理/中间件**（某些连接代理只放行部分命令）：客户端初始化时可能发 `CLIENT SETINFO`，代理不认 → 需要关掉（redis-py：`lib_name=""`）。

### 4.3 写数据的三条铁律

1. **一定要想过期**：缓存类数据**必须带 TTL**（`EX`/`EXPIRE`），否则内存会被慢慢吃满。
2. **不要把大对象塞一个 key**：单个 value 建议 **< 10KB**，集合元素建议 **< 5000**（大 key 会拖慢整个实例，删除时甚至会卡住）。
3. **写完要能被读到**：设计时就想好"将来按什么查"，把索引（set/zset）一起写进去。

---

## 5. 怎么读数据

```bash
GET    user:1001:name              # string
HGETALL order:123                  # hash 全部字段
HGET   order:123 status            # hash 单字段（字段多时优先用这个）
LRANGE queue:task:1 0 -1           # list 全部（0 -1 表示首到尾）
LRANGE queue:task:1 0 9            # 前 10 条
SMEMBERS user:1001:tags            # set 全部成员
SCARD     user:1001:tags           # set 基数（成员数），比 SMEMBERS 便宜
ZRANGE    rank:sell:202611 0 9 WITHSCORES     # zset 排名前 10（小→大）
ZREVRANGE rank:sell:202611 0 9 WITHSCORES     # 从大到小
ZRANGEBYSCORE rank:sell:202611 100 200        # 按分数区间
EXISTS user:1001:name              # 1/0，是否存在
TYPE   user:1001:name              # 值的类型
```

**遍历 key 只有一种正确姿势**：

```bash
SCAN 0 MATCH "order:*" COUNT 100              # 返回 (下一个游标, 一批 key)
SCAN 1179648 MATCH "order:*" COUNT 100        # 用上一步的游标继续，直到游标回到 0
```

- `SCAN` 是**渐进式**的：不会阻塞实例，但**可能重复返回、也可能漏**（同一轮里保证不重复，跨轮不保证）。
- **`KEYS *` 绝对不要在生产用**：它一次性遍历全部 key，会阻塞整个 Redis。

---

## 6. 过期、淘汰与内存

### 6.1 过期（TTL）

- 设置：`SET k v EX 60`（秒）/ `PX 60000`（毫秒）/ 事后 `EXPIRE k 60`
- 查看：`TTL k`（秒）/ `PTTL k`（毫秒）→ `-1` 永不过期、`-2` 不存在
- **删除策略**：惰性删除（访问时才判断）+ 定期随机抽样清理。所以**过期不等于立刻消失**，内存占用会滞后。

### 6.2 淘汰（内存满了怎么办）

配置 `maxmemory` + `maxmemory-policy`：

| 策略 | 行为 | 适用 |
|---|---|---|
| `noeviction` | 写满就报错 | 不能丢数据 |
| `allkeys-lru` / `allkeys-lfu` | 所有 key 里按最近/最少使用淘汰 | **纯缓存（最常用）** |
| `volatile-lru` / `volatile-ttl` | 只在**设了过期**的 key 里淘汰 | 缓存与持久数据混放 |
| `allkeys-random` | 随机淘汰 | 少见 |

> 铁律：**缓存和"不能丢的数据"不要放同一个实例**。要混放就只用 `volatile-*` 策略，并保证持久数据不设 TTL。

---

## 7. 持久化：为什么 Redis 也可能丢数据

| 方式 | 原理 | 优点 | 缺点 |
|---|---|---|---|
| **RDB**（快照） | 周期性 fork 子进程把内存 dump 成文件 | 文件小、恢复快 | **两次快照之间宕机会丢** |
| **AOF**（日志） | 每条写命令追加到日志 | 丢得少（`everysec` 最多丢 1 秒） | 文件大、恢复慢 |
| 混合（4.0+） | RDB 头 + AOF 增量 | 兼顾 | — |

**结论**：Redis 默认就不是"强持久"。**能丢的数据放 Redis，不能丢的放 MySQL**。

---

## 8. 事务、Lua、原子性

- `MULTI` … `EXEC`：命令**排队后一次性执行**，中间不会插别人的命令（隔离），但：
  - **运行期出错不会回滚**（比如类型用错，其他命令照常生效）；
  - 没有"要么全成要么全败"。所以它叫"命令组原子"，不叫事务。
- `WATCH key`：乐观锁，key 被别人改过则 `EXEC` 返回 nil。
- `EVAL`（Lua 脚本）：脚本内**整体原子**，适合"读-判断-写"的复合操作。
- 单条命令（`SET`/`INCR`/`HSET`）**本身就是原子的** —— 大部分场景够用，不需要事务。

---

## 9. 什么时候**不该**用 Redis

| 需求 | 为什么不行 | 该怎么办 |
|---|---|---|
| 复杂条件查询 / JOIN / 聚合统计 | 没有 WHERE、没有 JOIN | MySQL |
| 需要事务回滚、强一致 | 不支持回滚 | MySQL |
| 数据量远超内存（几百 GB） | 内存装不下 | MySQL / MongoDB |
| 全文检索、模糊搜索 | 不支持 | ES / MySQL LIKE（谨慎） |
| 需要长期可靠存储 | 持久化不保证不丢 | MySQL / 落盘 |
| 单个超大 value（图片/大 JSON） | 阻塞网络与实例 | 对象存储 + Redis 只存指针 |

---

## 10. 常见坑速查

| 坑 | 症状 | 正确做法 |
|---|---|---|
| `KEYS *` | 实例卡住几秒~几十秒 | 用 `SCAN` 翻页 |
| 大 key（几十 MB 的 string / 上万元素的 set） | 读写变慢、删除时卡顿 | 拆分 key；单 value <10KB、集合 <5000 |
| 热 key（某个 key QPS 极高） | 单分片打满 | 本地缓存 / 多副本散列 |
| 缓存没设 TTL | 内存缓慢涨满，触发淘汰把好数据挤掉 | 所有缓存类 key 一律带过期 |
| 用 Redis 当唯一数据源 | 宕机/淘汰后数据找不回 | 以 MySQL 为准，Redis 只做加速 |
| 用 string 存"一行记录" | 改一个字段要重写整串（还不原子） | 用 hash |
| 以为 `EXPIRE` 会立刻释放内存 | 内存占用滞后不降 | 正常现象（惰性+定期删除） |
| 集群下用多 key 命令 | `MGET`/`MSET`/`SUNION` 跨槽报错 | 保证同槽（key 加 `{tag}`），或拆多次单 key |
| 用 `MULTI` 当事务 | 出错不回滚，以为数据没变 | 关键写操作用 Lua 或业务层补偿 |
| 拿 Redis 做模糊搜索 | 想做"包含某词"的查询 | 做不到，换 ES/MySQL |
| 命令"莫名"报 `unknown command` | 前面有代理/中间件，只放行部分命令 | 先确认是不是中间件拦的；用 `PING` 验证链路 |

---

## 11. 版本差异速查（5 / 6 / 7）

踩坑多半踩在版本差异上，认版本再写代码：

| 能力 | 5.x | 6.x | 7.x |
|---|---|---|---|
| `ACL`（多用户 + 细粒度权限） | ❌ | ✅ **6.0 起** | ✅ |
| RESP3 / `HELLO` 握手 | ❌（只有 RESP2） | ✅ | ✅ |
| 客户端多线程 I/O（`io-threads`） | ❌ | ✅ | ✅ |
| `UNLINK`（异步删除大 key） | ✅ 4.0 起 | ✅ | ✅ |
| `STREAM` 结构 | ✅ **5.0 起** | ✅ | ✅ |
| hash/list 的 `listpack` 编码 | ❌（ziplist） | ❌ | ✅ **7.0 起** |
| `FUNCTION`（脚本库） | ❌ | ❌ | ✅ |

> 实用结论：
> - 连 **5.x**：客户端必须用 **RESP2**，`HELLO`/`ACL` 都用不了 → 权限只能靠网络和账号口令。
> - 想要"只读账号"：**必须 6.0+**，用 `ACL SETUSER ... +@read -@write -@admin` 建只读用户。
> - 大 key 用 `UNLINK` 删（4.0+ 异步释放），别用 `DEL` 卡住主线程。

---

## 12. 一分钟回忆卡

```
定位     内存 KV 数据库；快、贵、可能丢；不是主库
形态     key(字符串) → value(5 种类型之一)
没有     WHERE / JOIN / 事务回滚 / 复杂查询
五种类型 string(缓存、计数、锁)  hash(一行记录)  list(队列)
         set(去重/标签/存在性)   zset(排行榜/分数区间)
写       SET k v EX 60 · HSET k f v · RPUSH/ SADD / ZADD · EXPIRE
读       GET / HGETALL / LRANGE 0 -1 / SMEMBERS / ZRANGE 0 9 WITHSCORES
遍历     SCAN 0 MATCH 前缀:* COUNT 100（游标翻页，永不用 KEYS）
过期     TTL=-1 永久、-2 不存在；过期≠立刻删
满了     maxmemory-policy：纯缓存用 allkeys-lru/lfu
持久化   RDB 快照 / AOF 日志，都可能丢最近数据
建模     先想"将来按什么查"，把索引（set/zset）一起写进去
写读口诀 先设计过期，再设计索引；读不清就 TYPE 一下
版本     5 无 ACL 无 RESP3；只读账号要 6.0+；大 key 用 UNLINK 删
```

---

## 附：想继续深入时看什么

| 主题 | 关键词 |
|---|---|
| 五种类型的底层编码 | `intset` / `ziplist` / `listpack` / `quicklist` / `skiplist` |
| 内存与碎片 | `maxmemory`、`MEMORY USAGE`、`MEMORY DOCTOR`、`activedefrag` |
| 高可用 | 主从复制、`Sentinel`、`Cluster`（16384 槽） |
| 消息 | `LIST` 队列 vs `PUBSUB` vs `STREAM`（5.0） 三者语义完全不同 |
| 分布式锁 | `SET k v NX PX ms` + 唯一值 + Lua 释放 |
| 排障命令 | `INFO`、`SLOWLOG`、`CLIENT LIST`、`MEMORY USAGE`、`OBJECT ENCODING`、`DBSIZE` |
