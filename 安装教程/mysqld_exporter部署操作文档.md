# mysqld_exporter 部署操作文档

> 版本：v0.15.1（linux-amd64）
> 适用：单机 MySQL 主机部署采集器，Prometheus 抓取 `:9104/metrics`
> 说明：文中 `<DB_HOST_IP>` 替换为目标数据库主机 IP；密码已脱敏，实际使用时替换为 20 位随机字母数字密码（如 `********`）。

## 1. 下载与安装

```bash
cd /tmp
wget https://github.com/prometheus/mysqld_exporter/releases/download/v0.15.1/mysqld_exporter-0.15.1.linux-amd64.tar.gz

tar -xf mysqld_exporter-0.15.1.linux-amd64.tar.gz

mkdir -p /opt/monitoring/mysqld_exporter
mv mysqld_exporter-0.15.1.linux-amd64 /opt/monitoring/mysqld_exporter/

# 软链指向目录内的二进制（不要指向目录本身）
ln -s /opt/monitoring/mysqld_exporter/mysqld_exporter-0.15.1.linux-amd64/mysqld_exporter \
      /opt/monitoring/mysqld_exporter/mysqld_exporter

/opt/monitoring/mysqld_exporter/mysqld_exporter --version
```

> 若服务器无法访问外网，可从已部署机器 scp 整个包再解压。

## 2. 创建监控用户（在目标 MySQL 上执行）

登录数据库：

```bash
mysql -uroot -p
```

执行 SQL（密码替换为实际值）：

```sql
create user 'exporter_user_r'@'%' identified by '********';

grant select, process, replication client on *.* to 'exporter_user_r'@'%';

flush privileges;
```

## 3. 采集器配置文件

```bash
vi /opt/monitoring/mysqld_exporter/.my.cnf
```

内容：

```
[client]
user=exporter_user_r
password=********
host=127.0.0.1
port=3306
```

设置权限（防止明文密码被其他用户读取）：

```bash
chmod 600 /opt/monitoring/mysqld_exporter/.my.cnf
```

## 4. 启动与验证

```bash
nohup /opt/monitoring/mysqld_exporter/mysqld_exporter --config.my-cnf=/opt/monitoring/mysqld_exporter/.my.cnf > /opt/monitoring/mysqld_exporter/exporter.log 2>&1 &
```

> 注意：`--config.my-cnf=` 等号后不能有空格。

验证：

```bash
# 进程
ps -ef | grep mysqld_exporter | grep -v grep

# 端口
ss -lntp | grep 9104

# 指标（mysql_up 应为 1）
curl -s http://127.0.0.1:9104/metrics | grep -E '^mysql_up |^mysql_global_status_uptime '
```

## 5. 可选：systemd 托管（推荐生产使用）

```bash
cat > /etc/systemd/system/mysqld_exporter.service <<'EOF'
[Unit]
Description=mysqld_exporter
After=network.target

[Service]
User=root
ExecStart=/opt/monitoring/mysqld_exporter/mysqld_exporter --config.my-cnf=/opt/monitoring/mysqld_exporter/.my.cnf
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mysqld_exporter
systemctl status mysqld_exporter --no-pager
```

（启用 systemd 后，停掉第 4 步的 nohup 进程，避免端口冲突。）

## 6. Prometheus 抓取配置（Prometheus 主机上）

在 scrape_configs 中新增：

```yaml
  - job_name: 'prod-mysql-dba'
    static_configs:
      - targets: ['<DB_HOST_IP>:9104']
    relabel_configs:
      - source_labels: [__address__]
        regex: '([^:]+):.*'
        target_label: ip
        replacement: '$1'
```

校验并 reload：

```bash
promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://<PROMETHEUS_HOST>:9090/-/reload
```

验收：

```promql
up{instance="<DB_HOST_IP>:9104"}      -- 应为 1
mysql_up{ip="<DB_HOST_IP>"}           -- 应为 1
```

## 附：常见问题

| 现象 | 排查 |
|---|---|
| 9104 未监听 | 查看 `cat /opt/monitoring/mysqld_exporter/exporter.log` |
| `mysql_up 0` | 账号/密码与数据库不一致；`.my.cnf` 的 host/port 错误 |
| Prometheus 抓取失败 | 防火墙放行 Prometheus → `:9104`；确认 target 状态 UP |
| `ACCESS DENIED` | 检查监控用户权限（SELECT / PROCESS / REPLICATION CLIENT） |
