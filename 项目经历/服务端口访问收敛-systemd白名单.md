# 服务端口访问收敛：只允许网关访问（systemd `IPAddressAllow` 方案）

> **一句话**：服务仍监听 `0.0.0.0`，但在 systemd 层用 `IPAddressAllow`/`IPAddressDeny` 把**来源 IP** 收敛到白名单——只让网关（和本机回环）连得进来。
> **适用**：服务器上没有 firewalld / iptables / nftables，或不想改监听地址、不想动代码，只要"外部连不上、只有网关能调"。
> **实测环境**：RHEL 系 Linux（systemd 252 及以上），两个 Python 后端服务。

> **说明**：本文中 `<网关IP>`、`<DB-IP-x>`、`<服务A>`、`<unit>` 等为占位符，按实际环境替换；文末附替换清单。

---

## 一、使用场景

| 场景 | 是否适用 |
| --- | --- |
| 服务只应被内部网关调用，不希望其他主机直连 | ✅ 首选 |
| 机器上没有任何防火墙工具（无 firewalld/iptables/nft） | ✅ 唯一轻量方案 |
| 不想改 `--host 0.0.0.0`（改了会打断部署自检、客户端配置） | ✅ 不动绑定 |
| 需要按来源 IP 做白名单（而不是按端口） | ✅ |
| 需要限制"服务能往哪出站"（比如只允许连指定数据库） | ✅（本方案是**双向**的，见坑 2） |
| 需要按路径／按用户鉴权 | ❌ 用应用层或网关做 |

### 为什么不用其他方案

| 方案 | 问题 |
| --- | --- |
| 改 `--host <本机IP>` | 只去掉 `0.0.0.0` 字面量，**不限制来源**；且本机 `127.0.0.1` 自检会挂 |
| firewalld rich rule | 机器上没装（本次就是这种情况） |
| iptables/nftables | 同样没装；且要额外处理规则顺序与持久化 |
| 应用层白名单 | 端口仍全网可达，只做到"连上就 403"；是兜底不是收敛 |

---

## 二、前置检查（3 条，只读）

```bash
# 1) 有没有防火墙工具
command -v firewall-cmd iptables nft ufw

# 2) systemd 版本（需 ≥ 235）
systemctl --version | head -1

# 3) 服务监听在哪
ss -lntp | grep -E ':8000|:8020\s'
```

判定：**tool 全无 + systemd ≥ 235 → 用本方案**；若 systemd < 235，改用 nftables 或应用层白名单。

---

## 三、通用命令（模板，照抄改 3 处占位符）

> 占位符：`<unit>` = systemd 单元名；`<allow-ips>` = 允许的 IP 列表；`<port>` = 端口（仅用于校验）

```bash
UNIT=<unit>                       # 例：<服务A>.service

# ① 建 drop-in 目录（不要直接改主 unit，会被部署脚本覆盖）
mkdir -p /etc/systemd/system/${UNIT}.d

# ② 写白名单（入站白名单 + 出站依赖，必须都写）
cat > /etc/systemd/system/${UNIT}.d/10-allowlist.conf <<'EOF'
[Service]
IPAddressAllow=<allow-ips>
IPAddressDeny=any
EOF

# ③ 生效
systemctl daemon-reload
systemctl restart ${UNIT}

# ④ 校验（两条线索都要对）
systemctl is-active ${UNIT}
systemctl show ${UNIT} -p IPAddressAllow -p IPAddressDeny
```

### 校验期望值

```
IPAddressAllow=127.0.0.1/32 <网关IP>/32 <出站依赖...>
IPAddressDeny=0.0.0.0/0 ::/0
```

### 回滚（30 秒）

```bash
rm /etc/systemd/system/${UNIT}.d/10-allowlist.conf
systemctl daemon-reload
systemctl restart ${UNIT}
```

---

## 四、验证方法

```bash
# ① 本机回环（部署自检依赖，必须在服务器上执行）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/healthz        # 期望 200

# ② 出站依赖是否被误杀（例：DB 连通性，只做 TCP 握手，不执行 SQL）
python3 - <<'PY'
import socket
for h in ("<DB-IP-1>", "<DB-IP-2>"):
    s = socket.socket(); s.settimeout(3)
    try: s.connect((h, 3306)); print(h, "OK")
    except Exception as e: print(h, "FAIL", e)
    finally: s.close()
PY
```

**负向验证（关键）**：必须在**另一台机器**上做——

```bash
nc -zv -w3 <服务器IP> <port>      # 期望超时（不是 refused）
```

> ⚠️ 在服务器自己身上 `curl <服务器IP>:<port>` **测不出拦截效果**：`IPAddressAllow` 只作用于**服务进程所在的 cgroup**，你在 shell 里发起的 curl 走 sshd 的 cgroup，不受该规则约束。

---

## 五、具体举例（脱敏版）

### 场景描述

两个后端服务跑在同一台服务器上，都由**内部网关**转发调用，且都要**出站连接 3 台数据库**取元数据：

| 服务 | 端口 | 出站依赖 |
| --- | --- | --- |
| `<服务A>`（`<unit-A>`） | `8020` | `<DB-IP-1>`、`<DB-IP-2>`、`<DB-IP-3>` |
| `<服务B>`（`<unit-B>`） | `8000` | 同上 3 台 |

要求：**入站只允许网关 `<网关IP>` + 本机回环 `127.0.0.1`**；出站只允许这 3 台数据库。

### 配置（两个服务一样）

```bash
for UNIT in <unit-A> <unit-B>; do
  mkdir -p /etc/systemd/system/${UNIT}.d
  cat > /etc/systemd/system/${UNIT}.d/10-allowlist.conf <<'EOF'
[Service]
# 入站：网关 + 本机回环；出站：三台数据库
IPAddressAllow=<网关IP> 127.0.0.1 <DB-IP-1> <DB-IP-2> <DB-IP-3>
IPAddressDeny=any
EOF
  systemctl daemon-reload
  systemctl restart ${UNIT}
done
```

### 校验与实测结果

```bash
ss -lntp | grep -E ':(8000|8020)\s'          # 仍旧 0.0.0.0（预期，绑定未变）
systemctl show <unit-A> <unit-B> -p IPAddressAllow -p IPAddressDeny
curl -s -o /dev/null -w '8000=%{http_code}\n' http://127.0.0.1:8000/healthz   # 200
curl -s -o /dev/null -w '8020=%{http_code}\n' http://127.0.0.1:8020/healthz   # 200
```

| 验证项 | 结果 |
| --- | --- |
| 外部主机直连 `<服务器IP>:8000` | 超时（改前 200）✅ |
| 外部主机直连 `<服务器IP>:8020` | 超时（改前 200）✅ |
| 经网关调用业务接口 | 200，返回正常 ✅ |
| 元数据实时查询（出站 DB） | 200，取到实时行数 ✅ |

---

## 六、踩坑清单（都是真实踩过的）

| # | 坑 | 现象 | 正确做法 |
| --- | --- | --- | --- |
| 1 | **改了主 unit，部署后被覆盖** | 重部署后限制消失 | 用 `*.service.d/10-allowlist.conf` drop-in，部署脚本不碰它 |
| 2 | **`IPAddressAllow` 是双向的** | 限制生效后元数据全变 ⏸/报错（服务连不上 DB） | 出站依赖（DB、认证服务、DNS）必须一并加进 Allow |
| 3 | 漏了 `127.0.0.1` | 部署自检、healthz 全挂 | 永远保留回环 |
| 4 | 只写 Allow 不写 `IPAddressDeny=any` | 白名单不生效（仍全网可达） | 两个必须成对出现 |
| 5 | 在服务器本机做负向测试 | 仍能连上，误判"没生效" | 负向测试换另一台机器做 |
| 6 | 用 `REJECT` 思路评价效果 | 期望"connection refused" | systemd 层面是**丢包**，外部表现为超时 |
| 7 | 只校验端口监听 | `ss` 仍显示 `0.0.0.0`，以为没生效 | 校验要看 `systemctl show -p IPAddressAllow` |

---

## 七、变更留痕模板（复制进工单）

> **变更**：`<服务名>` `<端口>` 对外收敛：由 `0.0.0.0` 全网可达改为 systemd `IPAddressAllow` 白名单（入站仅网关 `<网关IP>` + 本机回环；出站仅 `<依赖清单>`），`IPAddressDeny=0.0.0.0/0 ::/0` 兜底。
> **依据**：内部安全规范"不得允许调用方直连后端服务"。
> **验证**：外部主机直连超时；经网关调用 200；本机 healthz 200；出站依赖 TCP 握手 OK。
> **影响**：端口、契约、网关注册均未变，无需重注册；调用方使用方式不变。
> **回滚**：删除 `10-allowlist.conf` → `daemon-reload` → `restart`。
> **约束**：后续新增出站依赖（如新增数据库）必须同步加入 Allow，否则该依赖会静默不可用。

---

## 附：占位符替换清单

| 占位符 | 含义 | 替换为 |
| --- | --- | --- |
| `<unit>` / `<unit-A>` / `<unit-B>` | systemd 单元名 | 如 `xxx.service` |
| `<服务A>` / `<服务B>` | 服务标识 | 实际服务名 |
| `<网关IP>` | 只允许入站的来源 | 网关地址 |
| `<服务器IP>` | 被测服务器地址 | 本机业务地址 |
| `<DB-IP-1..3>` | 出站允许的数据库地址 | 实际 DB 主机 |
| `<port>` | 服务端口 | 如 `8000`、`8020` |
