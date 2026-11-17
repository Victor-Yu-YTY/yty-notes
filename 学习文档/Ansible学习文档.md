# Ansible 学习文档

> 目标：零基础快速建立整体认知，看懂团队里在用的 playbook。

---

## 一、Ansible 是什么

Ansible 是一个 **IT 批量自动化工具**。它的核心特点就一句话：

**在一台机器上装 Ansible，就能通过 SSH 批量控制几百上千台机器，不用在被控制的机器上装任何 Agent。**

- 装 Ansible 的那台机器叫 **控制节点（Control Node）**。
- 被控制的机器叫 **受管节点（Managed Node）**，只有 SSH 服务 + Python，别的都不用。
- 控制节点发出的不是脚本，而是 **Playbook（YAML 格式的"想要什么状态"声明）**。

---

## 二、能干什么

| 场景 | 举例 |
|---|---|
| 批量改配置 | 改 sshd_config 的白名单、统一装 Nginx |
| 批量装软件 | 给 100 台 CentOS 装 Java |
| 批量采集信息 | 把一堆物理机的 CPU/内存/磁盘导出一张 CSV |
| 批量发版 | 先停旧服务 → 拉新代码 → 起新服务 → 验活 |
| 权限管理 | 往 authorized_keys 里加公钥、删公钥 |
| 云资源编排 | 结合 cloud module 建/销毁云主机 |

核心价值：**把"一台台登录、一条条敲命令"变成"写一份 YAML、执行一条命令、出结果"。**

---

## 三、核心概念速查

| 概念 | 一句话解释 | 类比 |
|---|---|---|
| Inventory | 主机清单，列出要管哪些 IP，还能分组 | 通讯录 |
| Module | 实际干活的单元，如 `copy`、`service`、`yum`、`command` | 螺丝刀、扳手 |
| Task | 清单里的一条动作，调用一个 module | 拧一颗螺丝 |
| Playbook | YAML 文件，一串 Task 的集合，描述"最终状态" | 说明书 |
| Role | 把 Task、模板、变量打包复用的目录结构 | 工具箱 |
| Ad-hoc | 不写 playbook，一条命令直接批量执行 | 临时拿螺丝刀拧一下 |

### 一个最小 Inventory 例子（`hosts.yml`）

```yaml
[hw_collection_targets]
10.0.2.11
10.0.2.12

[onboarding_vms]
10.0.1.101
10.0.1.102
```

### 一个最小 Playbook 例子（`ping.yml`）

```yaml
---
- name: 测试连通性
  hosts: onboarding_vms
  tasks:
    - name: ping 这批机器
      ansible.builtin.ping:
```

执行：`ansible-playbook -i hosts.yml ping.yml`

### 一条 Ad-hoc 命令

```bash
ansible onboarding_vms -m ping -i hosts.yml          # 测 SSH 通不通
ansible onboarding_vms -m shell -a "hostname" -i hosts.yml   # 批量跑一条命令
```

---

## 四、真实场景 1：SSH 公钥推送 + 白名单加固

> 来自团队一次实际操作记录

**背景**：给两台新接入的虚拟机（10.0.1.101、10.0.1.102）打通运维通道，同时用白名单收紧访问来源。

### 为什么要用 Ansible 而不是手动？

两台机器手动敲其实也快，但这一套要改的是 SSH 配置，**改错一步就把自己关在门外**。Ansible 能：

- 先 `--syntax-check` 校验，避免语法错误；
- 先 `--limit` 一台试跑，确认无误再全量；
- 动作和回滚脚本成对存在（加公钥 / 删公钥、开白名单 / 撤销白名单），安全有退路；
- 增量式改 `authorized_keys`，不会覆盖掉其他团队已经加进去的 key。

### 三步 playbook 设计

#### ① ssh_add_key.yml：增量式添加 SSH 公钥

作用：把运维公钥追加进 `authorized_keys`，但不覆盖原有内容。

```yaml
---
- name: 增量式添加 SSH 公钥
  hosts: onboarding_vms
  tasks:
    - name: 追加指定公钥
      ansible.posix.authorized_key:
        user: root
        state: present
        key: "{{ lookup('file', '~/.ssh/deploy_key.pub') }}"
        # 注意这里没有 exclusive: true，所以是"增量"
```

对应 Ansible module：`ansible.posix.authorized_key`，幂等、增量。

#### ② ssh_whitelist.yml：硬控 SSH 白名单

作用：把 `sshd_config` 的 `AllowUsers` 收敛为"只允许从 10.0.0.1（堡垒机）登录"。

```yaml
---
- name: 硬控 SSH 白名单
  hosts: onboarding_vms
  tasks:
    - name: 写入 AllowUsers
      ansible.builtin.lineinfile:
        path: /etc/ssh/sshd_config
        regexp: '^AllowUsers'
        line: 'AllowUsers root@10.0.0.1'
        state: present

    - name: 校验 sshd 配置语法
      ansible.builtin.command: sshd -t
      changed_when: false

    - name: 重载 sshd
      ansible.builtin.service:
        name: sshd
        state: reloaded
```

这一步叫"硬控"，因为它把配置**强制收敛到模板值**，不管原来被谁改过什么。改完必须 `sshd -t` + reload，否则不生效。

#### ③ ssh_del_key.yml：增量式删除这次加的公钥

收尾动作，用完的公钥就撤，缩小暴露面：

```yaml
---
- name: 增量式删除 SSH 公钥
  hosts: onboarding_vms
  tasks:
    - name: 移除指定公钥
      ansible.posix.authorized_key:
        user: root
        state: absent
        key: "{{ lookup('file', '~/.ssh/deploy_key.pub') }}"
```

注意：它删的是**这次加的那一把**，其他团队的 key 不会被误删。

### 实际执行顺序（先加通道 → 再加限制 → 最后收尾）

```bash
# 1. 语法校验
ansible-playbook -i hosts.yml ssh_add_key.yml --syntax-check
ansible-playbook -i hosts.yml ssh_whitelist.yml --syntax-check
ansible-playbook -i hosts.yml ssh_del_key.yml --syntax-check

# 2. 先在一台上试跑
ansible-playbook -i hosts.yml ssh_add_key.yml --limit 10.0.1.101

# 3. 全量执行
ansible-playbook -i hosts.yml ssh_add_key.yml
ansible-playbook -i hosts.yml ssh_whitelist.yml
ansible-playbook -i hosts.yml ssh_del_key.yml
```

### 三步验证（防翻车）

```bash
# Ansible 层面：SSH + Python 都通
ansible onboarding_vms -m ping -i hosts.yml

# 密钥层面：禁密码后，纯公钥能不能登
ssh -o BatchMode=yes root@10.0.1.101 'hostname'

# 配置层面：AllowUsers 真生效了（sshd -T 读的是解析后的有效配置）
ssh root@10.0.1.101 'sshd -T | grep -i allowusers'
```

**顺序绝对不能乱**：先保证有可用的登录方式，再加白名单；白名单一旦生效，不在白名单里的来源会立刻被踢掉。

---

## 五、真实场景 2：线下物理机硬件信息批量采集

> 来自团队另一份实际操作记录

**背景**：给一批刚下线的物理机（还没装任何 Agent）采集整机 / 主板 / BIOS / CPU / 内存 / 磁盘 / RAID 卡 / 网卡 / 电源信息，最后汇总成一张 CSV。

### 流程总览

```
写采集 Playbook → 推 GitLab → 群刷机 git pull
        → --syntax-check 语法校验
        → --limit 单机试跑
        → 小批量验证
        → 全量批量采集
        → 拿回 CSV 导入数据库
```

### 关键 playbook 片段（示意）

```yaml
---
- name: 采集物理机硬件信息
  hosts: hw_collection_targets
  tasks:
    - name: 整机 / 主板 / BIOS
      ansible.builtin.command: dmidecode -t system,baseboard,bios
      register: hw_system

    - name: CPU
      ansible.builtin.command: lscpu
      register: hw_cpu

    - name: 内存
      ansible.builtin.command: dmidecode -t memory
      register: hw_mem

    - name: 磁盘
      ansible.builtin.command: lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
      register: hw_disk

    # ... RAID 卡、网卡、电源同理

    - name: 汇总成 CSV 行
      ansible.builtin.template:
        src: hw_row.j2
        dest: /tmp/hw_inventory/{{ inventory_hostname }}.csv

    - name: 从每台机器拉回控制节点
      ansible.builtin.fetch:
        src: /tmp/hw_inventory/{{ inventory_hostname }}.csv
        dest: ./collected/
        flat: true
```

### 为什么这个场景特别适合 Ansible

1. **机器上没有 Agent**，没法用 Salt/Puppet；Ansible 只要 SSH 能连就行。
2. **一次性批量操作**，不需要持续后台守护进程。
3. **命令写一次，改一处即生效全组**，比手动在每台机器上跑脚本可靠太多。
4. **`register` + `template` + `fetch`** 这一套让"每台一份结构化输出"成为可能，最后拼起来就是一张表。

---

## 六、常用命令速查

```bash
ansible <group> -m ping                                    # 测 SSH + Python
ansible <group> -m shell -a "hostname"                     # 批量跑命令
ansible-playbook playbook.yml --syntax-check               # 语法校验
ansible-playbook playbook.yml --limit <ip或组名>           # 限定范围试跑
ansible-playbook playbook.yml -v                           # 详细输出 (-vv / -vvv 更详细)
ansible-playbook playbook.yml --check                      # dry-run，只看会改什么
ansible-playbook playbook.yml -e "key=value"               # 传额外变量
ansible-doc <module>                                       # 查看 module 文档
```

---

## 七、常见坑速查

| 现象 | 原因 / 处理 |
|---|---|
| `Permission denied` | 控制节点对目标机 SSH 免密没打通，先手动 `ssh root@ip` 验证 |
| `python not found` | CentOS 7 默认 python2，显式加 `ansible_python_interpreter=/usr/bin/python2`；新系统要先装 python3 |
| 改了 sshd_config 没生效 | 忘了 reload sshd；Ansible 改完配置文件必须自己 reload |
| 白名单写错被锁 | 记住 playbook 都有回滚脚本，先在一台试跑再加白名单；改前一定 `--check` |
| 幂等性 | `command`/`shell` module 天生不幂等，写 `changed_when: false` 或换 `copy`/`authorized_key` 这类原生幂等的 module |

---

## 八、一句话收尾

> Ansible 解决的不是"远程执行"问题（SSH 早就解决了），而是**远程执行之后的"可重复、可验证、可回滚、可审计"**问题。把一堆手工命令收敛成一份 YAML，它的价值才真正显现。
