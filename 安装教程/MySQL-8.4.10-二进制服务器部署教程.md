# MySQL 8.4.10 二进制方式 Linux 服务器部署完整教程（CentOS 7 实测）

> 本文记录一次完整的 MySQL 8.4.10 生产级单机部署过程：从选包、下载、初始化，到启动托管、改密、验证，包含实际踩到的坑与排查方法。
>
> 文中示例服务器地址统一使用 `192.0.2.10`（RFC 5737 文档专用网段），请替换为你自己的服务器 IP。`server_id`、`uid/gid` 等也均为示例值。

## 为什么用二进制包

比起 `yum` / `apt` 安装，官方通用二进制包（Generic Binary）有几个实际优势：

- **版本完全可控**：发行版仓库里的 MySQL 版本通常滞后很多，二进制包可以直接指定到具体小版本
- **不污染系统**：不写系统包管理器数据库，卸载只需删目录
- **可以多版本共存**：不同 basedir 各跑一套，方便灰度升级或版本回退
- **路径可完全自定义**：数据目录、日志、socket 全部可放到独立数据盘

代价是需要手工做几件包管理器帮你做的事：建运行账号、建目录、写配置文件、配启动脚本。本文就是把这几件事讲清楚。

## 实验环境

| 项目 | 规格 |
|---|---|
| 操作系统 | CentOS 7，内核 3.10.0-1160.el7.x86_64 |
| glibc | 2.17 |
| CPU / 内存 | 2 核 / 4G |
| 数据盘 | 独立挂载 `/data`，40G（ext4） |
| 数据库版本 | MySQL 8.4.10 Community Server |
| 安装包 | `mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz`（790MB） |

> 规格偏小是有意为之：小规格机器上更能看出参数是否需要收缩，也更容易暴露内存问题。

## 目录

1. [第一步：确认 glibc 版本，选对安装包](#一确认-glibc-版本选对安装包)
2. [第二步：规划目录结构与运行账号](#二规划目录结构与运行账号)
3. [第三步：wget 下载安装包](#三wget-下载安装包)
4. [第四步：校验与解压](#四校验与解压)
5. [第五步：编写 my.cnf](#五编写-mycnf)
6. [第六步：初始化实例](#六初始化实例)
7. [第七步：配置启动脚本与开机自启](#七配置启动脚本与开机自启)
8. [第八步：设置 root 密码](#八设置-root-密码)
9. [第九步：验证](#九验证)
10. [常见报错汇总](#常见报错汇总)
11. [踩坑总结](#踩坑总结)
12. [附录：systemd 替代写法](#附录systemd-替代写法)

---

## 一、确认 glibc 版本，选对安装包

**这一步错了，后面全白费。** MySQL 8.4 的 Linux 通用二进制包有两个 glibc 分支：

| 包名 | 目标平台 | 适用 |
|---|---|---|
| `mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz` | 老发行版 | CentOS 7 / RHEL 7 |
| `mysql-8.4.10-linux-glibc2.28-x86_64.tar.xz` | 新发行版 | CentOS 8+ / RHEL 8+ / Ubuntu 20+ |

先查本机 glibc：

```bash
ldd --version | head -n1
getconf GNU_LIBC_VERSION
uname -m
```

CentOS 7 上会输出 `ldd (GNU libc) 2.17`，所以**必须选 glibc2.17 那个包**。

如果误用了 glibc2.28 的包，解压后运行会直接报：

```
/usr/local/mysql/bin/mysqld: /lib64/libc.so.6: version `GLIBC_2.28' not found
```

这类错误没有优雅的补救办法——换包重来是唯一选择。

> 顺便说明：CentOS 7 已停止维护，不在 MySQL 8.4 的官方认证发行版列表里。glibc2.17 通用包在这种老系统上实测能正常跑，但属于"能用、非官方兜底"的组合，长期使用需自行评估风险。

---

## 二、规划目录结构与运行账号

### 目录规划

先看一下磁盘情况，确认数据盘挂在哪里：

```bash
df -hT
lsblk -f
```

本例的规划：

| 用途 | 路径 |
|---|---|
| 程序目录（basedir） | `/usr/local/mysql` |
| 数据目录（datadir） | `/data/mysql/data` |
| binlog | `/data/mysql/binlog/` |
| 错误日志 / 慢日志 | `/data/mysql/log/` |
| socket / pid | `/data/mysql/tmp/` |
| 配置文件 | `/etc/my.cnf` |
| 安装包留存 | `/data/install_pkgs/` |

**关键原则：数据目录一定要放到独立数据盘，不要留在系统盘。** MySQL 的默认 datadir 是 `<basedir>/data`，如果不显式指定，数据会悄悄写进 `/usr/local/mysql/data`，系统盘迟早被撑爆。

### 创建运行账号

MySQL 不应该以 root 身份运行。创建一个专用系统账号：

```bash
groupadd -g 1001 mysql
useradd -r -u 997 -g 1001 -d /home/mysql -s /bin/false mysql
mkdir -p /home/mysql && chown mysql:mysql /home/mysql
```

- `-r`：创建系统账号
- `-s /bin/false`：禁止登录系统，这个账号只用来"拥有"数据库进程和文件
- `-u 997 -g 1001`：显式指定 uid/gid。**如果以后要做多机备份还原、主从数据搬迁，多台机器上 mysql 的 uid/gid 保持一致可以避免文件属主对不上的坑**。不做这些的话省略这两个参数、让系统自动分配也可以

先确认 uid/gid 没被占用：

```bash
getent passwd 997; getent group 1001
```
（无输出表示可用）

### 创建目录

```bash
mkdir -p /data/mysql/{data,binlog,log,tmp}
mkdir -p /data/install_pkgs

chown -R mysql:mysql /data/mysql
chmod 755 /data/mysql
chmod 750 /data/mysql/data

id mysql
ls -ld /data/mysql /data/mysql/*
```

关于权限，用三位数字表示「属主 / 同组 / 其他人」：`7 = rwx`、`5 = r-x`、`0 = ---`。

| 路径 | 权限 | 含义 |
|---|---|---|
| `/data/mysql` | `755` | mysql 全权，其他人可进入查看（便于运维排查） |
| `/data/mysql/data` | `750` | mysql 全权，同组可读可进入，**其他人完全进不去** |
| 各业务库目录 | `750` | 初始化时 MySQL 自动创建 |
| 数据文件 | `640` | 同上 |

**注意**：`755` 和 `750` 对 mysql 自己来说没区别（属主和组都是它），差别只在于**其他人能不能进**。所以 datadir 用 750 是一种数据隔离。

---

## 三、wget 下载安装包

```bash
mkdir -p /data/install_pkgs && cd /data/install_pkgs

nohup wget -4 -c -T 30 -t 3 \
  https://dev.mysql.com/get/Downloads/MySQL-8.4/mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz \
  > wget.log 2>&1 &

tail -f wget.log
```

几个参数值得说明：

| 参数 | 作用 |
|---|---|
| `-4` | **强制 IPv4**。如果服务器有 AAAA 记录但实际没有全局 IPv6 地址，wget 可能连到 IPv6 地址后长时间挂住 |
| `-c` | 断点续传。会话断了重跑同一条命令即可接着下，不用从头再来 |
| `-T 30` | 连接/读取超时 30 秒 |
| `-t 3` | 重试 3 次 |
| `nohup ... &` | 放后台，避免 SSH 会话超时把进程带走 |

下载中的排查：

```bash
# 看进度
tail -f wget.log

# 看文件大小（最终应为 828832968 字节）
ls -l
```

如果服务器的 SSH 会话容易超时，用 `nohup` 放后台是必要的——790MB 的包在慢带宽下可能要十几分钟。

> 也可以先探测一下包是否存在，不落盘：
> ```bash
> wget --spider -S https://dev.mysql.com/get/Downloads/MySQL-8.4/mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz 2>&1 | tail -20
> ```

---

## 四、校验与解压

### 校验

```bash
cd /data/install_pkgs
stat -c '%s' mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz   # 应为 828832968
xz -t mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz && echo "包完整"
```

`xz -t` 会完整解压一遍做 CRC 校验，**在 2 核机器上要跑几分钟**，属正常现象。

> 想省时间也可以跳过 `xz -t`——下一步的 `tar -xJf` 本身就会完整解压，压缩流损坏会直接报错。两道工序重复校验同一件事。

### 解压

```bash
cd /usr/local
tar -xJf /data/install_pkgs/mysql-8.4.10-linux-glibc2.17-x86_64.tar.xz
mv mysql-8.4.10-linux-glibc2.17-x86_64 mysql
```

注意两点：

1. **解压过程是静默的**，2～5 分钟没有输出属正常，不要 Ctrl+C。中断会留下残缺目录，必须 `rm -rf` 后重来。
2. **`mv` 之前先确认 `/usr/local/mysql` 不存在**。如果已存在，`mv` 不会报错，而是**静默把目录塞进去**变成 `/usr/local/mysql/mysql-8.4.10-.../`：

```bash
ls -d /usr/local/mysql* 2>&1
```

### 检查动态库依赖（关键）

```bash
ldd /usr/local/mysql/bin/mysqld | grep 'not found'
/usr/local/mysql/bin/mysqld --version
```

`ldd` 必须是**零输出**。`--version` 应输出：

```
/usr/local/mysql/bin/mysqld  Ver 8.4.10 for Linux on x86_64 (MySQL Community Server - GPL)
```

如果 `ldd` 报缺库，最常见的是这两个：

| 报错 | 缺失包 | 处理 |
|---|---|---|
| `libnuma.so.1 => not found` | `numactl-libs` | `yum install -y numactl-libs` |
| `libssl.so.* => not found` | `openssl11-libs` | `yum install -y openssl11-libs` |

```bash
yum install -y numactl-libs openssl11-libs
ldd /usr/local/mysql/bin/mysqld | grep 'not found'   # 复查，应无输出
```

---

## 五、编写 my.cnf

### 完整配置

```bash
cat > /etc/my.cnf <<'EOF'
[client]
port                                  = 3306
socket                                = /data/mysql/tmp/mysql.sock

[mysqld_safe]
open-files-limit                      = 65535

[mysqld]
user                                  = mysql
port                                  = 3306
server_id                             = 103306
basedir                               = /usr/local/mysql
datadir                               = /data/mysql/data
socket                                = /data/mysql/tmp/mysql.sock
pid-file                              = /data/mysql/tmp/mysql.pid
character-set-server                  = utf8mb4
collation-server                      = utf8mb4_0900_ai_ci
lower_case_table_names                = 0
skip_name_resolve                     = ON

log_error                             = /data/mysql/log/error.log
log_timestamps                        = SYSTEM
slow_query_log                        = ON
slow_query_log_file                   = /data/mysql/log/slow.log
long_query_time                       = 1

log_bin                               = /data/mysql/binlog/binlog
binlog_format                         = ROW
sync_binlog                           = 1
binlog_expire_logs_seconds            = 259200
gtid_mode                             = ON
enforce_gtid_consistency              = TRUE

innodb_buffer_pool_size               = 2G
innodb_buffer_pool_instances          = 1
innodb_redo_log_capacity              = 512M
innodb_flush_log_at_trx_commit        = 1
innodb_flush_method                   = O_DIRECT
max_connections                       = 150
open_files_limit                      = 65535
admin_address                         = 127.0.0.1
admin_port                            = 33062
EOF

chmod 644 /etc/my.cnf
```

### 分组解释

**[client]**

```ini
port   = 3306
socket = /data/mysql/tmp/mysql.sock
```

这里的 socket 路径和 `[mysqld]` 里保持一致。**不写这段的话**，本地 `mysql` 命令会去 `/tmp/mysql.sock` 找 socket，每次都得加 `-S` 参数，很麻烦。

**[mysqld_safe]**

```ini
open-files-limit = 65535
```

`mysqld_safe` 在拉起 mysqld 之前会先设 `ulimit -n`。写在这里可以保证无论用什么方式启动，文件句柄上限都一致。

**基础段**

| 参数 | 说明 |
|---|---|
| `user = mysql` | 进程以 mysql 身份运行，不是 root |
| `basedir` / `datadir` | **必须显式写**，否则默认 datadir 落在 `<basedir>/data`，数据会写进系统盘 |
| `socket` / `pid-file` | 都放 `/data/mysql/tmp`，注意父目录要属主 mysql |
| `character-set-server` / `collation-server` | 8.4 默认就是 utf8mb4，写出来是自我文档化，防止被后续改动带偏 |
| `lower_case_table_names` | **最重要的一项**，下文单独讲 |
| `skip_name_resolve` | 关闭反向 DNS 解析。好处是连接更快、DNS 故障不影响建连；**代价是建账号必须用 IP 或 localhost，不能用域名** |

> **关于 `lower_case_table_names`**
>
> 它控制库名/表名是否区分大小写：`0` 区分、`1` 不区分（存盘统一转小写）。
>
> **初始化之后无法修改**，因为库名对应磁盘目录名、表名对应文件名，改这个参数意味着重命名所有文件。8.0+ 若发现配置值与数据字典不一致，会直接拒绝启动：
>
> ```
> Different lower_case_table_names settings for server ('1') and data dictionary ('0')
> ```
>
> 所以必须在**初始化之前**定下来。Linux 上默认是 `0`，与存量库保持一致是最稳妥的选择。

**日志段**

| 参数 | 说明 |
|---|---|
| `log_error` | 不指定的话错误日志会写进 datadir，跟数据文件混在一起 |
| `log_timestamps = SYSTEM` | **默认是 UTC**，日志时间比北京时间少 8 小时，排查故障时极易误判，这个坑建议提前避开 |
| `slow_query_log` / `long_query_time = 1` | 慢日志是排障刚需，默认 10 秒阈值对小库太宽松 |

**binlog 段**

| 参数 | 说明 |
|---|---|
| `log_bin` | 开启 binlog，独立目录存放。用途：数据恢复、CDC 订阅、主从复制 |
| `binlog_format = ROW` | 8.4 默认就是 ROW，一致性最好 |
| `sync_binlog = 1` | 「双 1」之一，与 `innodb_flush_log_at_trx_commit = 1` 配合，保证断电不丢数据 |
| `binlog_expire_logs_seconds` | 保留期。示例给 3 天（259200），**按数据盘容量定**；盘大可以给 7 天（604800） |
| `gtid_mode = ON` | 为将来做主从预留，GTID 模式下搭建和切换都简单得多 |

**InnoDB 段（性能核心）**

| 参数 | 说明 |
|---|---|
| `innodb_buffer_pool_size = 2G` | 最关键的性能参数。示例机 4G 内存取 2G，留给系统约 2G。**如果这台机器还跑着别的服务，建议降到 1～1.5G** |
| `innodb_buffer_pool_instances = 1` | 按 CPU 核数定，2 核用 1 个实例即可。注意约束：instances ≤ `buffer_pool_size / chunk_size`（默认 chunk 128M） |
| `innodb_redo_log_capacity = 512M` | **8.4 的新参数**，替代老版本的 `innodb_log_file_size` + `innodb_log_files_in_group`。支持在线调整 |
| `innodb_flush_log_at_trx_commit = 1` | 「双 1」的另一半，事务提交即刷盘 |
| `innodb_flush_method = O_DIRECT` | 绕过 OS page cache，避免同一份数据在 InnoDB 和 OS 各存一份。内存紧张的机器尤其重要 |

**内存预算参考（4G 机器）**

| 用途 | 估算 |
|---|---|
| `innodb_buffer_pool_size` | 2.0G |
| performance_schema（默认开启） | 0.2～0.4G |
| 连接缓冲（150 连接 × 约 1M） | 约 0.15G |
| redo log buffer 及其他 | 约 0.05G |
| OS + 其他进程 | 0.3～0.5G |
| **合计** | **约 2.7～3.1G** |

留不到 1G 余量，属于"能跑但不宽松"。**上线后用 `free -h` 和 `ps -o rss= -p $(pidof mysqld)` 实测确认一下**，available 长期低于 500M 就该回调 buffer pool。

**连接与管理段**

| 参数 | 说明 |
|---|---|
| `max_connections = 150` | 全局上限，按内存定。想加大必须先加内存 |
| `open_files_limit` | 与 `[mysqld_safe]` 的 `open-files-limit`、系统 `limits.conf` 三者对齐 |
| `admin_address` / `admin_port` | 管理后门：连接数打满时可用 `mysql -h127.0.0.1 -P33062` 连进去处理。只监听本地 |

### 8.4 中已移除的参数（踩坑高发区）

网上大量 8.0 时代的配置模板直接套到 8.4 上会**启动失败**：

| 8.0 写法 | 8.4 情况 | 替代 |
|---|---|---|
| `default_authentication_plugin = mysql_native_password` | **参数已移除** | 用 `authentication_policy` + `mysql_native_password = ON` |
| `innodb_log_file_size = 512M` | **已废弃** | `innodb_redo_log_capacity` |
| `innodb_log_files_in_group = 2` | **已废弃** | 同上 |
| `slave_parallel_type` 等 `slave_*` 系列 | 大量移除/改名为 `replica_*` | 按需换成新名字 |

### 启动前校验配置（强烈建议）

```bash
/usr/local/mysql/bin/mysqld --defaults-file=/etc/my.cnf --validate-config --user=mysql
echo "退出码: $?"
```

预期：无输出、退出码 0。

这个命令**只校验配置合法性、不启动服务**，能把所有"参数不存在/写错"的问题在启动前一次性暴露出来，比启动失败后翻 error log 高效得多。

---

## 六、初始化实例

```bash
/usr/local/mysql/bin/mysqld --defaults-file=/etc/my.cnf \
  --initialize --user=mysql \
  --basedir=/usr/local/mysql \
  --datadir=/data/mysql/data

echo "退出码: $?"
```

几个要点：

- **`--defaults-file` 必须放在第一个参数位置**，否则不生效
- `datadir` 必须是**空目录**，否则初始化失败
- 过程静默 30～60 秒，属正常

初始化会生成一个**随机临时密码**，只打印在错误日志里：

```bash
grep 'temporary password' /data/mysql/log/error.log | tail -1
```

输出形如：

```
2026-01-01T10:00:00.000000+08:00 6 [Note] [MY-010454] [Server] A temporary password is generated for root@localhost: xxxxxxxxxxxx
```

**立刻记下来**，下一步要用。

### 检查数据目录

```bash
ls /data/mysql/data
du -sh /data/mysql/data
```

应该看到（约 20 个条目）：

| 类型 | 条目 |
|---|---|
| 系统库 | `mysql`、`performance_schema`、`sys` |
| 数据字典/表空间 | `mysql.ibd`、`ibdata1`、`undo_001`、`undo_002` |
| redo / 双写 | `#innodb_redo`、`#innodb_temp`、`#ib_16384_*.dblwr` |
| TLS 证书 | `ca.pem`、`server-cert.pem`、`server-key.pem`、`client-cert.pem` 等 |
| 标识 | `auto.cnf` |

`du -sh` 大约 600MB（其中 redo 占 512M）。

> 两个常见疑问：
> - **证书是自动生成的**，因为 8.4 默认 `auto_generate_certs=ON`，意味着 TLS 加密连接开箱可用
> - **此时还没有 `binlog.000001`**，binlog 要等第一次启动服务才创建

---

## 七、配置启动脚本与开机自启

MySQL 二进制包里自带一个 SysV 风格的启动脚本：

```bash
cp /usr/local/mysql/support-files/mysql.server /etc/init.d/mysql
chmod 755 /etc/init.d/mysql
```

脚本里有两行路径，默认是空的，建议写死（虽然它也会从 my.cnf 读，但显式写更稳）：

```bash
grep -nE '^(basedir|datadir)=' /etc/init.d/mysql
# 输出形如：
# 46:basedir=
# 47:datadir=

sed -i 's|^basedir=.*|basedir=/usr/local/mysql|; s|^datadir=.*|datadir=/data/mysql/data|' /etc/init.d/mysql
grep -nE '^(basedir|datadir)=' /etc/init.d/mysql
```

> 这里有个细节：即使不改这两行通常也能启动成功，因为脚本里 `basedir` 为空时的内置默认值恰好是 `/usr/local/mysql`，而 `datadir` 会通过 `my_print_defaults` 从 my.cnf 读到。但这属于"靠默认值兜住"，显式写死更可靠。

配开机自启并启动：

```bash
chkconfig --add mysql
chkconfig mysql on
service mysql start
```

预期输出：

```
Starting MySQL... SUCCESS!
```

`chkconfig --list mysql` 应显示：

```
mysql    0:off   1:off   2:on   3:on   4:on   5:on   6:off
```

### 配置 PATH

```bash
echo 'export PATH=/usr/local/mysql/bin:$PATH' > /etc/profile.d/mysql.sh
chmod +x /etc/profile.d/mysql.sh
source /etc/profile.d/mysql.sh

which mysql && mysql --version
```

之后直接敲 `mysql`、`mysqladmin` 即可，不用写全路径。

---

## 八、设置 root 密码

### 为什么必须改一次

`--initialize` 生成的临时密码处于**「已过期」状态**——能登录，但执行改密以外的任何语句都会报：

```
ERROR 1820 (HY000): You must reset your password using ALTER USER statement before executing this statement.
```

所以**至少要执行一次 `ALTER USER`**，实例才真正可用。

### 生成随机密码

```bash
NEWPW=$(tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 10)
echo "$NEWPW"
```

只取字母和数字，是为了避免 `!@#$%` 这类字符在 SQL 引号和 shell 里的转义麻烦。

记下密码后清理命令历史：

```bash
history -c
```

### 改密

交互式登录：

```bash
mysql -uroot -p
```

在 `Enter password:` 提示处粘贴**临时密码**（不回显，正常）：

```sql
ALTER USER 'root'@'localhost' IDENTIFIED BY '你的新密码';
```

> ⚠️ 注意：**不要把密码直接写在 `-p` 后面**
> ```bash
> mysql -uroot -pMyP@ss(w0rd     # ← 会报 syntax error
> ```
> 因为 `(` 在 shell 里是特殊字符。要么用交互式提示输入，要么加单引号 `-p'MyP@ss(w0rd'`（但会暴露在 `ps` 和命令历史里）。

### 也可以一条命令完成

不想手工输入时，可以让 shell 直接从错误日志取临时密码：

```bash
TMPPW=$(grep 'temporary password' /data/mysql/log/error.log | tail -1 | awk '{print $NF}')
NEWPW=$(tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 10)
echo "$NEWPW"

mysql -uroot -p"$TMPPW" --connect-expired-password \
  -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$NEWPW';"
```

`--connect-expired-password` 是必须的，否则非交互式执行会报 `ERROR 1862`。

### 验证

```bash
mysql -uroot -p -e "SELECT VERSION();"
```
用**新密码**登录，应输出 `8.4.10`。

---

## 九、验证

### 基础检查

```bash
service mysql status
chkconfig --list mysql
ss -lntp | grep -E '3306|33060|33062'
ls -ld /data/mysql /data/mysql/*
mysqladmin -uroot -p status
```

预期监听：

| 端口 | 用途 |
|---|---|
| `[::]:3306` | 业务端口 |
| `[::]:33060` | X Protocol（mysqlx） |
| `127.0.0.1:33062` | 管理端口 |

> 一个免费的小技巧：`ss -lntp` 里 3306 那行的 `Send-Q` 值等于 `max_connections`（示例是 150），可以顺手确认参数是否生效。

### 参数核对

```bash
mysql -uroot -p
```

```sql
SELECT VERSION() AS version,
       @@server_id                    AS server_id,
       @@lower_case_table_names       AS lctn,
       @@character_set_server          AS charset,
       @@innodb_buffer_pool_size/1024/1024/1024 AS buffer_pool_gb,
       @@max_connections              AS max_conn,
       @@gtid_mode                    AS gtid;
```

确认 `lctn = 0`（初始化后就改不了了）、`buffer_pool_gb` 与配置一致、字符集是 utf8mb4。

### 重启验证（必做）

```bash
reboot
```

重连后：

```bash
ps -ef | grep '[m]ysqld'
mysql -uroot -p -e "SELECT VERSION();"
```

**这一项是验收的硬标准**——它是唯一能证明 `chkconfig` 开机自启真正生效的方式。很多部署上线后第一次重启才发现没配自启，就是因为漏了这一步。

---

## 常见报错汇总

| 报错 | 原因 | 处理 |
|---|---|---|
| `GLIBC_2.28 not found` | 装错成 glibc2.28 分支的包 | 换 glibc2.17 包 |
| `libnuma.so.1 => not found` | 缺 numactl-libs | `yum install -y numactl-libs` |
| `libssl.so.* => not found` | OpenSSL 版本过低 | `yum install -y openssl11-libs` |
| `unknown variable 'default_authentication_plugin'` | 抄了 8.0 的配置 | 删除该行 |
| `unknown variable 'innodb_log_file_size'` | 同上 | 改用 `innodb_redo_log_capacity` |
| `ERROR 1820: You must reset your password ...` | 临时密码已过期 | 执行一次 `ALTER USER` |
| `ERROR 1862: Your password has expired` | 非交互式执行时密码过期 | 加 `--connect-expired-password` |
| `syntax error near unexpected token '('` | 密码直接跟在 `-p` 后面 | 改用交互式输入或加引号 |
| `ERROR 1045 Access denied` | 密码不对（含"用新密码登录但还没设置"的情况） | 用临时密码登录后先 `ALTER USER` |
| `Can't create/write to file ... (Errcode: 13)` | 目录属主/权限不对 | `chown -R mysql:mysql /data/mysql` |
| `Different lower_case_table_names settings ...` | 初始化前后该参数不一致 | 清空 datadir 重新初始化（该参数不可改） |
| `ERROR! The server quit without updating PID file` | 启动失败 | `tail -50 /data/mysql/log/error.log` |
| 应用连不上，提示 `caching_sha2_password` | 客户端/驱动版本过老 | 升级驱动到 8.x；或开启 `mysql_native_password`（不推荐，属于降级） |

---

## 踩坑总结

按踩到的痛苦程度排序，这几条最值得记：

1. **glibc 分支选错 = 全盘重来**。动手前先 `ldd --version`，10 秒的事，能省两小时。

2. **`lower_case_table_names` 是不可逆的**。它初始化后才暴露问题，且只能清库重来。开工前务必定下来。

3. **8.0 的配置模板不能直接喂给 8.4**。`default_authentication_plugin`、`innodb_log_file_size` 这些参数已被移除，写了直接启动失败。**用 `--validate-config` 先校验一遍**，能把这类问题在启动前全部抓出来。

4. **IPv6 AAAA 记录会坑 wget**。服务器没有全局 IPv6 地址但域名有 AAAA 记录时，wget 可能连到 IPv6 后挂住。一律加 `-4`。

5. **临时密码是"已过期"状态**。能登录，但除了改密什么都干不了。看到 `ERROR 1820` 不是权限问题，是还没改密码。

6. **`mv` 到已存在的目录会静默嵌套**。`mv xxx mysql` 时如果 `mysql` 目录已存在，会变成 `mysql/xxx/`，而且不报错。改名之前先 `ls -d` 确认。

7. **忘了重启验证，等于没验收**。`chkconfig on` 显示成功不代表重启后真的会拉起，只有真重启一次才算数。

8. **小内存机器别抄大机器的参数**。网上流传的配置大多来自 16C64G 环境，`innodb_buffer_pool_size = 12G`、`max_connections = 2000` 直接套到 4G 机器上必然 OOM。

---

## 附录：systemd 替代写法

上面的 `init.d` + `mysqld_safe` 是传统做法。如果系统是全新的、没有历史包袱，用 systemd 更现代，日志也能进 journald：

```ini
# /usr/lib/systemd/system/mysqld.service
[Unit]
Description=MySQL Server 8.4.10
After=network.target

[Service]
Type=simple
User=mysql
Group=mysql
ExecStart=/usr/local/mysql/bin/mysqld --defaults-file=/etc/my.cnf
ExecStop=/usr/local/mysql/bin/mysqladmin --defaults-file=/etc/my.cnf shutdown
Restart=on-failure
RestartSec=5
LimitNOFILE=65535
RuntimeDirectory=mysqld
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable mysqld
systemctl start mysqld
systemctl status mysqld
```

注意 `[mysqld_safe]` 段落对 systemd 方式不起作用（`mysqld_safe` 根本没参与），文件句柄限制靠 `LimitNOFILE` 控制——所以**两种启动方式不要混用**，选定一种。

---

## 安全建议（可选步骤）

部署完能跑之后，建议顺手做几件事：

1. **root 只保留本地登录**。这是 `--initialize` 的默认行为，不要为了"方便"改成 `root@'%'`。

2. **远程访问用独立账号**，按最小权限授权：

   ```sql
   CREATE USER 'app_user'@'192.0.2.%' IDENTIFIED BY '<强密码>';
   GRANT SELECT, INSERT, UPDATE, DELETE ON `mydb`.* TO 'app_user'@'192.0.2.%';
   ```

   **不要用 `%`**，能写具体 IP 就写具体 IP，至少落到一个最小网段。

3. **应用账号和管理账号分开**。应用只用 DML 权限，DDL 由运维/DBA 账号持有。

4. **需要图形化工具连库时，优先用 SSH 隧道**（Navicat / DBeaver 都支持）。隧道连接从服务器本地回环发出，这样 root 依然只允许 localhost，不用为了"方便"去开远程 root。

5. **顺手收掉 X Protocol 端口**。如果不用 MySQL Shell / X DevAPI，可以让 33060 只监听本地：

   ```ini
   mysqlx_bind_address = 127.0.0.1
   ```

6. **`skip_name_resolve = ON` 的连带影响**：以后建账号必须用 IP 或 `localhost`，用域名会匹配不上。

---

*本文基于一次真实的 CentOS 7 + MySQL 8.4.10 二进制部署过程整理，所有命令均实际执行验证。*
