# DeepSeek Harness(DSH)本地部署与使用教程

> DeepSeek Harness(以下简称 **DSH**)是 DeepSeek 官方的「一切皆插件」Agent 编程框架:
> 在本地把 DeepSeek 大模型接成一个能自己写代码、跑命令、查资料、管理文件并持续工作的智能体,
> 通过浏览器 Web 界面(默认 `http://127.0.0.1:3080`)交互。
>
> 本教程以 **Windows + Node.js 全局安装(npm)** 方式为主线,覆盖:
> **安装 → 配置 API Key → 启动(含开机自启)→ 版本检查与升级 → 插件配置**。
>
> 文中命令给出 PowerShell 与 CMD 两种写法,PowerShell 里遇到 `.ps1` 无法加载时按 [第 1.4 节](#14-windows-执行策略报错处理) 处理即可。

**目录**

- [1. 安装 Node.js(环境准备)](#1-安装-nodejs环境准备)
- [2. 全局安装 DeepSeek Harness](#2-全局安装-deepseek-harness)
- [3. 配置 DeepSeek API Key(推荐官方开放平台)](#3-配置-deepseek-api-key推荐官方开放平台)
- [4. 启动 DSH(手动启动 + 每次开机自动启动)](#4-启动-dsh手动启动--每次开机自动启动)
- [5. 检查版本与升级到最新版](#5-检查版本与升级到最新版)
- [6. 配置插件(Plugin)](#6-配置插件plugin)
- [7. 常见问题 FAQ](#7-常见问题-faq)
- [8. 安全与隐私提示](#8-安全与隐私提示)

---

## 0. 你需要了解的几个概念

| 概念 | 说明 |
|---|---|
| `dsh` | 唯一的命令行启动器(全局安装后获得),它本身不干活,只负责"启动某个 profile" |
| **Profile(配置档)** | 一组插件的叠加组合,是 DSH 的"运行形态"。每个 profile 一个目录,位于 `$DSH_HOME/profiles/<名称>` |
| `web` profile | 浏览器 Web 界面形态,命令 `dsh web`(等价于 `dsh --profile web`),也是本教程的主角 |
| 其他内置 profile | `headless`(跑一次任务就退出)、`acp`(自动化客户端)、`sdk` / `sdk-minimal`(SDK 服务) |
| `$DSH_HOME` | DSH 的用户数据根目录。默认是 `C:\Users\<你的用户名>\.dsh`,可用环境变量 `DSH_HOME` 修改 |
| **Bundle / 插件组合包** | 以 npm 包形式分发的一组"配置补丁层"。内置 bundle 来自全局安装,树外插件装在 profile 目录里 |

> 首次运行 `dsh web` 时,会自动在 `$DSH_HOME\profiles\web` 初始化好 web profile(无需手动建目录)。
> 你个人改的配置存放在该目录的 `cordis.patch.yml`(仅对本 profile 生效)与 `$DSH_HOME\cordis.patch.yml`(对所有 profile 生效)中。

---

## 1. 安装 Node.js(环境准备)

### 1.1 下载并安装

1. 打开 Node.js 官网下载页:https://nodejs.org/zh-cn
2. 下载 **LTS(长期支持)版** 的 Windows 安装包(`.msi`),双击安装。
3. 安装向导一路 **Next**,务必确认 **"Add to PATH"** 这一项是勾选状态(默认勾选)。
4. 安装完成后 **重新打开** 一个终端(PowerShell 或 CMD),输入:

```powershell
node -v
npm -v
```

能打印出版本号(例如 `v24.20.0` / `11.x.x`)即安装成功。

> 建议:Node.js 用 20 及以上版本(本教程在 Node v24 上验证通过)。

### 1.2 顺手把 pnpm 也装上(第 6 章插件功能需要)

插件管理是通过 pnpm 在 profile 目录里安装依赖的,`dsh` 检测不到 pnpm 时会提示需要安装:

```powershell
npm install -g pnpm
pnpm -v
```

### 1.3 安装位置提示(Windows)

npm 全局安装的包默认放在 `C:\Users\<你的用户名>\AppData\Roaming\npm`,里面的 `.cmd` 文件(`dsh.cmd`、`npm.cmd`、`pnpm.cmd`)通常已被加入 PATH。**如果出现"无法识别命令",先重新打开终端再试;仍不行则检查 PATH 中是否包含该目录。**

### 1.4 Windows 执行策略报错处理

在 PowerShell 中运行 `npm` / `dsh` / `pnpm` 时,如果看到:

```
npm.ps1 / dsh.ps1 ... cannot be loaded because running scripts is disabled on this system
```

说明 PowerShell 的执行策略禁止运行 `.ps1` 脚本。任选一种解决:

- **推荐(仅对当前用户放开限制,一次性执行):**

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

- 或者改用 **CMD(命令提示符)** / Windows Terminal 的 cmd 模式,`dsh.cmd` 不受执行策略影响;
- 或者用 `dsh.cmd`、`npm.cmd` 的完整写法(在 PowerShell 中直接调用 `.cmd` 版本也能绕过)。

---

## 2. 全局安装 DeepSeek Harness

### 2.1 安装

在终端执行:

```powershell
npm install -g @deepseek-ai/dsh
```

> 包名是 `@deepseek-ai/dsh`(不是 `deepseek-harness`)。官方仓库:https://github.com/deepseek-ai/deepseek-harness
> 版本尚在快速迭代,当前可能是带 `rc` 的预发布版(如 `0.1.2-rc.1`),属正常现象。

### 2.2 验证安装

```powershell
dsh --version
```

能打印出版本号即安装成功。再看一眼帮助信息了解全局命令:

```powershell
dsh --help
```

常用的几个入口:

| 命令 | 作用 |
|---|---|
| `dsh web` | 启动 Web 界面(等价于 `dsh --profile web`),**本教程主用** |
| `dsh --profile headless "你的任务"` | 跑一个一次性任务,打印结果后退出 |
| `dsh --profile <name> --dump-config` | 不启动,直接查看某个 profile 的合成配置树 |
| `dsh plugin --profile <name> add/remove <包>` | 给某个 profile 安装/移除插件(见第 6 章) |

### 2.3 数据目录长什么样

运行过一次后,`%USERPROFILE%\.dsh` 下大致是:

```
C:\Users\<你>\.dsh\
├─ .credentials.yaml      # 本机凭据(如 DeepSeek API Key、插件 Token),值经过脱敏存储
├─ settings.yaml          # 界面/应用设置
├─ sessions\              # 会话历史记录
├─ storages\              # 附件、文件存储
└─ profiles\
   ├─ node_modules
   └─ web\                # web profile:package.json、cordis.patch.yml、node_modules 等
```

---

## 3. 配置 DeepSeek API Key(推荐官方开放平台)

DSH 调用模型需要 DeepSeek 开放平台的 API Key。**建议直接在官网注册、充值、创建密钥**,流程如下。

### 3.1 在 DeepSeek 开放平台创建密钥

1. 打开官网并注册/登录:https://platform.deepseek.com
2. **充值(购买余额)**:平台按实际用量(token 数)计费,先充值再调用。入口通常在控制台的钱包/费用相关页面,支持支付宝等付款方式。
3. 进入 **"API Keys"(API 密钥)** 页面,点击 **"创建 API Key"**,起个名字(如 `dsh-local`)。
4. 创建后把形如 `sk-...` 的密钥**完整复制并妥善保存**(关闭页面后一般不再显示完整内容)。

> 注意:密钥等同"钱包钥匙",只在自己电脑上用,不要发到群里、贴进代码仓库或截图分享。

### 3.2 把密钥交给 DSH(三选一)

#### 方式 A:在 Web 界面里填(最推荐,无需碰文件)

1. 先按 [第 4 章](#4-启动-dsh手动启动--每次开机自动启动) 启动 `dsh web`,打开 `http://127.0.0.1:3080`;
2. 首次进入会引导配置,或进入 **设置(Settings)→ 模型(Models)** 相关页面;
3. 在 DeepSeek 连接/API Key 输入框粘贴 `sk-...` 密钥并保存;
4. 界面里发起一条消息测试,若密钥无效会提示"API 密钥无效 / API key is invalid"。

DSH 会把密钥写入本机 `%USERPROFILE%\.dsh\.credentials.yaml`(内部键名 `DEEPSEEK_API_KEY`),之后无需重复填写。

#### 方式 B:设置环境变量(适合脚本/自启场景)

DeepSeek 模型的配置默认读取环境变量 **`DEEPSEEK_API_KEY`**。设好后重启 `dsh web` 即可:

```powershell
# 临时(当前终端会话有效)
$env:DEEPSEEK_API_KEY = "sk-你的密钥"

# 永久(写入用户环境变量,之后新开终端都生效)
setx DEEPSEEK_API_KEY "sk-你的密钥"
```

> 也可以把密钥写进 `$DSH_HOME\.env`(`DEEPSEEK_API_KEY=sk-...` 一行),DSH 启动时会按 继承环境 > 启动目录 `.env` > DSH 主目录 `.env` 的顺序加载。

#### 方式 C:直接编辑凭据文件

退出 `dsh` 后编辑 `%USERPROFILE%\.dsh\.credentials.yaml`,在对应键(如 `DEEPSEEK_API_KEY`)填入密钥。**该文件由 DSH 维护,格式敏感,改前建议先备份;日常使用优先选方式 A 或 B。**

### 3.3 验证

在 Web 界面新建会话发送一句 `你好`,能正常收到回复即配置成功。若报鉴权失败,回 [3.1](#31-在-deepseek-开放平台创建密钥) 检查密钥是否正确、账户余额是否充足。

---

## 4. 启动 DSH(手动启动 + 每次开机自动启动)

### 4.1 手动启动

1. 打开终端,建议先 `cd` 到你打算作为**默认工作区**的目录(DSH 会把启动时所在目录当作工作区根,例如 `D:\work`、`D:\DSH`);
2. 执行:

```powershell
dsh web
```

3. 第一次会初始化 web profile,然后通常会自动打开浏览器;若没有,手动访问 **http://127.0.0.1:3080**(以启动日志里打印的地址为准,端口若被占用 DSH 会另选并打印)。
4. **保持这个终端窗口开着**——关掉它等于关掉 DSH。要退出时在该窗口按 `Ctrl+C`。

> 小提示:`dsh web` 只在进程存活期间可访问;它需要独占终端。想让它"后台悄悄跑",用下面的开机自启方案即可。

### 4.2 每次开机自动启动(Windows)

推荐二选一。登录 Windows 后 DSH 会自动在后台拉起,浏览器打开 `http://127.0.0.1:3080` 即可使用。

#### 方案 A:启动文件夹 + VBS(简单,无窗口)

1. `Win + R` 输入 `shell:startup` 回车,打开"启动"文件夹;
2. 新建一个文本文件,改名 `dsh-web.vbs`,右键"编辑",粘贴:

```vbs
Set ws = CreateObject("Wscript.Shell")
' 隐藏窗口启动;可把工作目录换成你常用的目录
ws.Run "cmd /c cd /d D:\DSH && dsh web", 0, False
```

3. 保存即可。之后每次登录系统都会**静默后台**启动 `dsh web`。

#### 方案 B:任务计划程序(更稳,开机即起、可单独停止)

打开 **任务计划程序(Task Scheduler)** → 右侧 **创建任务**:

- **常规**:名称填 `DSH Web`;勾选"不管用户是否登录都要运行"(可选)与"使用最高权限运行"(一般不需要);
- **触发器**:新建 → "登录时" 或 "启动时";
- **操作**:新建 →
  - 程序或脚本:`cmd.exe`
  - 添加参数:`/c cd /d D:\DSH && dsh web`
  - 起始于(可选):`D:\DSH`
- 确定保存(可能会要求输入登录密码)。

也可以直接用命令行创建等价任务:

```powershell
schtasks /Create /TN "DSH Web" /TR "cmd /c cd /d D:\DSH && dsh web" /SC ONLOGON /F
```

删除该自启任务:

```powershell
schtasks /Delete /TN "DSH Web" /F
```

> 说明:开机自启本质就是"有人替你执行了 `dsh web`",因此 API Key 配好后(第 3 章)重启电脑即可直接访问界面。

### 4.3 停止 DSH

- 手动启动:到启动它的终端按 `Ctrl+C`;
- 开机自启:任务计划程序里右键任务 → **结束/禁用**;或在任务管理器结束 `node.exe`(注意别误杀其他 node 程序);
- 停止后 `http://127.0.0.1:3080` 将无法访问,属正常现象,重新启动即可恢复。

---

## 5. 检查版本与升级到最新版

### 5.1 查看当前版本

```powershell
dsh --version
```

### 5.2 查看 npm 上发布的最新版本

```powershell
# 只看最新版本号
npm view @deepseek-ai/dsh version

# 查看所有发布标签(含 rc 预发布等)
npm view @deepseek-ai/dsh dist-tags
```

如果 `dsh --version` 的结果落后于上面的 `version`,就可以升级了。

### 5.3 升级到最新版

```powershell
npm install -g @deepseek-ai/dsh@latest
```

升级完成后:

1. **完全退出**正在运行的 `dsh web`(第 4.3 节),再重新执行 `dsh web`;
2. 重新打开页面(必要时 `Ctrl+F5` 强制刷新浏览器缓存)。

> - DSH 的内置 bundle(如 `dsh-base`、`dsh-web-app`)每次都从**全局安装**解析,因此升级 `dsh` 后 profile 自动使用新版本,无需重装 profile;
> - 你自己的配置(`cordis.patch.yml`、`.credentials.yaml` 等)存放在 `$DSH_HOME`,**升级不会动它们**;
> - 如果你给某个 profile 装过**树外插件**,可顺手在 profile 里把它们也更新一下(见 [6.4](#64-更新与移除插件))。

### 5.4 小贴士

- 版本号带 `rc`/`beta` 说明是预发布,功能可能变化较快;升级前如担心兼容性,可先备份 `%USERPROFILE%\.dsh` 目录;
- 遇到升级后行为异常,优先检查:是否还有**旧进程**在跑、页面缓存是否刷新、`dsh --version` 是否已是目标版本。

---

## 6. 配置插件(Plugin)

### 6.1 插件是什么

DSH 里"插件"以 npm 包形式分发,本质是给 profile 叠加的**配置补丁层(bundle)**。分两类:

| 类型 | 来源 | 说明 |
|---|---|---|
| **内置 bundle** | 全局安装的 `@deepseek-ai/dsh` 自带 | 如 `@deepseek-ai/dsh-base`、`@deepseek-ai/dsh-web-app`;写在 profile 的 `package.json` → `dsh.profile.bundles` 里,升级 dsh 时随之更新 |
| **树外插件** | 你自己安装到某个 profile | 装在 `$DSH_HOME\profiles\<profile>\node_modules`,通过 `dsh plugin` 命令用 pnpm 管理 |

### 6.2 前提:pnpm

`dsh plugin` 会把参数原样转发给 pnpm,因此需要先全局安装 pnpm(见 [1.2](#12-顺手把-pnpm-也装上第-6-章插件功能需要)):

```powershell
npm install -g pnpm
```

### 6.3 给 web profile 添加插件

基本命令(在**任意目录**执行即可,profile 由 `--profile` 指定):

```powershell
# 从 npm registry 安装
dsh plugin --profile web add <插件包名>

# 从 GitHub 仓库安装指定版本(真实示例:dsh-grafana 监控面板插件)
dsh plugin --profile web add "github:guhanfei-ai/dsh-grafana#v0.10.1"

# 安装本地/指定路径的插件(相对路径会相对你执行命令的目录解析)
dsh plugin --profile web add D:\my-plugin
```

执行过程:首次会初始化 profile → 在 `$DSH_HOME\profiles\web` 里执行 `pnpm add ...` → 安装完成后自动把"声明了 `dsh.bundle` 的插件"写进 `dsh.profile.bundles` 层列表。

> **重要:添加/移除插件后,需要重启 `dsh web` 让新的 bundle 层在下次启动时生效**(bundle 列表只在启动时解析;运行中改的是 `cordis.patch.yml` 才会热更新)。

### 6.4 更新与移除插件

```powershell
# 更新 profile 的全部依赖(若有插件在新版本里新增了 bundle 声明,会自动激活)
dsh plugin --profile web update

# 更新单个插件
dsh plugin --profile web update <插件包名>

# 移除插件(会同时从 bundles 层列表里去掉)
dsh plugin --profile web remove <插件包名>

# 查看某个包为何被安装 / 列出依赖关系
dsh plugin --profile web why <插件包名>
```

### 6.5 常见提示与处理

- **`warning: xxx declares no dsh.bundle`**:该包没有 bundle 声明,被当作普通依赖安装、不会成为配置层——通常是正常的,只提醒你别指望它改配置;
- **git 托管插件的构建拦截**:带 `prepare` 脚本的 git 插件在安装时会被 pnpm 拦截,按终端提示把打印出的 key 加进 `$DSH_HOME\profiles\web\pnpm-workspace.yaml` 的 `allowBuilds` 里,再重新执行一次即可;
- **没有同名内置 profile 时**:`dsh plugin --profile <任意新名字> add <包>` 会自动初始化一个"仅含 `dsh-base`"的自定义 profile,适合搭自己的组合。

### 6.6 细粒度配置:patch 文件(进阶)

不想装插件、只想微调配置时,编辑 patch 文件即可:

- `$DSH_HOME\profiles\web\cordis.patch.yml` —— 只作用于 web profile;
- `$DSH_HOME\cordis.patch.yml` —— 作用于所有 profile(优先级高于上面的 profile 层)。

内容是一个**顶层 YAML 数组**,每一项是一条"针对指定 id 的配置覆盖/禁用/插入"(支持 `!!js` 表达式,格式与各插件文档一致)。web profile 是 `patchReload: live`,保存后**运行中即热重载**,不必重启;`cordis.yml` 是框架生成的空根配置,**不要手动编辑它**。改坏导致启动失败时,把 patch 文件先改名备份、恢复成空数组 `[]` 即可救回。

---

## 7. 常见问题 FAQ

**Q1:提示 `dsh 不是内部或外部命令` / `无法识别 dsh`**
A:全局目录(`...\AppData\Roaming\npm`)没进 PATH,或终端是修改 PATH 之前开的。重新打开终端;还不行就手动把该目录加入系统 PATH。

**Q2:PowerShell 报 `dsh.ps1 无法加载,禁止运行脚本`**
A:执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`,或改用 CMD / `dsh.cmd`。详见 [1.4](#14-windows-执行策略报错处理)。

**Q3:`http://127.0.0.1:3080` 打不开 / 提示拒绝连接**
A:确认 `dsh web` 进程还活着(自启任务是否被禁用、终端是否被关)。页面地址以启动日志打印为准。

**Q4:发消息报鉴权失败 / API 密钥无效**
A:① 检查 API Key 是否填对(重填方式 A/B/C);② 登录 https://platform.deepseek.com 确认**余额充足**、密钥未删除未禁用;③ 是否用了代理导致连不上 API。

**Q5:升级后界面或行为奇怪**
A:完全退出旧进程再重启;浏览器强制刷新(`Ctrl+F5`);确认 `dsh --version` 为目标版本;必要时备份 `%USERPROFILE%\.dsh` 后处理。

**Q6:想把 profile 恢复出厂**
A:备份 `cordis.patch.yml` 等自定义文件后,删除 `%USERPROFILE%\.dsh\profiles\web` 整个目录,下次 `dsh web` 会按内置模板重新初始化(自定义文件记得放回去)。

**Q7:担心端口被占用**
A:DSH 默认监听 3080;若被占用会在启动日志给出新地址。也可查启动参数帮助:`dsh web --help`。

---

## 8. 安全与隐私提示

- **API Key 属于敏感凭据**,只保存在本机 `$DSH_HOME\.credentials.yaml` / 环境变量中;不要在公开场合分享,泄露后第一时间去开放平台**删除并重建**密钥;
- 密钥默认只读取,不经你确认不会把 `sk-` 值打印到日志(界面显示时也会脱敏);
- 如不想上报使用遥测数据,可设置环境变量 `DSH_TELEMETRY_DISABLED=1`(**任意非空值即视为关闭**),设好后重启 `dsh web` 生效;
- 升级、增删插件前对 `%USERPROFILE%\.dsh` 做一次拷贝备份,成本最低、最稳妥。

---

*本文基于 `@deepseek-ai/dsh 0.1.2-rc.1`(npm 最新发布)与 Windows 实测环境编写;DSH 迭代较快,命令细节以 `dsh --help`、`dsh web --help` 及官方仓库为准。*
