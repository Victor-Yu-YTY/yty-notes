# Linux 服务器常用命令手册（运维 / DBA 实战版 · v2）

> **适用对象**：日常要登上 Linux 服务器做巡检、部署、排障的运维/DBA。
> **版本说明**：v2 起每条命令都标有【风险】和【语法】；讲解与场景分段落展示。
> **执行习惯**：内部环境统一用 **root** 直连（不用 `sudo`、不用 `su`）；**先只读取证、再动配置**。
> **占位符**：`<服务器IP>`、`<unit>`（systemd 单元名）、`<端口>`、`<库名>`、`<用户>` 按实际替换。

**风险标注图例**（每条命令都有，务必先看）

| 标记 | 含义 |
| --- | --- |
| ✅ **只读** | 只看不改：不写文件、不动配置、不影响服务，随意执行 |
| ⚠️ **有影响** | 会写文件 / 改配置 / 改权限 / 启停服务 / 建删用户 / 写库等，**执行前先确认** |
| ℹ️ **视用法** | 同一条命令只读与否取决于**参数或子命令**，条目内会注明哪种用法安全 |

**目录**

- [一、远程连接与文件传输](#一远程连接与文件传输)
- [二、系统与服务管理（systemd）](#二系统与服务管理systemd)
- [三、进程与资源](#三进程与资源)
- [四、网络与端口](#四网络与端口)
- [五、防火墙与访问收敛](#五防火墙与访问收敛)
- [六、文件与目录](#六文件与目录)
- [七、查看文件内容](#七查看文件内容)
- [八、文本搜索与处理](#八文本搜索与处理)
- [九、磁盘与容量](#九磁盘与容量)
- [十、用户与权限](#十用户与权限)
- [十一、压缩与备份](#十一压缩与备份)
- [十二、定时任务（cron）](#十二定时任务cron)
- [十三、软件包管理](#十三软件包管理)
- [十四、Docker 容器](#十四docker-容器)
- [十五、数据库常用命令](#十五数据库常用命令)
- [十六、常用"一条命令看全身"套路](#十六常用一条命令看全身套路)

---

## 一、远程连接与文件传输

### ssh —— 远程登录服务器

**风险**：✅ 只读（仅登录，不执行远程命令时）

**语法**：`ssh [选项] [用户@]主机 [远程命令]`

```bash
ssh root@172.16.2.250
```

**讲解**：用 SSH 协议登录远端主机的命令行界面，加密传输。登录后可执行任意命令，退出用 `exit`。

**场景**：日常登服务器做巡检、排障；`ssh -p <非22端口> user@ip` 连自定义端口。

### ssh-keygen / ssh-copy-id —— 生成密钥对，做免密登录

**风险**：⚠️ 有影响（生成密钥文件、修改目标机 `~/.ssh/authorized_keys`）

**语法**：`ssh-keygen [-t 类型] [-C 注释]`、`ssh-copy-id [选项] 用户@主机`

```bash
ssh-keygen -t ed25519 -C "ops@example"        # 一路回车
ssh-copy-id root@172.16.2.250                  # 把公钥推到目标机
```

**讲解**：生成公钥/私钥对，把公钥放到目标机 `~/.ssh/authorized_keys` 后，登录免输密码。

**场景**：脚本里要自动 `scp`/`ssh`（如每日备份中转），必须免密；一次配置，后面所有自动化都受益。

### scp —— 本地和服务器互传文件

**风险**：⚠️ 有影响（会写入目标位置的文件）

**语法**：`scp [选项] 源 目标`，源/目标可以是 `用户@主机:路径`

```bash
scp local.tar.gz root@172.16.2.250:/tmp/          # 本机 → 服务器
scp root@172.16.2.250:/tmp/dump.sql ./            # 服务器 → 本机
scp -P 2222 -r dir root@host:/data/               # 自定义端口、传目录
```

**讲解**：基于 SSH 的文件复制，`本机路径 远端:路径` 的方向看参数顺序。传目录加 `-r`。

**场景**：把打好的包传上去部署、把备份文件拉下来存档、传 SQL 脚本进数据库机。

### rsync —— 增量同步（比 scp 更适合大量/重复传输）

**风险**：⚠️ 有影响（写目标目录；带 `--delete` 会删除目标端多余文件）

**语法**：`rsync [选项] 源 目标`

```bash
rsync -avz --progress /data/backup/ root@172.16.10.205:/data/backup/
rsync -avz --delete src/ user@host:/dst/    # 目标端多出的文件也会被删（慎用）
```

**讲解**：只传"有变化的部分"，断点续传、可排除文件，传输进度可见。源路径结尾 `/` 表示"把目录里的内容同步过去"，不带 `/` 表示"连目录本身一起同步"。

**场景**：备份文件每天往中转机/灾备机同步；日志目录归档；比每次全量 `scp` 省带宽省时间。

### telnet —— 探测某个端口通不通（老式但直观）

**风险**：✅ 只读（仅建立 TCP 连接探测）

**语法**：`telnet 主机 [端口]`

```bash
telnet 172.16.2.250 8020
```

**讲解**：能连上会显示 `Connected`（或进入会话界面）；连不上会 `Connection refused`（服务没在听）或一直挂起（被防火墙丢包）。

**场景**：快速确认"对端服务端口在不在"。注意：很多新系统默认没装 telnet，可改用 `nc`。

### nc -zv —— 更干净的端口连通性测试

**风险**：✅ 只读（只探测，不发送数据）

**语法**：`nc [-z] [-v] [-w 超时秒] 主机 端口`

```bash
nc -zv -w3 172.16.2.250 8020
```

**讲解**：`-z` 只测连接不发数据，`-v` 显示结果，`-w3` 超时 3 秒。成功打印 `succeeded`，失败打印超时/拒绝。

**场景**：验证服务端口可达性；**做"端口被收敛后外部是否打不通"的负向验证时，必须在另一台机器上跑**（在服务器本机跑测不出拦截效果）。

---

## 二、系统与服务管理（systemd）

### systemctl status —— 查看服务状态

**风险**：✅ 只读

**语法**：`systemctl status [单元名]`

```bash
systemctl status dba-mcp
```

**讲解**：看服务是否 `active (running)`、主 PID、最近几条日志、启动失败原因。`q` 退出。

**场景**：服务"起不来/挂了"的第一条命令；确认部署后进程是否真的活着。

### systemctl start / stop / restart —— 启停、重启服务

**风险**：⚠️ 有影响（启动/停止/重启服务进程）

**语法**：`systemctl {start|stop|restart} 单元名`

```bash
systemctl start   dba-mcp      # 启动
systemctl stop    dba-mcp      # 停止
systemctl restart dba-mcp      # 重启（改了配置/换镜像后）
```

**讲解**：按单元名控制服务生命周期，`restart` = stop + start。

**场景**：部署后重启服务；改完配置让服务重新加载；服务卡死时先 `restart` 再观察。

### systemctl enable / disable / is-enabled —— 开机自启

**风险**：ℹ️ 视用法（`enable`/`disable` ⚠️ 有影响，`is-enabled` ✅ 只读）

**语法**：`systemctl {enable|disable} 单元名`、`systemctl is-enabled 单元名`

```bash
systemctl enable  dba-mcp      # 开机自启
systemctl disable dba-mcp      # 取消开机自启
systemctl is-enabled dba-mcp   # 查是否已设为自启
```

**讲解**：enable 建立开机启动链接（`/etc/systemd/system/multi-user.target.wants/` 下），disable 删除。

**场景**：新部署的服务必须 `enable`，否则机器重启后服务不会自动起来（这是很多"重启后服务全挂"事故的根源）。

### systemctl is-active —— 脚本里判断服务是否在跑

**风险**：✅ 只读

**语法**：`systemctl is-active 单元名`

```bash
systemctl is-active dba-mcp
```

**讲解**：只输出一行 `active` / `inactive` / `failed`，退出码 0/3/其它，适合被脚本用 `if` 判断。

**场景**：部署脚本、巡检脚本里判断服务状态；`if [ "$(systemctl is-active x)" = "active" ]`。

### systemctl daemon-reload —— 改完 unit 文件后必须执行

**风险**：⚠️ 有影响（重新加载 systemd 配置，不重启服务）

**语法**：`systemctl daemon-reload`

```bash
systemctl daemon-reload
```

**讲解**：让 systemd 重新读取所有 unit/配置文件。**不执行它，新写的 `.service`、drop-in 配置不会生效**。

**场景**：新建/修改 `/etc/systemd/system/xxx.service` 或 `xxx.service.d/*.conf` 之后、`restart` 之前，先 `daemon-reload`。

### systemctl show -p —— 查服务运行参数（含安全白名单）

**风险**：✅ 只读

**语法**：`systemctl show 单元名 [-p 属性名 ...]`

```bash
systemctl show dba-mcp -p IPAddressAllow -p IPAddressDeny
```

**讲解**：按属性名打印服务实际的生效参数，比看配置文件更可靠（能确认 `daemon-reload` 后是否真的生效）。

**场景**：做完 systemd 网络白名单后核对 `IPAddressAllow`/`IPAddressDeny` 是否配对正确。

### service —— 老式服务管理（CentOS 7 之前 / init.d 脚本）

**风险**：ℹ️ 视用法（`status` ✅ 只读；`start`/`stop` ⚠️ 有影响）

**语法**：`service 服务名 {start|stop|restart|status}`

```bash
service mysql stop
service mysql start
service mysql status
```

**讲解**：走 `/etc/init.d/<name>` 脚本的兼容入口。很多"二进制安装的 MySQL"就是用 `init.d + mysqld_safe` 托管的。

**场景**：管理没有 systemd unit 的老服务；`systemctl` 找不到单元时先 `ls /etc/init.d/` 看看。

### journalctl -u —— 看服务自己的日志

**风险**：✅ 只读

**语法**：`journalctl [选项] [-u 单元名]`（`-f` 实时、`-n 行数`、`--since 时间`）

```bash
journalctl -u dba-mcp -f                 # 实时跟踪
journalctl -u dba-mcp --since "1 hour ago"
journalctl -u dba-mcp -n 100 --no-pager  # 最近 100 行
```

**讲解**：systemd 统一管理的日志（存在 journald 里），`-u` 指定服务，`-f` 实时跟，`--since`/`-n` 限时间/行数。

**场景**：服务起不来、报错，先看它自己打印了什么；迁移/部署时盯 `Listening on :3000` 之类的启动标志。

---

## 三、进程与资源

### ps -ef —— 列出所有进程

**风险**：✅ 只读

**语法**：`ps [-e|-ef|aux]`

```bash
ps -ef | grep '[m]ysqld'
```

**讲解**：`-e` 所有进程，`-f` 全格式（UID、PID、PPID、命令）。`grep '[m]ysqld'` 的方括号技巧能自动排除 grep 本身。

**场景**：确认某进程在不在跑、谁启动了它、跑多久了；`ps -ef | grep java` 找业务进程。

### kill / pkill —— 结束进程

**风险**：⚠️ 有影响（终止进程，业务进程可能丢数据）

**语法**：`kill [-信号] PID...`、`pkill [-f] 模式`

```bash
kill 10869               # 按 PID 发 SIGTERM（礼貌地请求退出）
kill -9 10869            # 强杀（进程不响应 SIGTERM 时最后手段）
pkill -f 'java.*order'   # 按命令行匹配批量杀
```

**讲解**：`kill` 默认发 15 号信号（SIGTERM，进程能收尾），`-9` 是 SIGKILL 强制杀。`pkill -f` 按完整命令行匹配。

**场景**：旧进程残留占端口、测试进程清场。**注意**：数据库/服务进程别上来就 `-9`，先 `kill` 给机会优雅退出。

### top —— 实时看资源占用排行

**风险**：✅ 只读

**语法**：`top [选项]`（`-b -n1` 脚本快照、`-p PID` 盯进程）

```bash
top
top -b -n1 | head -20          # 脚本里取一次快照
top -p 10869                   # 只盯一个进程
```

**讲解**：CPU/内存占用排序实时刷新。看懂三行头就够：`load average`（1/5/15 分钟平均负载）、`%Cpu(s)`、`KiB Mem`。

**场景**：机器卡、接口慢，先 `top` 看谁吃 CPU/内存；配合 `top -Hp <PID>` 看线程级占用。

### free -h —— 看内存与交换分区

**风险**：✅ 只读

**语法**：`free [-h]`

```bash
free -h
```

**讲解**：按人类可读单位显示内存总量/已用/可用。重点看 `available` 列（真正还能用的），`buff/cache` 大不是坏事，Linux 会用空闲内存做缓存。

**场景**：排查"内存不够/被 OOM 杀"（如备份进程被内核 kill 的复盘场景）；看 swap 是否被大量使用。

### uptime —— 看负载与运行时长

**风险**：✅ 只读

**语法**：`uptime`

```bash
uptime
```

**讲解**：输出当前时间、开机时长、1/5/15 分钟平均负载。负载 > CPU 核数说明在做"排队"。

**场景**：快速判断机器是否健康；重启后确认机器起来了多久。

### vmstat —— 看系统整体资源曲线（CPU/内存/换页/IO）

**风险**：✅ 只读

**语法**：`vmstat [间隔] [次数]`

```bash
vmstat 1 5
```

**讲解**：每秒采一次、共 5 次。看 `r`（运行队列，>核数说明 CPU 排队）、`si/so`（换页，持续非 0 说明内存吃紧）。

**场景**：怀疑"机器整体变慢但说不清哪慢"时先跑一遍；配合 `iostat` 区分是 CPU 还是磁盘瓶颈。

### iostat -x —— 看磁盘 IO 压力

**风险**：✅ 只读

**语法**：`iostat [-x] [间隔] [次数]`

```bash
iostat -x 1 5
```

**讲解**：`-x` 显示扩展指标：`%util`（设备繁忙度，接近 100% 说明磁盘打满）、`await`（平均等待毫秒）。

**场景**：数据库/备份变慢排查；大文件 `tar` 打包时看磁盘是否成为瓶颈。

### lsof —— 看某个文件/端口被谁占着

**风险**：✅ 只读

**语法**：`lsof [选项] [名称|路径|:端口]`

```bash
lsof -i:8020                 # 谁在监听/连接 8020 端口
lsof -p 10869 | grep log     # 某进程打开了哪些日志文件
lsof +D /data/outline        # 谁占着这个目录里的文件
```

**讲解**：ls 是"看文件"，lsof 是"看打开关系"（文件被哪个进程打开、端口被谁占用）。

**场景**：端口被占却找不到进程、`umount` 时提示"设备忙"、删了文件磁盘空间没释放（被进程占用）。

---

## 四、网络与端口

### ss -lntp —— 看本机监听了哪些端口（最常用）

**风险**：✅ 只读

**语法**：`ss [-lntp]`（`-l` 监听、`-n` 数字、`-t` TCP、`-p` 进程）

```bash
ss -lntp
ss -lntp | grep -E ':(8000|8020)\b'
```

**讲解**：`-l` 只看监听、`-n` 显示数字端口、`-t` TCP、`-p` 显示进程。能直接看到 `0.0.0.0:8020` 由哪个 PID/进程监听。

**场景**：部署后确认服务监听端口、绑定地址（`0.0.0.0` 还是 `127.0.0.1`）；排查端口冲突。

### ss -tnp —— 看已建立的连接

**风险**：✅ 只读

**语法**：`ss -tnp [过滤]`（`-s` 连接数汇总）

```bash
ss -tnp | grep 3306
ss -s                      # 连接数汇总概览
```

**讲解**：`-t` TCP、`-n` 数字、`-p` 带进程；已建立连接（ESTABLISHED）列表。

**场景**：看谁在连数据库、业务机到 DB 的长连接有多少、是否有大量异常连接堆积。

### netstat -tunlp —— 老牌端口/连接查看（部分新系统已不预装）

**风险**：✅ 只读

**语法**：`netstat [-tunlp]`

```bash
netstat -tunlp | grep 3306
```

**讲解**：`t` TCP、`u` UDP、`n` 数字、`l` 监听、`p` 进程。功能与 `ss` 类似，CentOS 7+ 默认可能没有。

**场景**：老系统排障；脚本里习惯性用它（新机器优先用 `ss`）。

### curl —— 验证 HTTP 服务是否健康

**风险**：ℹ️ 视用法（GET/HEAD ✅ 只读；POST/PUT/DELETE 等会改服务端数据 ⚠️ 有影响）

**语法**：`curl [选项] URL`（`-s` 静默、`-o` 输出、`-w` 状态码、`-H` 头、`-X` 方法、`-d` 数据）

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/healthz
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/xxx
```

**讲解**：`-s` 静默（不显示进度条）、`-o /dev/null` 丢弃响应体、`-w '%{http_code}'` 只打印状态码。带 `-H` 可以加请求头。

**场景**：部署自检（期望 200）、探活接口、验证鉴权是否生效。**注意**：在本机 `curl` 自己的公网 IP 测不出端口收敛效果，要换另一台机器测。

### wget —— 下载文件（服务器上常用）

**风险**：⚠️ 有影响（会写本地文件）

**语法**：`wget [选项] URL`（`-c` 续传、`-O` 保存名、`-T` 超时、`-t` 重试）

```bash
wget -c https://example.com/mysql-8.4.tar.xz
wget -c -T 30 -t 3 -O pkg.tar.xz https://example.com/pkg.tar.xz
```

**讲解**：`-c` 断点续传、`-T` 超时、`-t` 重试次数、`-O` 指定保存名。加 `nohup ... &` 可后台下载。

**场景**：服务器上拉安装包、拉镜像 tar 包；网速慢时后台下载用 `tail -f wget.log` 看进度。

### ping —— 测网络通不通

**风险**：✅ 只读

**语法**：`ping [-c 次数] 主机`

```bash
ping -c 4 172.16.2.250
```

**讲解**：发 ICMP 探测包，`-c 4` 只发 4 个。能通说明网络层可达；不通可能是防火墙丢 ICMP 或真断网。

**场景**：判断"连不上"是网络问题还是服务问题；ping 通但端口连不上 → 多半是服务没起或防火墙拦端口。

### ip a / ip route / hostname -I —— 看本机 IP 与路由

**风险**：✅ 只读

**语法**：`ip {a|route|...}`、`hostname [-I]`

```bash
ip a                # 看所有网卡/IP
ip route            # 看默认网关
hostname -I         # 一行输出本机所有 IPv4 地址
```

**讲解**：`ip` 是新版网络配置工具（替代 ifconfig）。`ip a` 看地址，`ip route` 看路由表，`hostname -I` 找自己 IP 最快。

**场景**：确认机器绑定的 IP 对不对、走哪块网卡出网、多网卡环境排查。

---

## 五、防火墙与访问收敛

### firewall-cmd —— firewalld 管理端口（RHEL/CentOS/Rocky 系）

**风险**：ℹ️ 视用法（`--list-*` ✅ 只读；`--add-port`/`--reload`/`--remove-port` ⚠️ 有影响）

**语法**：`firewall-cmd [--list-ports|--add-port=端口/协议 [--permanent]|--reload|--list-all|...]`

```bash
firewall-cmd --list-ports                          # 已放行的端口
firewall-cmd --add-port=8020/tcp --permanent       # 放行 8020（--permanent 永久）
firewall-cmd --reload                              # 重载规则
firewall-cmd --list-all                            # 看当前 zone 全部规则
```

**讲解**：`--add-port` 加规则、`--permanent` 写入持久配置、`--reload` 让规则生效。不加 `--permanent` 重启服务就丢。

**场景**：新服务端口要对外开放时；排查"端口听着但外部连不上"是不是被防火墙拦了。**注意**：很多服务器 `firewalld` 根本没启动（inactive），此时 `firewall-cmd` 命令会报错，说明这台机器不用它管。

### iptables —— 底层防火墙规则

**风险**：ℹ️ 视用法（`iptables -L -n -v` ✅ 只读；`-I`/`-A`/`-D` 插入/追加/删除规则 ⚠️ 有影响）

**语法**：`iptables [-t 表] {-L|-I 链 规则...|-A 链 规则...}`

```bash
iptables -L -n -v                                 # 看现规则
iptables -I INPUT -p tcp --dport 8020 -j ACCEPT   # 临时放行（重启失效）
iptables-save > /etc/iptables/rules.v4            # 保存（不同发行版路径不同）
```

**讲解**：直接操作内核 netfilter 规则链，`-I` 插到链首、`-j ACCEPT` 放行。规则默认重启即失效，要额外做持久化。

**场景**：firewalld 没装/被禁用时的端口放行；排查规则冲突。**注意**：`iptables-nft` 混用时要看清是不是同一套规则。

### nft —— 新的防火墙框架

**风险**：ℹ️ 视用法（`nft list ruleset` ✅ 只读；`nft add/delete` ⚠️ 有影响）

**语法**：`nft {list|add|delete|...} [规则...]`

```bash
nft list ruleset              # 看全部规则
nft add rule inet filter input tcp dport 8020 accept   # 示例：放行（需配合 table/chain）
```

**讲解**：nftables 是 iptables 的后继方案，规则以"表/链/规则"组织。

**场景**：较新的发行版；与 systemd 的 `IPAddressAllow` 配合做来源收敛。

### ufw —— Ubuntu 系简易防火墙

**风险**：ℹ️ 视用法（`ufw status` ✅ 只读；`ufw allow`/`deny`/`reload` ⚠️ 有影响）

**语法**：`ufw [status|allow|deny|reload|...]`

```bash
ufw status              # 状态与规则
ufw allow 8020/tcp      # 放行
ufw reload              # 重载
```

**讲解**：`ufw` 是 iptables 的简化封装，命令语义直白。

**场景**：Debian/Ubuntu 机器上的端口放行。

### systemd IPAddressAllow —— 不改监听地址，按来源 IP 收敛访问

**风险**：⚠️ 有影响（写 drop-in 配置文件、重启服务；配置错会切断连出连入）

**语法**：在 `<unit>.d/10-allowlist.conf` 中写 `IPAddressAllow=...` / `IPAddressDeny=any`，然后 `systemctl daemon-reload && systemctl restart <unit>`

```bash
UNIT=<unit>
mkdir -p /etc/systemd/system/${UNIT}.d
cat > /etc/systemd/system/${UNIT}.d/10-allowlist.conf <<'EOF'
[Service]
IPAddressAllow=<网关IP> 127.0.0.1 <出站依赖IP...>
IPAddressDeny=any
EOF
systemctl daemon-reload
systemctl restart ${UNIT}
systemctl show ${UNIT} -p IPAddressAllow -p IPAddressDeny   # 校验
```

**讲解**：用 systemd 的 drop-in 配置，把服务进程的**入站来源**收敛到白名单（`IPAddressAllow`），其余全丢（`IPAddressDeny=any`）。`IPAddressAllow` 是**双向**的，出站依赖（数据库、认证服务）必须也写进去。

**场景**：服务器上没有 firewalld/iptables，又不想改 `--host 0.0.0.0` 监听；只让内部网关 + 本机回环能连后端服务。回滚：删掉 `10-allowlist.conf` → `daemon-reload` → `restart`。

---

## 六、文件与目录

### pwd / ls -l —— 我在哪、目录里有什么

**风险**：✅ 只读

**语法**：`pwd`、`ls [选项] [路径]`（`-l` 长格式、`-h` 人类可读、`-t` 按时间）

```bash
pwd
ls -lh            # 人类可读大小、权限、属主
ls -lht           # 按修改时间倒序
```

**讲解**：`pwd` 显示当前目录绝对路径；`ls -lh` 的 `h` 是把字节变成 K/M/G，`t` 按时间排序。

**场景**：登机器先确认自己在哪；找最近改过的文件。

### cd —— 切换目录

**风险**：✅ 只读（只改变当前 shell 的工作目录）

**语法**：`cd [目录]`（`cd ..` 上级、`cd -` 回到上次目录）

```bash
cd /data/outline
cd ..            # 上一级
cd -             # 回到上一次所在目录
```

**讲解**：切换工作目录；`cd -` 在两个目录间快速来回跳。

**场景**：进项目目录、回上级目录、在两个常用目录间跳。

### mkdir -p —— 建目录（自动建父目录）

**风险**：⚠️ 有影响（创建目录）

**语法**：`mkdir [-p] 目录...`

```bash
mkdir -p /tmp/pg-backup-docker/outline-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p /etc/systemd/system/${UNIT}.d
```

**讲解**：`-p` 父目录不存在就一起建，目录已存在也不报错。

**场景**：建备份目录（带时间戳）、建 systemd drop-in 目录、脚本里建日志目录。

### cp / mv —— 复制、移动（改名）

**风险**：⚠️ 有影响（写/移动/覆盖文件）

**语法**：`cp [选项] 源 目标`、`mv [选项] 源 目标`

```bash
cp -a /data/outline/.env /data/outline/.env.bak-$(date +%Y%m%d)
mv old.conf new.conf          # 改名
mv /tmp/pkg.tar.gz /data/install_pkgs/
```

**讲解**：`cp -a` 保留权限/时间戳（备份配置常用），`mv` 同分区内是改名/移动。

**场景**：改配置前先备份一份（`.bak-时间戳`）；把下载的包挪到归档目录。

### ln -s —— 建软链接

**风险**：⚠️ 有影响（创建链接文件）

**语法**：`ln [-s] 目标 链接名`

```bash
ln -s /data/mysql/bin/mysql /usr/local/bin/mysql
```

**讲解**：建"快捷方式"，`-s` 是软链接（指向路径，目标删了链接就断）。`ls -l` 可看指向。

**场景**：把新版本程序挂到 PATH 里、挂载点目录、多版本切换（切链接指向）。

### touch —— 建空文件 / 更新时间戳

**风险**：⚠️ 有影响（创建或更新时间戳）

**语法**：`touch 文件...`

```bash
touch /tmp/lock.flag
```

**讲解**：文件不存在就建空文件，存在就把修改时间更新为当前。

**场景**：脚本里做标记文件、做简单锁；`ls -l` 看文件是否更新过。

### find —— 按条件找文件

**风险**：ℹ️ 视用法（纯筛选 ✅ 只读；带 `-delete` 删除、`-exec rm` 等 ⚠️ 有影响）

**语法**：`find 起始路径 [条件...] [-exec 命令 {} \;] [-delete]`

```bash
find /data -name "*.log" -mtime +7                 # 7 天前的日志
find / -type f -size +1G 2>/dev/null               # 大于 1G 的文件
find /data -name "*.tar.gz" -mtime +3 -delete      # 清理（先去掉 -delete 预览）
find . -name "*.sh" -exec chmod +x {} \;           # 找到后执行命令
```

**讲解**：`-name` 名字、`-mtime` 修改天数、`-size` 大小、`-exec` 对结果执行命令（`{}` 代表文件、`\;` 结尾）。

**场景**：清理旧日志、找大文件占盘、批量改权限。**删除类操作先把 `-delete` 去掉跑一遍看结果**。

### which / command -v —— 命令到底在哪

**风险**：✅ 只读

**语法**：`which 命令...`、`command -v 命令...`

```bash
which mysql
command -v firewall-cmd iptables nft ufw
```

**讲解**：`which` 查命令可执行文件路径；`command -v` 是 POSIX 标准写法，一次查多个、有输出即存在。

**场景**：判断某个工具装了没有（如前置检查"有没有防火墙工具"）；排查 PATH 里用的是哪个版本。

---

## 七、查看文件内容

### cat —— 直接打印文件内容

**风险**：✅ 只读

**语法**：`cat 文件...`

```bash
cat /etc/os-release
cat config.yaml
```

**讲解**：把文件内容原样输出到终端。适合小文件、确认配置。

**场景**：看小配置文件、系统版本标识；大文件别用 `cat`（刷屏），用 `less`。

### less —— 分页查看大文件（推荐）

**风险**：✅ 只读

**语法**：`less [选项] [文件]`（`+F` 进入实时跟踪）

```bash
less /var/log/messages
less +F /var/log/cron        # 相当于 tail -f，Ctrl+C 退出跟随后
```

**讲解**：一次一屏，上下翻页；`/关键字` 搜索、`q` 退出、`G` 跳末尾。`+F` 进入实时跟踪模式。

**场景**：看大日志时首选；`+F` 当实时日志用（要退出跟随按 `Ctrl+C`）。

### head -n —— 看文件开头

**风险**：✅ 只读

**语法**：`head [-n N] 文件...`

```bash
head -n 5 /data/backup.sql
```

**讲解**：只打印前 N 行（默认 10 行）。

**场景**：确认 CSV 表头、快速看文件格式、大文件只关心开头的场景。

### tail -n —— 看文件结尾

**风险**：✅ 只读

**语法**：`tail [-n N] 文件...`

```bash
tail -n 50 /var/log/messages
```

**讲解**：只打印最后 N 行。

**场景**：日志最新部分在末尾；确认备份/任务最后是否成功（如 `tail` 看到 `dump complete`）。

### tail -f —— 实时跟踪日志（排障必备）

**风险**：✅ 只读

**语法**：`tail [-f] [-n N] 文件...`

```bash
tail -f /opt/scripts/greenplum/logs/gp_conn_alert_mail.log
tail -f -n 100 /data/outline/outline.log
```

**讲解**：`-f` 让文件追加内容实时刷出来，`Ctrl+C` 退出。`-n 100` 先显示最后 100 行再跟。

**场景**：部署/迁移时盯日志（如等 `Listening on :3000`）；反复跑的任务看执行过程。

### vim —— 服务器上编辑文件的编辑器

**风险**：⚠️ 有影响（编辑并保存文件内容）

**语法**：`vim [文件]`

```bash
vim /etc/my.cnf
```

**讲解**：打开文件编辑。基础操作：`i` 进入插入、`Esc` 退出插入、`:wq` 保存退出、`:q!` 不保存强退、`/关键字` 搜索、`dd` 删一行、`u` 撤销。

**场景**：现场改配置（先 `cp` 备份再改）；`sed` 不适合做复杂编辑时用 vim。

---

## 八、文本搜索与处理

### grep —— 在文本里找内容（服务器上出现频率最高的命令之一）

**风险**：✅ 只读

**语法**：`grep [选项] 模式 [文件...]`（`-n` 行号、`-c` 计数、`-v` 反向、`-E` 扩展正则、`-r` 递归、`-i` 忽略大小写）

```bash
grep 'error' app.log                    # 普通匹配
grep -n 'Timeout' app.log               # 带行号
grep -c '^GATEWAY_TOKEN=' /etc/dba-mcp/dba-mcp.env   # 只数条数
grep -v 'DEBUG' app.log                 # 排除某模式
grep -E ':(8000|8020)\b' ss-out.txt     # 扩展正则
grep -r 'DATABASE_URL' /data/outline/   # 递归查目录
grep -i 'error' app.log                 # 忽略大小写
```

**讲解**：`-n` 行号、`-c` 计数、`-v` 反向（排除）、`-E` 扩展正则、`-r` 目录递归、`-i` 忽略大小写。

**场景**：从日志里捞报错、从配置里找某个键、从命令输出里筛目标行；`grep -c` 做"条数校验"（如期望 1 条）。

### grep -oP —— 只提取匹配的部分（GNU grep）

**风险**：✅ 只读

**语法**：`grep -oP '正则' [文件]`（`-o` 只输出匹配、`-P` Perl 正则）

```bash
grep -oP '(?<=X_SQL_REVIEW_MCP_TOKEN=).*' /etc/sql-review-mcp/sql-review-mcp.env
```

**讲解**：`-o` 只输出匹配片段、`-P` 用 Perl 正则（支持环视等高级语法）。

**场景**：从配置行里精确抠出 token/密码值（注意输出别贴进聊天记录）。

### sed -i —— 批量/原地改文本（改配置神器）

**风险**：ℹ️ 视用法（不带 `-i` 只读打印 ✅；带 `-i` 直接改文件 ⚠️ 有影响，先备份）

**语法**：`sed [选项] '脚本' [文件]`（`-i` 原地修改、`s/旧/新/g` 替换、`-n 'N,Mp'` 打印区间）

```bash
sed -i 's/0.87/1.12/g' docker-compose.yml          # 全局替换
sed -i 's/\r$//' apply_patch.py                    # 清 Windows 换行（CRLF）
sed -i 's/^#*\s*enabled=.*/enabled=true/' x.conf   # 改某行的值
sed -n '10,20p' app.log                            # 只看 10-20 行（不带 -i）
```

**讲解**：`-i` 直接改文件（**先 `cp` 备份！**），`s/旧/新/g` 做替换，`\r$` 表示行尾的 CRLF 换行符，`-n 'N,Mp'` 打印指定行区间。

**场景**：改 compose 里的镜像标签（改完 `grep` 复核）、清 CRLF 解决脚本"报 `\r: command not found`"、脚本里批量改配置。

### awk —— 按列取数据/做简单统计

**风险**：✅ 只读（仅查询统计；除非把输出重定向写文件）

**语法**：`awk [-F 分隔符] '程序' [文件...]`

```bash
awk -F: '{print $1}' /etc/passwd              # 按冒号分列，取第 1 列
awk -F'\t' '{print $2, $3}' file.tsv          # 制表符分列
awk '{sum+=$1} END{print sum}' numbers.txt    # 求和
awk 'NR==1' file.txt                          # 只打印第 1 行
```

**讲解**：`-F` 指定分隔符（默认空格），`$N` 取第 N 列，`NR` 是行号，`END{}` 在最后统计。

**场景**：解析 `ps`/`ss` 输出取列、统计文件里某字段总和、取配置文件某个值。

### cut —— 按字符/字段截取

**风险**：✅ 只读

**语法**：`cut {-d 分隔符 -f 字段}|{-c 字符区间} [文件...]`

```bash
cut -d: -f1 /etc/passwd     # 按分隔符取字段
cut -c1-20 file.txt         # 取每行前 20 个字符
```

**讲解**：`-d` 分隔符、`-f` 第几段、`-c` 字符区间。只按固定分隔取列，逻辑比 awk 简单。

**场景**：日志里截时间戳、取某字段；简单场景 `cut` 比 `awk` 快。

### sort —— 排序

**风险**：✅ 只读

**语法**：`sort [选项] [文件...]`（`-t` 分隔符、`-k` 键、`-n` 数字、`-r` 倒序、`-u` 去重）

```bash
sort -t: -k2 -n /etc/passwd     # 按第 2 列数字排序
sort -u file.txt                # 排序并去重
sort -rn numbers.txt            # 数字倒序
```

**讲解**：`-t` 分隔符、`-k` 排序键、`-n` 按数字、`-r` 倒序、`-u` 去重。

**场景**：按大小/时间排序、排完配合 `head` 取 Top N、日志去重统计。

### uniq -c —— 去重并计数

**风险**：✅ 只读

**语法**：`uniq [-c] [文件]`（配合 `sort` 使用）

```bash
cat access.log | awk '{print $1}' | sort | uniq -c | sort -rn | head -10
```

**讲解**：去掉相邻重复行并计数（所以通常先 `sort` 再 `uniq -c`），最后 `sort -rn` 按次数倒序取 Top。

**场景**：统计访问日志里每个 IP 的访问次数、每类错误出现次数；经典"Top 10 分析"流水线。

### wc -l —— 数行数/字数

**风险**：✅ 只读

**语法**：`wc [-l|-w|-c] [文件...]`

```bash
wc -l app.log
grep -c 'error' app.log
```

**讲解**：`-l` 行数（`-w` 词数、`-c` 字节数）。`grep -c` 直接对匹配行计数。

**场景**：确认日志有没有增长、统计迁移条数（如"63 条迁移逐条过"）、校验导出文件行数。

### xargs —— 把管道结果变成命令参数

**风险**：ℹ️ 视用法（后面的命令决定影响：`xargs ls` ✅ 只读，`xargs rm` ⚠️ 有影响）

**语法**：`xargs [选项] 命令`（`-n N` 每次几个、`-I {}` 占位符）

```bash
find /data -name "*.log" | xargs -n1 ls -lh
find . -name "*.sh" | xargs -I{} chmod +x {}
echo 'a b c' | xargs -n1 echo
```

**讲解**：把上一条命令的输出"拼接"成下一条命令的参数。`-n1` 每次传 1 个、`-I{}` 指定占位符（适合参数需放中间的场景）。

**场景**：批量对 `find` 结果执行操作、批量改权限/查询；管道和循环之间的桥梁。

---

## 九、磁盘与容量

### df -h —— 看磁盘分区还剩多少空间

**风险**：✅ 只读

**语法**：`df [-h] [路径]`

```bash
df -h
df -h /tmp /data /root      # 只看指定目录所在分区
```

**讲解**：`-h` 人类可读。重点看 `Use%` 列。**任何"写大文件"前先看一眼余量**（如备份前）。

**场景**：部署/备份前检查余量；排查"磁盘满了"导致服务异常；确认 `/tmp` 是不是独立分区。

### du -sh —— 看某个目录到底占多少

**风险**：✅ 只读

**语法**：`du [-s] [-h] [目录...]`（`sort -rh` 排序取 Top）

```bash
du -sh /var/lib/outline/data       # 看单个目录总大小
du -sh /data/* | sort -rh | head   # 找最占地方的东西
du -sh *                          # 当前目录下各项大小
```

**讲解**：`-s` 只汇总不列明细、`-h` 人类可读。`sort -rh` 按大小倒序排，`head` 取 Top N。

**场景**：确认某个服务数据卷大小；清理前找出大目录；备份时评估打包体积（如附件卷 25G 级）。

### lsblk —— 看块设备（盘和分区）

**风险**：✅ 只读

**语法**：`lsblk [-f]`

```bash
lsblk
lsblk -f          # 带文件系统类型与挂载点
```

**讲解**：以树形列出磁盘、分区、挂载点，`-f` 额外显示文件系统类型和 UUID。

**场景**：新加的盘该不该分区/挂载、排查"数据盘在哪、挂没挂上"。

### mount / umount —— 挂载/卸载磁盘

**风险**：⚠️ 有影响（改变挂载状态，误操作会让服务访问不到数据）

**语法**：`mount [设备] [挂载点]`、`umount 挂载点|设备`、`findmnt [路径]`

```bash
mount /dev/sdb1 /data
umount /data
findmnt /data          # 确认挂载情况
```

**讲解**：把分区挂到目录下才能访问；`umount` 前确保没有进程在用（否则报 "device is busy"，用 `lsof` 找占用者）。

**场景**：数据盘挂载、扩容后重新挂载、维护前卸载。

### blkid —— 看分区的 UUID/文件系统类型

**风险**：✅ 只读

**语法**：`blkid [设备]`

```bash
blkid
```

**讲解**：打印各分区的 UUID、类型；写 `/etc/fstab` 做开机自动挂载时用 UUID 比设备名更稳。

**场景**：配 fstab 自动挂载、核对分区信息。

---

## 十、用户与权限

### chmod —— 改文件权限

**风险**：⚠️ 有影响（改变文件权限，`-R` 递归要特别小心）

**语法**：`chmod [-R] 权限模式 文件...`

```bash
chmod +x verify.sh          # 加可执行
chmod 755 deploy.sh         # 属主 rwx、其它 r-x
chmod 600 /etc/dba-mcp/dba-mcp.env    # 只有属主能读写（密钥文件必须这样）
chmod -R 750 /data/mysql    # 递归改
```

**讲解**：权限 = 属主/属组/其他 三组，`r=4 w=2 x=1`。`755` 服务器脚本通用；**含密码的 env/密钥文件用 `600`**；脚本从 Windows 传上来没执行权限时 `chmod +x`。

**场景**：部署后脚本跑不了（权限被 Windows 打包弄丢）、密钥文件权限审计、目录权限收紧。

### chown —— 改文件属主/属组

**风险**：⚠️ 有影响（改变属主/属组，改错会导致服务读写失败）

**语法**：`chown [-R] 用户[:组] 文件...`

```bash
chown mysql:mysql -R /data/mysql
chown -R outline:outline /var/lib/outline
```

**讲解**：`chown 用户:组 路径`，`-R` 递归。进程以某用户身份跑时，数据目录必须对得上属主，否则服务读写失败。

**场景**：二进制部署 MySQL 后修数据目录属主；容器映射目录权限不对时排查。

### useradd / passwd / id —— 建用户、改密码、看身份

**风险**：ℹ️ 视用法（`useradd`/`passwd` ⚠️ 有影响；`id` ✅ 只读）

**语法**：`useradd [-m] [-s shell] 用户`、`passwd [用户]`、`id [用户]`

```bash
useradd -m -s /bin/bash appuser
passwd appuser
id appuser
id            # 看当前用户
```

**讲解**：`useradd -m` 建家目录、`-s` 指定 shell；`passwd` 交互设置密码；`id` 看 UID/GID/所属组。

**场景**：建服务运行账号、确认"我是不是 root"、排查权限问题。

### sha256sum —— 校验文件完整性（备份/传输后必做）

**风险**：✅ 只读

**语法**：`sha256sum [选项] 文件...`（`-c` 按清单校验）

```bash
sha256sum dump.sql.gz
sha256sum -c checksums.txt     # 按清单文件逐个校验
sha256sum dump.sql.gz | tee dump.sha256
```

**讲解**：生成/核对文件的 SHA-256 摘要。传输或备份后对一下哈希，一致才说明文件没损坏。

**场景**：备份完成校验、`scp` 大文件后确认完整性、发布包校验（对账"备份四件套"）。

---

## 十一、压缩与备份

### tar czf —— 打包并 gzip 压缩

**风险**：⚠️ 有影响（生成压缩包文件）

**语法**：`tar czf 包名.tar.gz 路径...`（`c` 创建、`z` gzip、`f` 指定文件、`v` 显示过程）

```bash
tar czf backup-$(date +%Y%m%d).tar.gz /var/lib/outline/data
tar czvf backup.tar.gz ./dir/          # 带 v 显示打包过程
```

**讲解**：`c` 创建、`z` 用 gzip 压缩（得 `.tar.gz`）、`f` 指定包名。打包目录时常用相对路径，看到过程心里有数。

**场景**：备份附件卷/数据目录（如 25G 级附件卷）、归档日志、打发布包。

### tar tzf / xzf —— 先看内容再解压

**风险**：ℹ️ 视用法（`t` 列内容 ✅ 只读；`x` 解压到磁盘 ⚠️ 有影响）

**语法**：`tar tzf 包.tar.gz`、`tar xzf 包.tar.gz [-C 目标目录]`

```bash
tar tzf pkg.tar.gz        # 只列出内容（不解压）
tar xzf pkg.tar.gz        # 解压
tar xzf pkg.tar.gz -C /opt/mcpservers/dba-mcp/   # 解压到指定目录
```

**讲解**：`t` 列出、`x` 解压、`-C` 指定解压目标目录。解压前先 `t` 看一眼结构，避免解错位置。

**场景**：部署补丁包/发布包、恢复备份；`t` 先看包内结构。

### gzip / gunzip —— 单文件压缩/解压

**风险**：⚠️ 有影响（压缩时原地替换并删除原文件）

**语法**：`gzip [-9] 文件`、`gunzip 文件.gz`、`gzip -dc 文件.gz`（解压到标准输出）

```bash
gzip -9 access.log             # 压缩（原文件变 .gz，原文件被删）
gunzip access.log.gz           # 解压
gzip -dc access.log.gz | less  # 解压到标准输出看内容
```

**讲解**：`-9` 最高压缩比、`-dc` 解压到 stdout（`gzcat` 效果）。注意 `gzip` 默认**删除原文件**。

**场景**：压缩大日志节省磁盘；看历史压缩日志不用先解压。

### unzip —— 解 zip 包

**风险**：⚠️ 有影响（解压写入磁盘）

**语法**：`unzip 包.zip [-d 目标目录]`

```bash
unzip pkg.zip -d /opt/app/
```

**讲解**：解压 Windows 常用的 zip 格式，`-d` 指定目标目录。

**场景**：从同事那里收的 zip 包、Windows 打包的现场文件。

---

## 十二、定时任务（cron）

### crontab -l —— 看当前用户的定时任务

**风险**：✅ 只读

**语法**：`crontab -l`

```bash
crontab -l
crontab -l | grep backup     # 过滤某个任务
```

**讲解**：列出当前用户已配置的 cron 条目（5 个时间字段 + 命令：分 时 日 月 周）。

**场景**：确认某个定时任务配没配、几点跑；排查"任务为什么没执行"先看有没有条目。

### crontab -e —— 编辑定时任务

**风险**：⚠️ 有影响（修改定时任务配置，配错可能重复跑/漏跑）

**语法**：`crontab -e`（`分 时 日 月 周 命令`，命令用绝对路径）

```bash
crontab -e
# 例：每天 02:00 备份
0 2 * * * /opt/scripts/backup.sh >> /opt/scripts/logs/backup.log 2>&1
# 例：0-2 点每小时采集
* 0-2 * * * /opt/scripts/collect.sh >> /opt/scripts/logs/collect.log 2>&1
```

**讲解**：编辑当前用户的 crontab。**命令必须写绝对路径**；重定向 `>> log 2>&1` 把输出记下来（否则任务静默失败你根本看不见）。

**场景**：配置日常备份、巡检、数据采集；排查脚本"手动能跑、定时不跑"时看 cron 环境差异（PATH 不同）。

### systemctl status crond —— 确认 cron 服务在跑

**风险**：✅ 只读

**语法**：`systemctl status {crond|cron}`

```bash
systemctl status crond        # RHEL 系
systemctl status cron         # Debian 系
```

**讲解**：cron 守护进程本身挂了，所有定时任务都不会执行。`active (running)` 才算正常。

**场景**：定时任务"一条都没跑"时，先确认 cron 服务活着。

---

## 十三、软件包管理

### dnf / yum install —— 装软件包

**风险**：⚠️ 有影响（安装/升级系统软件包）

**语法**：`dnf|yum install [-y] 包名...`

```bash
dnf install -y rsync telnet nc
yum install -y rsync telnet nc
```

**讲解**：RPM 系包管理器（新系统用 `dnf`，老系统用 `yum`），`-y` 免交互确认。

**场景**：服务器缺工具时装上（如 `nc`、`rsync`）；部署前补依赖。

### rpm -qa —— 查装了什么

**风险**：✅ 只读

**语法**：`rpm -qa [模式]`、`rpm -qf 文件`

```bash
rpm -qa | grep -i mysql
rpm -qf /usr/local/bin/mysql     # 某文件属于哪个包
```

**讲解**：`-qa` 列出已装包，`-qf` 反查文件归属。

**场景**：确认某软件装没装/装的什么版本；查某个可执行文件是哪个包带来的。

---

## 十四、Docker 容器

### docker ps —— 看正在跑的容器

**风险**：✅ 只读

**语法**：`docker ps [-a]`

```bash
docker ps
docker ps -a          # 含已停止的
```

**讲解**：列出容器：ID、镜像、状态（Up/Exited）、端口映射、名字。`-a` 把停掉的也列出来。

**场景**：部署后确认容器活着（`Up (healthy)`）；排查容器反复重启（Exited）。

### docker exec -it —— 进容器里执行命令

**风险**：ℹ️ 视用法（`docker exec ... psql -c "SELECT ..."` ✅ 只读；写库/改文件等 ⚠️ 有影响）

**语法**：`docker exec [-it] [-e 变量=值] 容器 命令`

```bash
docker exec -it outline-postgres-outline-1 psql -U postgres
docker exec -e PGPASSWORD="$PASS" outline-postgres-outline-1 psql -U "$USER" -d "$DB" -Atc "SELECT 1;"
```

**讲解**：`-it` 交互式进 shell（`bash`/`sh`），或直接执行单条命令。`-e` 给容器进程传环境变量（如数据库密码）。

**场景**：容器内查数据库、看容器内文件、执行一次性命令（不改容器镜像）。

### docker logs -f —— 看容器日志

**风险**：✅ 只读

**语法**：`docker logs [-f] [--tail N] 容器`

```bash
docker logs -f --tail 100 outline-postgres-outline-1
```

**讲解**：`-f` 实时跟踪，`--tail 100` 先看最后 100 行。

**场景**：容器启动失败/服务起不来时看它输出；盯迁移日志等待关键行。

### docker compose up -d —— 按 compose 文件起服务

**风险**：⚠️ 有影响（创建/重建/更新容器）

**语法**：`docker compose up [-d] [服务...]`

```bash
docker compose up -d outline
docker compose up -d
```

**讲解**：根据 `docker-compose.yml` 创建/更新容器并后台运行（`-d`）。改了镜像标签后执行它让新镜像生效。

**场景**：升级服务换镜像后重建容器（先 `sed` 改标签、`grep` 复核，再 `up -d`）。

### docker compose logs -f —— 跟踪 compose 服务日志

**风险**：✅ 只读

**语法**：`docker compose logs [-f] [服务...]`

```bash
docker compose logs -f outline
```

**讲解**：等价于 `docker logs -f`，但按 compose 服务名来；多副本/多服务时更直观。

**场景**：升级后盯启动过程（等 `Listening on :3000` 出现）、盯迁移逐条执行。

### docker images / save / load —— 镜像查看与导入导出

**风险**：ℹ️ 视用法（`images` ✅ 只读；`save`/`load` ⚠️ 有影响（写文件/导入镜像））

**语法**：`docker images`、`docker save -o 文件 镜像`、`docker load -i 文件`

```bash
docker images                       # 本机镜像
docker save -o outline-1.12.tar outline:1.12
docker load -i outline-1.12.tar
```

**讲解**：`save` 把镜像导出成 tar，`load` 从 tar 导入。离线环境（不能直连仓库）靠这个搬镜像。

**场景**：把打好的镜像 tar 推到 harbor、在目标机 `load` 起来；演练机/本地临时镜像清理前先导出留存。

---

## 十五、数据库常用命令

### mysql —— MySQL 客户端连库/执行 SQL

**风险**：ℹ️ 视 SQL（`SELECT`/`SHOW` ✅ 只读；`INSERT/UPDATE/DELETE/DDL` ⚠️ 有影响）

**语法**：`mysql [-h 主机] [-u 用户] [-p] [-e SQL] [库名]`

```bash
mysql -h127.0.0.1 -uroot -p -e "SELECT VERSION();"
mysql -h127.0.0.1 -u<用户> -p -e "SHOW DATABASES;"
mysql -h127.0.0.1 -u<用户> -p <库名> < backup.sql    # 导入 SQL
```

**讲解**：`-h` 主机、`-u` 用户、`-p` 密码（建议交互输入，不写进命令历史）、`-e` 执行单条 SQL 后退出。导入用重定向。

**场景**：临时查数据、验证账号权限、导入备份；**密码别直接写在命令行**（`-p` 后不跟值，回车再输）。

### mysqldump —— 导出 MySQL 逻辑备份

**风险**：✅ 只读（对数据库只读；会生成备份文件到本地）

**语法**：`mysqldump [-u 用户] [-p] [--single-transaction] [--routines] [--triggers] 库名 > 文件.sql`

```bash
mysqldump -u<用户> -p --single-transaction --routines --triggers <库名> > dump.sql
mysqldump -u<用户> -p --all-databases > all.sql
```

**讲解**：`--single-transaction` 保证 InnoDB 一致性备份不锁表；`--routines` 导存储过程、`--triggers` 导触发器。

**场景**：升级前备份数据库、迁移数据、定期逻辑备份。

### pg_dump —— PostgreSQL 逻辑备份

**风险**：✅ 只读（对数据库只读；生成备份文件到本地）

**语法**：`pg_dump [-U 用户] [-d 库名] [-f 文件] [库名]`

```bash
pg_dump -U <用户> -d <库名> -f outline-$(date +%Y%m%d).sql
pg_dump -U <用户> -d <库名> | gzip > dump.sql.gz
```

**讲解**：导出 PG 库为 SQL 文件，`-f` 指定输出文件；大库可管道 `gzip` 压缩。

**场景**：Outline/Keycloak 这类 PG 业务库升级前备份；按时间戳命名归档。

### psql —— PostgreSQL 客户端

**风险**：ℹ️ 视 SQL（`SELECT` ✅ 只读；写操作 ⚠️ 有影响）

**语法**：`psql [-U 用户] [-d 库名] [-c SQL] [-f 文件] [-Atc SQL]`

```bash
psql -U <用户> -d <库名> -c "SELECT count(*) FROM \"SequelizeMeta\";"
psql -U <用户> -d <库名> -f backup.sql        # 执行 SQL 文件
psql -U <用户> -d <库名> -Atc "SELECT 1;"     # 单值输出（脚本里用）
```

**讲解**：`-c` 执行单条 SQL、`-f` 执行文件、`-At` 无边框纯文本（脚本取数用）。库名/表名含大写时用双引号。

**场景**：升级后对账（数行数/列数）、查 `SequelizeMeta` 迁移账本、回滚时重放 SQL。

### mongo —— MongoDB 客户端执行命令

**风险**：ℹ️ 视命令（`--eval 'db.stats()'` ✅ 只读；`db.xxx.insert/update/remove` ⚠️ 有影响）

**语法**：`mongo [主机/库] [-u 用户] [-p] [--authenticationDatabase 库] [--eval 'JS']`

```bash
mongo --quiet --eval 'db.stats().ok' <库名>
mongo <主机>/<库> -u <用户> -p --authenticationDatabase <auth库> --eval 'db.serverStatus().uptime'
```

**讲解**：`--quiet` 少打日志、`--eval` 直接执行 JS 表达式。

**场景**：巡检不同库的统计、健康检查脚本里取状态。

### redis-cli —— Redis 客户端

**风险**：ℹ️ 视命令（`ping`/`info` ✅ 只读；`set`/`del`/`flushall` ⚠️ 有影响）

**语法**：`redis-cli [-h 主机] [-p 端口] 命令`

```bash
redis-cli -h 127.0.0.1 -p 6379 ping
redis-cli -h 127.0.0.1 -p 6379 info memory | head -20
```

**讲解**：`ping` 返回 `PONG` 说明通；`info` 看运行指标（memory 段看内存）。

**场景**：确认 Redis 可达、看内存水位。

---

## 十六、常用"一条命令看全身"套路

### date —— 生成时间戳，命令按时间命名

**风险**：✅ 只读

**语法**：`date [选项] [+格式]`（`+%Y%m%d-%H%M%S`）

```bash
TS=$(date +%Y%m%d-%H%M%S)
date '+%Y-%m-%d %H:%M:%S'
```

**讲解**：`+%Y%m%d-%H%M%S` 生成 `20261105-153000` 式的时间戳；每次执行都会变，所以先存变量再复用。

**场景**：备份文件名/目录带时间戳，避免覆盖；脚本里统一时间基准。

### set -a + source .env —— 一条命令加载服务环境变量

**风险**：✅ 只读（只影响当前 shell，不写系统）

**语法**：`set -a; source 文件; set +a`

```bash
set -a; source .env; set +a
echo "user=$OUTLINE_DB_USER pass长度=${#OUTLINE_DB_PASSWORD}"   # 核对但不打印口令
```

**讲解**：`set -a` 让 env 文件里的变量自动 export 到当前 shell，`source` 执行该文件，`set +a` 恢复。之后 `$OUTLINE_DB_USER` 等变量就能直接在命令里用。

**场景**：备份脚本、运维脚本里不再手填数据库账号密码（口令不落盘、不进聊天记录）。

### 一条命令看服务健康（ss + curl + systemctl）

**风险**：✅ 只读（端口探测 + GET 健康接口）

**语法**：`ss -lntp | grep 端口; curl -s -o /dev/null -w '%{http_code}' URL; systemctl is-active 单元`

```bash
ss -lntp | grep -E ':8020\b'; \
curl -s -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:8020/healthz; \
systemctl is-active dba-mcp
```

**讲解**：一次跑完"端口在听 + 健康接口 200 + 服务 active"三件事，用分号串起来。

**场景**：部署后快速自检、巡检脚本、变更前后对比截图取证。

### 登机后第一屏：系统信息速览

**风险**：✅ 只读

**语法**：`whoami; id; hostname -I; uname -a; cat /etc/os-release; systemctl --version`

```bash
whoami; id; hostname -I; uname -a; cat /etc/os-release | head -3; systemctl --version | head -1
```

**讲解**：一次打印"我是谁、在哪台机器、什么系统、systemd 版本"。`head -3` 避免刷屏。

**场景**：登上一台陌生机器先摸清底细（决定用 systemd 还是 init.d、有没有可用的防火墙工具）。

### 一条命令数数据库行数 / 校验条数

**风险**：✅ 只读（示例为 SELECT 对账）

**语法**：`docker exec -e PGPASSWORD=口令 容器 psql -U 用户 -d 库 -Atc "SELECT ..." | tee 文件`

```bash
docker exec -e PGPASSWORD="$PASS" <容器> psql -U "$USER" -d "$DB" -Atc \
  "SELECT count(*) FROM \"SequelizeMeta\";" | tee /tmp/meta-before.txt
```

**讲解**：`psql -Atc` 输出纯数字，`tee` 同时存文件留证据。

**场景**：升级前后对账（如迁移账本从 X 条变 Y 条）、备份前后一致性校验。

---

> **安全提醒（写进习惯）**
> 1. 标 ⚠️/ℹ️ 的命令，执行前先确认参数；`rm -rf`、`kill -9`、`chmod -R`、`sed -i` 改配置前，先 `ls` / `cp` 备份 / 先不带 `-delete` 跑一遍预览。
> 2. 密码只走 `-p` 交互或 `source .env` 变量，绝不写进命令、脚本或聊天记录；含密码的文件权限 `600 root:root`。
> 3. 生产变更遵循"只读取证 → 改 → 校验 → 回滚方案"四步；端口/防火墙收敛类操作在**另一台机器**做负向验证。
> 4. 定时任务中的命令用绝对路径，并重定向日志到文件，否则失败无声无息。
