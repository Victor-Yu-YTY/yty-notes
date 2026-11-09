# 规范变更记录

| 日期 | 规范版本 | 变更内容 | 同步动作 |
| --- | --- | --- | --- |
| 2026-11-06 | 2026-11-03 | **v1.4.0 修订 3（服务侧加固，规范条款未变）**：①`ReviewResult.ask()` 增加"残留 `{占位符}` 即回落 fallback"闸门——目录话术与调用方字段不匹配时绝不把花括号发给研发；②`Q-CTX-STRUCT`/`Q-COMMENT` 两处调用点补齐字段（此前 218 上会显示 `{column}`）；③`scripts/mcp_client_test.py` 的审计断言改为：拒绝码/拒绝说明/鉴权拒绝等只校验**本次运行**新增记录，而 `event=startup` 查**全量历史**（启动审计必然早于测试，之前误报 71 条）；④`scripts/selftest.py` 增至 **58 项**（新增：目录话术按字段渲染、残留占位符回落、questions.yaml 占位符白名单）；⑤`deploy.sh` 段落标题与头部注释对齐（58 项 / 218 正式机） | 规范文本未变，仅服务侧与自检脚本修正；包重打（`sql-review-mcp-v1.4.0-2.218.tar.gz`） |
| 2026-11-06 | 2026-11-03 | **v1.4.0 修订（评审改进，条款本身未变）**：①`/healthz` 与审计改读**实时**规则集（原来读启动时缓存，改 md 后版本/指纹不更新，无法自证"按哪版判的"）②`config/STANDARD_VERSION` 按 mtime 惰性重读 ③`/healthz` 不再暴露安装路径 ④工具类型不符在**取元数据之前**就判掉（不白跑库查询）⑤单条检查抛异常改为落 `ENGINE-03` ⏸ 未判定（不再静默漏审）⑥规则小节支持 `**样例**：命中/通过`，由 `scripts/check_specs.py` 逐条真跑（本轮内置 50 条）⑦`standards/README.md` 新增"规则执行顺序 = 文档顺序"的依赖契约 | 文档新增 `**样例**` 字段（`ddl.md` 35 条、`dml.md` 15 条）；`check_specs.py` 增加样例回归与 `--no-samples`；`selftest.py` 增至 55 项；`audit_selftest.py` 补新拒绝码/stage/fatal 断言 |
| 2026-11-06 | 2026-11-03 | **v1.4.0 规则全文档化（条款本身未变）**：`ddl.md` 与 `dml.md` 改为**同构骨架**（`### 编号 标题` + 级别/适用/检查或判定/参数/条款/说明/整改/情形/自动改写/反问/停用）；**级别、说明、整改、情形文案、阈值、参数、适用范围、停用开关全部从 md 读**，代码只保留判定原语（DDL 检查 30 个 + DML 事实与表达式引擎）。改规则只改 md，不用改代码、不用重启；新增同类规则（如再加一个禁用类型）零代码。`mysql.md` 增补《条款与规则对照表》（逐条标注落到哪条规则、或为何不审核）。新增 `standards/README.md`（改规则指南） | 新增 `src/sql_review_mcp/{spec,engine,ddlchecks}.py`、`standards/README.md`、`scripts/check_specs.py`；删除 `src/sql_review_mcp/{standard,rules,dmstandard,dmengine}.py`、`scripts/check_standard.py`；`questions.yaml` 话术改为可带占位符并由服务实时读取 |
| 2026-10-29 | 2026-10-29 | 初版：按《MySQL 使用规范》抽取 TB/IX/AL/DX/SC 五类共 29 条规则，覆盖建表、索引、变更、破坏性操作与规模阈值 | 首次建立 |
| 2026-10-29 | 2026-10-29 | 正式版 v1.0.0：160 测试期验证通过后定稿（规则集与 v0.1.1 一致，未改规范条款） | 未变 |
| 2026-10-29 | 2026-10-29 | **更名：dba-ddl-mcp → SQL-Review（技术标识 sql-review-mcp）**，功能与规则集不变 | 未变 |
| 2026-11-03 | 2026-10-29 | **v1.3.0：新增 DML 审核（INSERT / UPDATE）**，规范条款未改。DML 规则独立成 `standards/dml.md`（22 条规则 + 2 条允许项，判定写成一行表达式，mtime 热加载）；新增第 9 个工具 `review_dml`；`DELETE` / `REPLACE INTO` 一律驳回；**只报错不改写**。`get_rules` 增加 `scope=all|ddl|dml` | 新增 `standards/dml.md`、`src/sql_review_mcp/{dml,dmfacts,dmstandard,dmengine}.py`、`scripts/probe_dml.py` |
| 2026-11-03 | 2026-10-29 | **规范文件三件套：规则来源统一到 `standards/`，改规则只改这一处**。`mysql.md` 保留为通用总纲（人读、可追溯，不参与抽取）；新增 `standards/ddl.md`（DDL 判定细则，抽取结果与原 `mysql.md` 逐条核对完全一致：35 条 / missing 0 / 阈值不变，仅 `uncovered` 因 DML 条款移交而由 8 条降为 5 条）与 `standards/dml.md`；`config/rules_dml.yaml` 退场 | 新增 `standards/{ddl,dml}.md`、`src/sql_review_mcp/dmstandard.py`；删除 `config/rules_dml.yaml`；`settings.yaml` 的 `standard` 段改为 `ddl:` / `dml:` |
| 2026-11-05 | 2026-10-29 | **v1.2.1 修订（规范未改）**：规则清单补齐至 35 条（新增 `TB-15` 抽取修复 + `IX-05`/`IX-06`/`AL-07`/`AL-08`/`DX-03`/`DX-04`/`SC-02` 按代码内置登记）；非 DDL 不再给 `verdict=pass`；`CREATE INDEX` 的 `sql_final` 真正改写；元数据请求级缓存 | 规范文本未变，仅服务侧修正 |
| 2026-11-05 | 2026-10-29 | **v1.2.0：规则改为运行期从 `standards/mysql.md` 抽取**（`src/sql_review_mcp/standard.py`）。规范条款本身未改；`config/rules.yaml` 退场，规则清单改为抽取结果（27 条：24 条抽自规范 + 3 条代码内置硬约束），阈值优先取规范原文、`settings.yaml` 仅作兜底 | 删除 `config/rules.yaml`；新增 `config/questions.yaml`、`config/rules.override.yaml`、`scripts/check_standard.py` |

> **更新流程（v1.4.0 起，改规则只改文档）**
> 1. 改规则只动 `standards/ddl.md`（DDL）或 `standards/dml.md`（DML）——**唯一改动点**；
>    改级别 / 改阈值 / 改文案 / 改情形 / 停用 / 加同类规则，全部在这一步完成；
>    `standards/mysql.md` 是通用总纲（人读 + 条款对照表），改它不影响判定
> 2. 跑 `PYTHONPATH=src python3 scripts/check_specs.py`：**必须退出码 0**
>    - 报错会带「文件名 + 第 N 行 + 可用值」（未知检查名 / 占位符不存在 / 情形名不属于该检查 /
>      阈值拼错 / 允许项冲突 / 缺适用），按提示直接改那一行即可
> 3. 跑 `PYTHONPATH=src python3 scripts/selftest.py`：应 **58/58 通过**（含"只改 md 就不改代码生效"的验证；本机缺 PyYAML 时 57/57）
> 4. 若有条款调整，同步升 `config/STANDARD_VERSION`，并在本文件追加一行
> 5. **不用重启**（mtime 热加载，下次调用即生效）；只有改了代码才 `systemctl restart sql-review-mcp`，
>    然后 `curl /healthz` 看 `rules_count` / `dml_rules_count` / `rules_version` → 再跑 `scripts/selftest.py`
>
> 说明：文档写坏时服务**不切换**，继续用上一版并在返回体带 `warnings`；改坏后又重启 →
> **拒绝启动**（退出码 2），不允许"带着旧逻辑悄悄上线"。
> 不在本服务职责内的条款（`SELECT` 查询类、审核/工单流程类）在 `mysql.md` 对照表里标为「不审核」，
> `get_rules` 的 `uncovered_forced_clauses` 会如实列出，不假装覆盖。
> 改规则的详细指南见 `standards/README.md`。
