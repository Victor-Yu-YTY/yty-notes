# 项目记录：主机 CPU 飙升排查 SOP

> 文档用途：**下次再遇到 CPU 飙升，直接从第四节开始照做**。
> 事件时间：2026-11-10；影响范围：单台 4 核采集机，CPU 100% 累计约 50 分钟。
> 脱敏说明：文中主机名、IP、内部域名、账号等一律用占位符表示（`<HOST>` / `<HOST_IP>` / `<FE_NODE>` / `<内部域名>` / `<GRAFANA>`），请按实际环境替换。

---

## 一、事件摘要

| 项 | 内容 |
|---|---|
| 现象 | 主机 CPU 100%，告警「MySQL-CPU使用率」连续触发 |
| 第一直觉 | 以为是 MySQL 慢查询 |
| 实际原因 | **一个每分钟跑的采集脚本，文本解析耗时失控 + cron 无并发保护 → 实例叠加吃满 4 核** |
| 与 MySQL 的关系 | **无关**。全程 `mysqld` 占用 0.1% |
| 结论 | 告警没错，但根因不在告警指向的对象上 |

---

## 二、元凶与因果链

元凶：`/opt/scripts/<APP>/sr_show_processlist_all.sh`（`* * * * *` 每分钟执行）

```
① 某次 SHOW FULL PROCESSLIST 返回了「超长 Info」（超长/多行 SQL）
        ↓
② 脚本用 bash 逐行正则解析 + 字符串反复拼接（+=），复杂度 O(n²)
        ↓
③ 单次耗时从 ~1 秒膨胀到数分钟，一个 subshell 吃满 1 个核（纯 CPU，无 IO）
        ↓
④ cron 不等上一次结束，下一分钟又起一个 → 并发 1→2→3→4
        ↓
⑤ 4 核正好同时跑 4 个 → 稳定在「4 并发 / 100% CPU」，谁也不退
        ↓
⑥ 直到那条超长 SQL 从 processlist 消失，新实例恢复秒级，旧的陆续算完 → 一分钟内回落到 1%
        ↓
⑦ 约 42~44 分钟后再次出现超长 SQL → 下一波
```

**本质一句话：不是"跑得频繁"，而是"某次跑得太久"，并且 cron 会把任何一次"太久"自动复制成并发。**

修复（已上线）：解析时把 `Info` 截断到 2000 字符，切断 O(n²) 的输入规模。

---

## 三、排查思路（可复用的方法论）

### 3.1 先分层，别急着查业务

按「主机 → 进程 → 应用内部」三层往下走，每层用最少的数据否定掉大部分假设：

```
第 1 层：主机资源会不会根本不是 CPU 问题？
   看 user / system / iowait / steal 四项拆分
   ├─ user 高、iowait 0、steal 0  → 纯计算，且是本机自己的进程
   ├─ iowait 高                  → 磁盘或网络存储
   ├─ steal 高                   → 宿主机超卖，找平台侧
   └─ load 高但 CPU 不高          → D 态（锁/IO 等待），看 wchan

第 2 层：哪个进程？
   用 process_exporter 的历史数据按 groupname 排名（见 4.2）
   没有 process_exporter 就当场 ps（见 4.1）

第 3 层：这个进程在干什么？
   /proc/<pid>/ 下的状态 + 该程序的日志 + 它的调度方式（cron？systemd timer？手工？）
```

### 3.2 三个立刻能定性的判据

| 观察 | 结论 |
|---|---|
| `forks/s` 没升高、`ctxswitch/s` 持平 | 少数进程在**连续计算**，不是大量短进程/锁竞争 |
| 进程 `State: R` + `wchan=0` + fd 里没有 socket | **纯用户态忙等**，排除网络、锁、IO |
| `Tcp_CurrEstab` 不变 | 没有应用并发涌入 |
| `Cached` / 磁盘读不变 | 没在读大文件（不涉及备份、全盘扫描） |
| `intr/s` 升高 ≈ 核数 × 1000Hz | 只是 tick 副产物，**不是**设备中断风暴 |

### 3.3 这次踩过的坑（下次别重复）

1. **告警规则名会骗人**——规则叫「MySQL-CPU使用率」，实际和 MySQL 无关。规则里的 `job` 标签也可能不是你以为的那个（本次是 `consul-prod-machine`，不是业务名），按业务名筛数据会全空。
2. **进程名会骗人**——所有 shell 脚本的 `comm` 都是 `bash`；吃 CPU 的还可能是 **subshell**（fork 出来的子 shell，argv 和父进程一模一样）。只看进程名永远找不到脚本。
3. **别指望事后追溯**——本机没装 `psacct`、`auditd` 无 execve 规则、agent 不记进程快照、脚本卡住时不写日志。**这类"短命、不写日志"的进程退出后不留痕迹，必须在发生当下抓一次。**
4. **正常态基线很重要**——平时 CPU 1~2% 时看进程毫无意义，要能说清"平静时的样子"，才能识别"异常时的样子"。

---

## 四、【核心】下次 CPU 飙升的操作步骤

### 4.1 第 1 步：30 秒定性（只读）

```bash
# ① 当前负载与 CPU 拆分
uptime; vmstat 1 3

# ② 一键抓现场：进程 + 完整命令行 + 父进程（这是最关键的一条命令）
ps -eo pid,ppid,etime,pcpu,args --sort=-pcpu | head -25
```

判读：
- `us` 高、`sy` 低、`wa` 为 0 → 纯计算
- `ps` 第一名是谁，基本就锁定了方向
- **`etime` 是隐藏信息**：如果第一名刚起来几秒/几分钟，说明是这波新起的任务，而不是常驻进程

### 4.2 第 2 步：如果有 process_exporter，先查历史排名

本机已部署 process_exporter（端口 9256）。在 Grafana / Prometheus 里查：

```promql
# 哪个进程组在烧 CPU（历史任意时刻都能查，这是唯一能"事后看进程"的数据源）
topk(10, sum by(groupname)(rate(namedprocess_namegroup_cpu_seconds_total{instance="<HOST_IP>:9256"}[5m])) * 100)

# 进程组里有多少个进程（用来识别"实例叠加"）
namedprocess_namegroup_num_procs{instance="<HOST_IP>:9256", groupname="bash"}
```

> 注意：`groupname` 取的是 `comm`，所有 shell 脚本都会归到 `bash` 一类。**它能告诉你"是 bash 在烧"，但告诉不了"是哪个 .sh"**——所以要靠第 3 步。

### 4.3 第 3 步：定位到具体脚本（本 SOP 的核心动作）

```bash
# ⭐ 一条命令拿到：吃 CPU 的进程 + 完整命令行 + 谁拉起的它
{ ps -eo pid,ppid,etime,pcpu,args --sort=-pcpu | head -15; 
  echo "--- shell/脚本明细 ---";
  ps -eo pid,ppid,etime,pcpu,args | grep -E '\.sh|/bin/bash|bash -' | grep -v grep;
  echo "--- 父进程链 ---";
  for p in $(ps -eo pid,pcpu --sort=-pcpu | awk 'NR>1 && $2>30 {print $1}' | head -8); do
    pp=$(awk '{print $4}' /proc/$p/stat 2>/dev/null);
    echo "=== pid=$p ppid=$pp";
    echo "  self  : $(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)";
    echo "  parent: $(tr '\0' ' ' < /proc/$pp/cmdline 2>/dev/null)";
  done; } | tee /tmp/hot_now_$(date +%H%M%S).txt
```

**父进程怎么读**：

| parent 内容 | 含义 |
|---|---|
| `/usr/sbin/crond -n` | cron 任务 → 去查 crontab |
| `/usr/lib/systemd/systemd` | systemd 服务/定时器 → `systemctl status <unit>` |
| `-bash` / 某个 shell | 有人在会话里手跑 |
| 空 | 拉起它的进程已退出，被 reparent 到 pid 1 |

### 4.4 第 4 步：判断它是"正常态"还是"异常态"

拿到脚本后，**别急着 kill**，先回答三个问题：

```bash
# ① 它的设计周期是什么？（cron / timer / 循环）
crontab -l | grep -F '<脚本名>'
systemctl list-timers --all | grep -i '<相关关键词>'

# ② 它平时要跑多久？（对比日志里的时间戳跨度）
tail -30 /<日志路径>

# ③ 现在有几个实例在跑？（> 1 就说明在叠加）
ps -eo pid,etime,args | grep -F '<脚本名>' | grep -v grep
```

**关键判断**：如果"单次运行时长 > 调度周期"，那就是**拥塞崩塌**，不管根因是什么，先按下面的第 5 步止血。

### 4.5 第 5 步：处置（按风险从低到高）

| 措施 | 说明 |
|---|---|
| 加并发锁 | `flock -n <锁文件>` 包在 cron 行里。**一行解决叠加，让"慢"永远只能慢一个实例** |
| 加超时 | `timeout 55 <命令>`，给单次运行硬上限 |
| 限制输入规模 | 截断大字段、加分页、加 `LIMIT` |
| 等待自愈 | 如果确认是"积压排队"，它会自己排空（本次就是），不必动手 |
| kill（最后手段） | 只在确认无业务影响时做；杀之前先确认该脚本是否有半途写入（如 `TRUNCATE`），避免留下残缺数据 |

### 4.6 第 6 步：闭环

- 把现场文件 `/tmp/hot_now_*.txt` 留着
- 记录：**哪个脚本 / 谁拉起 / 单次多久 / 周期多久 / 是否叠加**
- 判断要不要改脚本或改调度

---

## 五、常用命令速查

```bash
# CPU 拆分
vmstat 1 3;  mpstat -P ALL 1 2 2>/dev/null

# 进程排名（含完整命令行）
ps -eo pid,ppid,user,etime,pcpu,args --sort=-pcpu | head -20

# 线程排名（怀疑单进程多线程时）
top -H -b -n2 -d1 -o %CPU | awk '/^ *PID/{f++} f==2' | head -25

# 某个进程到底在干什么
P=<pid>
cat /proc/$P/status | grep -E '^State|^Threads'
echo -n 'wchan='; cat /proc/$P/wchan; echo
ls -l /proc/$P/fd | head -20          # 有 socket = 在等网络；有 sh-thd = bash here-string
tr '\0' ' ' < /proc/$P/cmdline; echo

# 网络连接（确认是否在等远端）
ss -tnp 2>/dev/null | grep <目标IP> | head

# 系统侧历史（若有 sysstat）
sar -u -s <起> -e <止>; sar -q -s <起> -e <止>

# MySQL 侧（确认是不是 DB 问题）
mysql -e "SELECT LEFT(DIGEST_TEXT,100) d, COUNT_STAR n, ROUND(SUM_TIMER_WAIT/1e12,1) sec
          FROM performance_schema.events_statements_summary_by_digest
          ORDER BY SUM_TIMER_WAIT DESC LIMIT 15;"
```

---

## 六、本机的监控能力清单（决定你能查到什么）

| 数据源 | 能力 | 局限 |
|---|---|---|
| node_exporter（9100） | 主机 CPU/IO/网络/内存历史 | 看不到进程 |
| **process_exporter（9256）** | **进程组级 CPU 历史（唯一能事后看进程的）** | 只按 `comm` 分组，脚本都叫 `bash` |
| 应用与脚本日志 | 能看到"谁跑了、跑了几条、跑多久" | 卡死时不写日志 |
| crontab | 能看到调度周期与命令行 | cron 本身不记录耗时 |
| `psacct` | ❌ **未安装** | — |
| `auditd` execve | ❌ **无规则**，audit.log 长期未写 | — |
| 主机安全 agent | ❌ 日志里无进程快照 | — |

**结论：本机不具备"事后追溯进程"的能力。一旦发现 CPU 飙高，必须在发生当下执行 4.1 / 4.3 的 `ps`，否则无从追查。**

---

## 七、待办（提升下次排查效率）

| 优先级 | 事项 | 收益 |
|---|---|---|
| 高 | 安装 `psacct` 并启用 | 任何 CPU 异常，`sa \| head` 一条命令看到"哪个命令吃了多少 CPU 秒"，彻底解决事后追溯 |
| 高 | 所有 `* * * * *` 采集脚本统一加 `flock -n` + `timeout` | 从机制上杜绝"单次超时 → 实例叠加 → 4 核打满" |
| 中 | `process_exporter` 改为按 cmdline 分组 | Grafana 里直接显示脚本路径的历史 CPU 曲线 |
| 中 | 增加进程级告警（非白名单进程组 CPU >80% 持续 3 分钟） | 下次一眼看到"谁在烧" |

---

## 八、五条铁律（排查时别违反）

1. **别用 `gdb -p` / `strace -f` / `perf` 去 attach 正在吃 CPU 的业务进程**——会停住目标，代价远大于收益。
2. **别为了"复现"重跑疑似 SQL 或疑似脚本**——等于人为制造第二次故障。
3. **别 `kill` 了事**——不清根因，下一波照样来；而且可能留下残缺数据。
4. **替换正在运行的脚本，必须在同目录 `mv` 重命名，不能用 `cp` 覆盖**——覆盖会破坏正在执行该脚本的进程（bash 边读边执行）；`mv` 只换目录项，运行中的实例继续读旧内容，安全且原子。
5. **告警里的"对象名"不等于"责任方"**——先按第三、四节往下分层，再下结论。
