# Greenplum 学习文档（脱敏通用版）

> 本文件分三部分：**一、GP 是什么**（原理，够用就行）→ **二、这套集群长什么样**（示例拓扑，对着查）→ **三、启动 GP 的简洁命令**（日常启停用）。
>
> **说明**：本文所有 IP、主机名、目录、时间均已脱敏替换为通用示例值，结构、原理、命令与判读方法完全通用。
> 拿到自己的环境时，只需把示例里的 `mdw / smdw / sdw1~sdw4`、`10.0.0.x`、`/data/greenplum` 换成实际值即可。

---

## 0. 一句话先记住

> **Greenplum = 一个"总调度"（Master）+ 若干"干活的机器"（Segment）；一条 SQL 交给总调度拆开，让所有干活的机器并行算，最后汇总结果返回给你。**

它不是"一主一备的高可用数据库"，而是**一个由多台机器拼起来、对外看起来像一个大数据库的并行数据库集群**。

---

# 一、Greenplum 是什么

## 1.1 和 MySQL / Oracle 的区别在哪

普通单机数据库：

```
你 ──► 一台服务器上的一个数据库进程 ──► 一块磁盘
       （数据全在一台机器上，算不动就加 CPU/内存/磁盘）
```

Greenplum：

```
                ┌──────────── Master（总调度，只存"目录信息"，几乎不存业务数据）
   你 ──► psql ─┤
                └──────────── 把一条 SQL 拆成 8 份，同时发给 8 个 Segment
                                     │
        ┌────────────┬───────────────┼───────────────┬────────────┐
     Segment1     Segment2        Segment3        ...         Segment8
     （各存 1/8 的表数据，各算各的）
        └────────────┴───────────────┼───────────────┴────────────┘
                                     ▼
                            结果汇总 → 返回给你
```

关键结论（也是新手最容易误解的三点）：

| 误解 | 事实 |
|---|---|
| "GP 就是 PostgreSQL 改名" | 语法/协议确实是 PostgreSQL 血统（GP 5.x 内核是 PG 8.3，GP 6 是 PG 9.4，GP 7 是 PG 12），但**加了分布式执行层**：数据分片、并行计算、集群管理工具都是原版 PG 没有的 |
| "连上 Master 就等于看到全部数据" | 对，但 Master **自己几乎不存业务数据**，数据都在 Segment 上。所以 Master 挂了 = 全库不可用，即使数据完好 |
| "加机器就能变快" | 一台 Segment 机器要承担它上面所有实例的 IO。**慢通常不是 CPU 不够，而是数据分布不均 / 网络 / 磁盘**（见 2.4 的实际分布表） |

## 1.2 必须掌握的 7 个名词

| 名词 | 通俗解释 | 在示例环境里对应什么 |
|---|---|---|
| **Master** | 总调度。接收客户端连接（5432）、解析 SQL、分发任务、汇总结果。数据库的"目录"（哪些库、表、字段）存在它这里 | `mdw` = `10.0.0.11` |
| **Standby Master** | Master 的**热备**。持续从 Master 同步目录信息，Master 挂了可以顶上去 | `smdw` = `10.0.0.12` |
| **Segment** | 干活的机器/实例。每一份表数据都存在某个 Segment 上 | `sdw1`~`sdw4` = `10.0.0.13`~`10.0.0.16` |
| **Primary（主实例）** | Segment 的"正本"，业务读写都走它 | 8 个，全部在跑 |
| **Mirror（镜像实例）** | Segment 的"副本"，实时从 Primary 同步。Primary 挂了它顶上 | 8 个，其中 **4 个示例为 down** |
| **gpseg-N** | 实例的编号（N=0..7）。**同一个编号 = 同一份数据**（主和镜像编号相同） | `gpseg0`~`gpseg7` |
| **gpadmin** | 运行 GP 的**专用 Linux 用户**。所有 GP 工具必须用它跑，root 会被拒绝 | 数据目录属主、`gpstart` 执行者 |

## 1.3 三个"环境变量式"的名词（每次登录都要用）

```bash
GPHOME=/usr/local/greenplum-db                    # 程序安装目录（示例值）
source $GPHOME/greenplum_path.sh                  # 必须 source，否则 gpstart/gpstate/psql 找不到
MASTER_DATA_DIRECTORY=/data/greenplum/master/gpseg-1   # Master 数据目录（以环境实际为准！）
```

> ⚠️ 两个高频坑：
> **① 数据目录路径不一定是默认的 `/data/master/gpseg-1`**，很多环境装在别的挂载点（如 `/opt/...`）。别猜，用 `ps -ef | grep -- '-D'` 或 `~gpadmin/gpAdminLogs/gpstart_*.log` 里的路径确认。
> **② 环境变量往往没写进 `gpadmin` 的 `.bashrc`**，每次登录后必须手工 `export MASTER_DATA_DIRECTORY=...`，否则 `gpstart` 报找不到目录。

## 1.4 一条 SQL 在 GP 里是怎么跑的

以 `select count(*) from orders where ...` 为例：

1. 你的 `psql` 连到 **Master:5432**
2. Master 解析 SQL、生成执行计划（这一步和普通 PG 一样）
3. Master 发现 `orders` 是分片表 → 把条件改写后**并行下发**给 8 个 Primary Segment
4. 8 个 Segment 各自扫描本地那 1/8 数据、各自 count
5. 结果回到 Master，Master 求和 → 返回给你

所以：

- **查询慢**，看是不是某个 Segment（某台机器）特别慢 → 全集群被它拖住
- **更新/删除慢或报错**，常见是分布键选得不好导致数据倾斜
- **`select count(*) from gp_dist_random('gp_id')` 返回几行 = 有几个实例在正常干活**（这是最有用的一条检查命令）

---

# 二、这套集群长什么样

## 2.1 拓扑总览（对着这个看）

```
                    客户端（psql / BI / 应用）
                              │ 5432
                              ▼
      ┌───────────────────────────────────────────────┐
      │ 10.0.0.11   mdw                               │  ← Master（dispatch）
      │ 数据目录 /data/greenplum/master/gpseg-1        │
      └───────────────────────────────────────────────┘
                              │ 实时同步目录
                              ▼
      ┌───────────────────────────────────────────────┐
      │ 10.0.0.12   smdw                              │  ← Standby Master
      │ 状态：Standby host passive（待命中）            │
      └───────────────────────────────────────────────┘

   ── Segment 层：4 台机器 × 每台 2 个数据目录（data / data2）× 每目录 primary+mirror ──

  10.0.0.13 sdw1   10.0.0.14 sdw2
  10.0.0.15 sdw3   10.0.0.16 sdw4

  10.0.0.17 sdw5   ← 在集群里，但当前不承载任何 Segment（空闲机器）
```

> 注意主机名的坑：Master 叫 `mdw`，Segment 叫 `sdw1/sdw2/...`，**编号前缀不代表同一台机器**。集群里如果同时存在 `xxx-master` 和 `xxx-segment` 这类命名，务必用 IP 或 `hostname -I` 核对，别靠名字猜。

## 2.2 机器清单

| IP | 主机名 | 角色 | 该机上应该有 | 端口 |
|---|---|---|---|---|
| 10.0.0.11 | mdw | **Master** | 1 个实例：`master/gpseg-1` | 5432 |
| 10.0.0.12 | smdw | **Standby Master** | 1 个实例：`gpseg-1`（备） | 5432 |
| 10.0.0.13 | sdw1 | Segment | 4 个实例（data/data2 各 primary+mirror） | 40000+ |
| 10.0.0.14 | sdw2 | Segment | 4 个实例 | 40000+ |
| 10.0.0.15 | sdw3 | Segment | 4 个实例 | 40000+ |
| 10.0.0.16 | sdw4 | Segment | 4 个实例 | 40000+ |
| 10.0.0.17 | sdw5 | Segment（空载） | 0 个实例 | — |

> 端口规律：Master 固定 **5432**；Segment 实例一般从 **40000** 起（`data/` 一套、`data2/` 一套，第二套常从 50000 起）。
> **准确值不要背，用 `gp_segment_configuration` 查**。

## 2.3 数据目录结构（这个必须记，排障全靠它）

```
/data/greenplum/                        ← GP 数据根目录（示例路径，实际可能挂在 /opt 等其它位置）
├── data/
│   ├── master/gpseg-1/                 ← Master 数据
│   │   ├── postgresql.conf             ← 配置文件
│   │   ├── postmaster.pid              ← 存在=实例在跑或上次异常退出；不存在=干净停机
│   │   └── pg_log/                     ← 主库日志，每天一个 gpdb-YYYY-MM-DD_000000.csv
│   ├── primary/gpseg0/  ~ gpseg3/      ← Segment 正本
│   └── mirror/gpseg0/   ~ gpseg3/      ← Segment 副本
└── data2/                              ← 同一台机器上的"第二套"实例目录
    ├── primary/gpseg4/  ~ gpseg7/
    └── mirror/gpseg4/   ~ gpseg7/
```

**为什么有 `data` 和 `data2` 两套？**
因为物理机只有几台，但要求 8 个分片；GP 允许**单机多实例**，用不同目录 + 不同端口跑多个实例，这样几台机器就能凑出 8 个分片。代价是**一台机器挂了会同时丢多个实例**，所以镜像必须放在"另一台机器"上（下面 2.4 就是按这个规则排的）。

程序安装目录是另一个路径，别搞混：

```
/usr/local/greenplum-db/bin/               ← gpstart / gpstate / psql 等命令在这里
/usr/local/greenplum-db/greenplum_path.sh  ← 必须 source 的环境脚本
```

## 2.4 分片与镜像的实际分布（8 primary + 8 mirror = 16 个实例）

| Segment | Primary 在 | Mirror 在 | 示例环境状态 |
|---|---|---|---|
| gpseg0 | sdw1 data/primary | sdw2 data/mirror | Primary ✅ / **Mirror ❌ down** |
| gpseg1 | sdw2 data/primary | sdw3 data/mirror | ✅ / ✅ |
| gpseg2 | sdw3 data/primary | sdw4 data/mirror | ✅ / ✅ |
| gpseg3 | sdw4 data/primary | sdw1 data/mirror | ✅ / **Mirror ❌ down** |
| gpseg4 | sdw1 data2/primary | sdw2 data2/mirror | Primary ✅ / **Mirror ❌ down** |
| gpseg5 | sdw2 data2/primary | sdw3 data2/mirror | ✅ / ✅ |
| gpseg6 | sdw3 data2/primary | sdw4 data2/mirror | ✅ / ✅ |
| gpseg7 | sdw4 data2/primary | sdw1 data2/mirror | Primary ✅ / **Mirror ❌ down** |

读这张表要知道两件事：

1. **镜像永远在"另一台机器"上** → 任何单台 Segment 机器整体宕机，数据都不丢（但会损失性能）。
2. 上表里的 **4 个 mirror 是 down 的**（gpseg0 / 3 / 4 / 7 的镜像，集中在 sdw1 和 sdw2 两台机器上）——这是本文的示例故障状态。
   意味着：**这 4 个分片只有一份数据，没有冗余**。如果 sdw1 / sdw4 上对应的 primary 再出问题，这 4 个分片的数据就不可访问。这是**最需要优先处理的隐患**。
   > 判读经验：**同一台机器上的 mirror 全部 down、别的机器全好**，多半是磁盘/目录/权限/挂载这类共性问题，不是偶发故障。只修不查根因，下次重启还会再来。

## 2.5 版本信息为什么重要

```bash
# 查版本（三条任选）
psql -d postgres -c "select version();"
gpstate -s | grep -i version
psql -d postgres -c "select * from gp_version();"
```

示例环境：

```
Greenplum Database 5.25.0
内核：PostgreSQL 8.3.23
构建时间：2020-02（x86_64 Linux）
```

⚠️ **必须知道的事**：GP 5.x 是**老分支，已停止官方支持**（现在主流是 GP 6 和 7，内核 PG 9.4/12）。
影响：

- 网上大部分新教程（`gpexpand` 新参数、`gpbackup`、资源组 `gp_resource_group`）在 5.x 上**不一定适用**，写命令前先确认版本相关语法
- 5.x **没有官方 systemd 开机自启支持** → 这是"重启后数据库没起来"的最常见原因（见 2.6）
- 5.x 与 6/7 差异较大，未来升级属于**大版本迁移**（数据导出导入级别），不能原地升

## 2.6 一次真实的停机案例（时间已脱敏）

| 时间 | 发生了什么 |
|---|---|
| D-3 22:00 | 整批机器**计划性停机**（系统 shutdown，GP 干净关闭，`postmaster.pid` 不存在） |
| D-1 09:00 | 机器重启。**GP 没有开机自启，重启后没有任何人执行 `gpstart`** |
| D-1 ~ D | 集群整体不可用（0 个 postgres 进程），业务连不上，**持续约 2 天无人发现**，监控也没覆盖 |
| D 09:30 | 登 Master 执行 `gpstate -s` 报 `Connection refused ... 5432` —— 一开始还怀疑是网络/参数问题 |
| D 09:45 | 确认：`pgrep -a postgres` 空、5432 无监听、各台机器实例数全 0 → **整集群停机** |
| D 10:00 | gpadmin 下 `gpstart`，启动清单里出现了此前漏登记的 Segment 主机，先按 N 核对拓扑再启动 |
| D 10:10 | 按 y 启动，约 3 分钟，`Successful segment starts = 12 / Failed = 0 / Skipped(标记 down) = 4` |
| D 10:20 | `gpstate -s` 确认：Master ✅、Standby passive ✅、**8 个 Primary 全部 Up** ✅ |

**排查顺序值得记住**（这就是真实故障排查的思路）：

```
连不上
 ├─ ① 我在哪台机器？（hostname -I）           → 登错机器是最常见的"故障"
 ├─ ② 本机有没有 GP 进程？（pgrep -a postgres -D）  → 空 = 停机
 ├─ ③ 本机端口有没有监听？（ss -lnt | grep 5432）
 ├─ ④ 数据目录在不在？postmaster.pid 有没有？（判断上次是干净停机还是崩溃）
 ├─ ⑤ 谁在什么时候停的？（~gpadmin/gpAdminLogs/gpstop_*.log、gpstart_*.log）
 ├─ ⑥ 机器是不是重启过？（uptime -s / who -b）→ 重启 + 无 gpstart 记录 = 本次根因
 └─ ⑦ 磁盘/内存够不够？（df -h / free -g / dmesg | grep -i oom）
```

---

# 三、启动 GP 的简洁命令

## 3.1 标准三步（在 Master 上，root 登录后执行）

```bash
su - gpadmin                      # 必须带横杠，否则环境变量不加载
source /usr/local/greenplum-db/greenplum_path.sh
export MASTER_DATA_DIRECTORY=/data/greenplum/master/gpseg-1    # 换成实际路径

gpstart                           # 交互确认输入 y；免交互用 gpstart -a
```

root 下一条命令搞定（不切会话）：

```bash
su - gpadmin -c 'source /usr/local/greenplum-db/greenplum_path.sh && \
  export MASTER_DATA_DIRECTORY=/data/greenplum/master/gpseg-1 && \
  gpstart -a'
```

> `gpstart` **不能用 root 跑**，GP 工具会主动检查运行用户；root 下必须先 `su - gpadmin`。
> 数据目录属主是 `gpadmin`、权限 700，**不要 chown / chmod 777**，否则之后启动直接失败。

## 3.2 启动过程中会看到什么

```
1. 校验环境（版本、目录、gpadmin 互信）
2. 以 admin mode 起 Master，读 catalog，拿到"应该起哪些实例"的清单
3. 关掉这个临时 Master
4. 并行启动全部 Segment + Standby Master
   → 屏幕上每秒打一个点，这是心跳不是卡死
5. 正式启动 Master，业务可以连了
```

- 全集群从零启动 **3~10 分钟都正常**，期间**不要 Ctrl+C、不要重复执行 gpstart**
- 输出里 `Skipped segment starts` = 配置里已标记 down 的实例（示例环境固定是 4 个）
- 期望看到：`Successful segment starts = 12`、`Failed segment starts = 0`
- 如果报 `Do not have enough valid segments to start the array` → **停下来**，说明配置里被标记 down 的实例太多，先查原因，别反复重试

## 3.3 启动后必须验证（只读）

```bash
gpstate -s
psql -d postgres -c "select count(*) from gp_dist_random('gp_id');"    # 应为 12（在跑实例数）
```

判据：**8 个 Primary 全部 `Up`** 才算真正启动完成；只看 Master 起来不算。
`gpstate -e` 可以确认哪些 mirror 未同步（示例环境是 gpseg0/3/4/7 的镜像）。

## 3.4 关闭（备用）

```bash
gpstop            # 交互确认；gpstop -a 免交互；gpstop -r 重启
gpstop -m         # 只停 master（维护模式，业务不可用，可改配置）
```

---

## 附录：拿到自己环境时，先替换这几处

| 文中位置 | 示例值 | 你要换成 |
|---|---|---|
| 拓扑图与机器清单 | `10.0.0.11` / `mdw` | 你的 Master IP 和主机名（`hostname -I` 确认） |
| Standby | `10.0.0.12` / `smdw` | 你的 Standby Master |
| Segment 主机 | `sdw1`~`sdw5` | 你的 Segment 机器（数量可能不同） |
| 数据根目录 | `/data/greenplum` | 实际路径，用 `ps -ef \| grep -- '-D'` 或 `gpstart_*.log` 确认 |
| 实例数 | 8 primary + 8 mirror = 16 | `select count(*) from gp_segment_configuration;` 查实际值 |
| 端口 | 40000+ | `gp_segment_configuration.port` 查实际值 |
| 版本 | GP 5.25.0 | `select version();` 查实际值（决定命令语法是否适用） |
