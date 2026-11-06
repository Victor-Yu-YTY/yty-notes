# MCP 编写参考

> 经验来源：本人已写的 3 个 MCP —— `db-read-mcp`（只读查询）、`sql-review-mcp`（规则评审）、`sql-executor-mcp`（写操作）。
> 一句话：**MCP 就三层——入口注册工具、工具调业务、业务拆成可测试的模块。**

---

## 一、MCP 是什么（10 行速懂）

- 协议：**JSON-RPC 2.0**，客户端和服务端固定三步：
  1. `initialize` 握手 → 拿 `Mcp-Session-Id`（HTTP 传输时）
  2. `tools/list` 发现工具（名称 + 参数 JSON Schema）
  3. `tools/call` 调用 `{name, arguments}` → 返回内容（`isError` 标记失败）
- 传输二选一：
  - **stdio**：客户端把 MCP 当子进程启动（本地工具、Claude Desktop 这类客户端）
  - **streamable HTTP**：服务端部署成 HTTP 服务，`POST /mcp/`（远程/多客户端）
- 一个"工具" = 一个函数：函数签名自动变成 JSON Schema，参数描述就是给 AI 看的说明。

### 客户端配置：配到 Agent 里

我的 MCP 基本都是作为 Agent 的客户端配置接入的，一条配置大概长这样：

```json
{
  "mcpServers": {
    "my-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp/",
      "headers": {"X-MCP-TOKEN": "${MY_TOKEN}"}
    }
  }
}
```

- 每个 MCP 一条，名字就是它在 Agent 里的工具前缀；
- `type` 是传输方式（http 服务直接写 `http`），`url` 带 `/mcp/` 路径；
- 鉴权走 `headers`，token 用 `${VAR}` 占位，**不写死明文**；
- 改完 Agent 侧配置要重新加载才生效。

---

## 二、标准目录结构（从 3 个真实 MCP 归纳）

```text
<mcp-name>/
├── src/<pkg>/
│   ├── __init__.py          # 只放 __version__ 等元信息
│   ├── server.py            # ★ 唯一入口：建 FastMCP、注册工具、启动
│   ├── config.py            # 配置加载（读 YAML/env，做校验）
│   └── <业务模块>.py         # 按职责拆：engine / provider / guard / cache ...
├── config/
│   └── settings.yaml        # 运行参数（端口、路径、环境目标）；密钥不写这里
├── standards/               # 【可选】规则类服务的规则文件（*.md），与代码分离
├── scripts/                 # 【可选】自检/探测脚本（check_*.py、e2e_*.py）
├── tests/                   # 【可选】单元测试
├── README.md                # 怎么跑、目录说明
├── CHANGELOG.md             # 版本变更记录
├── VERSION                  # 版本号（与 __version__ 双写，代码优先读文件）
├── pyproject.toml / requirements.txt   # 依赖（锁版本）
├── .gitattributes           # 强制 *.sh / *.py / *.md 用 LF（跨平台换行统一）
├── .gitignore               # 忽略运行期与产物（logs/ data/ cache/ .venv/ __pycache__/）
└── OWNERS                   # 责任人（谁负责评审/维护）
```

每个目录一句话判据：

| 位置 | 放什么 | 判据 |
|---|---|---|
| `src/<pkg>/server.py` | 工具注册、传输、启动 | **只做"接线"，不写业务逻辑** |
| `src/<pkg>/engine.py` 等 | 业务逻辑 | 可单独 import、可单测、不 import server |
| `config/` | 参数配置 | 改了要重启；**不含任何密钥** |
| `standards/` | 业务规则 md | 改了不用重启（热加载）；非规则类服务不要建 |
| `scripts/` | 自检/探测 | 人工或 CI 按需跑 |
| `VERSION` | 版本 | 与 `__version__` 一致 |
| `.gitattributes` | 换行符策略 | 强制 `*.sh`/`*.py`/`*.md` 用 LF，防止 Windows 下 CR 混入 |
| `.gitignore` | 忽略清单 | 运行期目录、`.venv`、`__pycache__`、`*.tar.gz` 不入库 |
| `OWNERS` | 责任人 | "角色 + 负责范围"，交付/交接时用，评审时照着找人 |

> 原则：**入口薄、业务厚**。server.py 里一个工具函数不超过 5 行——只做"参数→调业务→套返回"。

---

## 三、核心代码怎么设计（从 3 个实现提炼的 7 条经验）

### 1. 最小骨架（server.py 只做接线）

```python
# src/my_pkg/server.py
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-mcp")

@mcp.tool()
def list_items(env: str = "test") -> dict:
    """列出环境下的条目。"""
    return engine.list_items(env)   # 业务全在 engine 里

@mcp.tool()
def get_item(item_id: str) -> dict:
    """按 ID 查询一条。"""
    return engine.get_item(item_id)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")   # 或 "stdio"
```

### 2. 工具函数 = 薄壳：校验 → 调业务 → 统一返回

经验：
- 工具名 = 函数名，**动词开头**（`list_*` / `get_*` / `review_*` / `run_*`）；
- 参数尽量扁平，给默认值；参数 description 写清楚，它会被 AI 读到；
- 工具数量克制：**5~10 个**，一个工具一个明确动作。

### 3. 业务拆成独立模块（关键：能单测）

```
server.py   →  engine.py   →  provider.py   →  外部系统（DB / API）
（接线）      （纯逻辑）      （IO 抽象）
```

- `engine.py`：不依赖网络/数据库，纯计算和判断，**可单测**；
- `provider.py`：把外部访问（数据库连接、HTTP 调用）抽象成接口，测试时用桩替换；
- `guard.py`（可选）：安全校验（只读 SQL 只放 SELECT 之类）。

### 4. 统一返回信封（所有工具一致）

```json
{"ok": true, "tool": "list_items", "env": "test", "elapsed_ms": 1.3,
 "truncated": false, "data": {...}}
{"ok": false, "code": "ARG_INVALID", "message": "..."}
```

- 工具内部**异常不裸抛**，统一转成错误信封（fail-closed）；
- `data` 放业务数据，`ok/code/message` 是骨架字段——调用方无需理解业务也能判断成败。

### 5. 配置与密钥分离

- `config/settings.yaml` 写参数（端口、路径、默认环境）；
- 密钥（token、密码）**只从环境变量读**，不写进配置文件和代码；
- 配置加载后做一次校验（缺必填项就启动失败，不要带病运行）。

### 6. 规则类服务：规则外置（sql-review-mcp 的经验）

- 规则写成 `standards/*.md`（人可读、可改），代码只做"md → 结构化规则"的编译 + 执行；
- **改规则不改代码**，规则文件热加载（按 mtime 判断）；
- 规则每条带 `id / level / 检查条件 / 修复建议`，评审结果按 `verdict` 汇总。

### 7. 写操作类：预检 → 计划 → 执行 → 可回滚（sql-executor-mcp 的经验）

- 任何写操作前先**预检**（参数/权限/上下文是否齐全），缺了就拒绝，不猜；
- 写之前**先落 undo 记录**（flush + fsync），再执行；
- 回滚 SQL **只生成不执行**，返回给调用方；
- 提供 `dry_run` 三态：`off / plan / rollback`。

---

## 四、写新 MCP 前的设计清单（5 个问题）

1. **只读 / 写 / 混合？** 决定是否要 guard 和 rollback 机制；
2. **工具清单**：列出"名称 + 参数 + 返回"，确认没有多余工具；
3. **传输**：自己本地用 → stdio；给别人/多客户端用 → streamable-http；
4. **要不要鉴权**：对外暴露必须带 token（HTTP 头或 env）；本地 stdio 可以不要；
5. **要不要外置规则**：规则会频繁变的（评审/校验类）才建 `standards/`，否则不要。

---

## 五、命名与版本约定

| 对象 | 规则 | 例 |
|---|---|---|
| 包/项目名 | 小写短横线 | `demo-mcp` |
| Python 包 | 小写下划线 | `demo_mcp` |
| 工具名 | 小写字母/数字/下划线，动词开头 | `list_items`、`run_query` |
| 环境名 | 固定档位 | `dev` / `test` / `prod` |
| 版本号 | 主.次.修订 | `0.1.0` |

---

## 六、常见坑（实践经验）

- **工具里直接写业务逻辑** → 不可测、不可复用；拆到 engine；
- **异常裸抛** → 客户端拿不到结构；统一转错误信封；
- **密钥写进配置/代码** → 泄露面大；只读环境变量；
- **返回格式不统一** → 每个工具一个样；固定 `{ok, data, code, message}`；
- **工具太多太碎** → AI 选不明白；按"动作"合并，5~10 个足够；
- **端口/路径写死** → 换环境就崩；进 config；
- **改规则要改代码** → 规则类服务把规则外置成 md，热加载。
