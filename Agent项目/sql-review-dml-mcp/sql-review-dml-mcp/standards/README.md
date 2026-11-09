# 规则文档使用说明（改规则只看这一页）

本目录下的三份文档是**规则唯一来源**：

| 文件 | 角色 | 是否参与判定 | 谁来改 |
| --- | --- | --- | --- |
| `ddl.md` | DDL 判定细则（35 条规则） | ✅ 是（`spec.py` + `ddlchecks.py`） | DBA |
| `dml.md` | DML 判定细则（22 条规则 + 2 条允许项） | ✅ 是（`spec.py` + `engine.py`） | DBA |
| `mysql.md` | 通用总纲（人读 + 条款对照表） | ❌ 否（只做追溯） | DBA |
| `CHANGELOG.md` | 规范变更记录 | ❌ | DBA |

**核心承诺：改 `ddl.md` / `dml.md` 就能改规则，不用改代码、不用重启服务。**

> ★ **本包（sql-review-dml-mcp v1.0.0）只带 `dml.md` + 总纲 `mysql.md`**：DDL 判定细则
> `ddl.md` 与 DDL 工具都在另一个服务 **SQL Review DDL（sql-review-ddl-mcp，8010）** 的包里。
> 改 DML 规则改本包的 `standards/dml.md`；改 DDL 规则去 DDL 那个包改，两边互不影响。

> **注意"规则数"与"条款数"不是一回事**：DDL 规范条款共 53 条，其中多条款合并成一条规则（如 `IX-01` 合并 IDX_/UNIQ_/大写三条）、
> 判不了的建议类条款不产出规则、规范没逐字写但审核必须有的动作以代码内置补齐，最终是 **35 条可执行规则**——改规则时按"规则（`### XX-nn` 小节）"为单位改，不要按条款条数去核对。
（判定逻辑本身在代码里，但"判成什么级别、用什么话术、卡什么阈值、要不要这条规则"全在文档。）

---

## 一、规则小节的标准写法

两条文档用**同一套骨架**，只有「判定」这一行不同：

```markdown
### TB-03 禁用 FLOAT / DOUBLE 存浮点
- **级别**：❌
- **适用**：create_table
- **检查**：forbidden_types                       ← DDL 用这个
- **参数**：types = FLOAT, DOUBLE, REAL; fix_mode = decimal
- **条款**：存储浮点数必须使用 DECIMAL，禁止使用 FLOAT 和 DOUBLE
- **说明**：禁止使用 {old} 存储浮点数，必须用 DECIMAL
- **整改**：已改为 {replace_with}，请确认精度
- **自动改写**：true
```

```markdown
### DM-05 UPDATE 必须带 WHERE
- **级别**：❌
- **适用**：update
- **判定**：has_where = false                      ← DML 用这个
- **依赖**：table_exists                          ← 可选：缺这些事实就标 ⏸
- **条款**：UPDATE 必须有 WHERE 条件，禁止全表更新
- **说明**：UPDATE 缺少 WHERE，会更新全表
- **整改**：补 WHERE 条件；确需全表更新请走 DBA 评估流程
```

字段清单（顺序随意，大小写不敏感）：

| 字段 | 必填 | 取值 | 说明 |
| --- | --- | --- | --- |
| 级别 | ✅ | ❌ / ⚠️ / ✅ / ⏸ | ❌=驳回并必须整改；⚠️=提示不阻塞；✅=纯建议；⏸=未判定 |
| 适用 | ✅ | DDL：`create_table` `alter_table` `create_index` `drop_index` `drop_table` `truncate` `drop_database`<br>DML：`insert` `update` `delete` `replace` `batch` | 多条用逗号；写错会**拒绝加载并报可用值** |
| 检查 | DDL 必填 | 见第四节 | 判定原语名 |
| 判定 | DML 必填 | 表达式，如 `has_where = false and multi_table = true` | 语法见 `dml.md` 附录 A |
| 依赖 | DML 可选 | 事实名（逗号分隔） | 缺事实 → 该条标 ⏸，不会当成通过 |
| 参数 | 可选 | `key = value; key = v1, v2` | 可重复写多行累加；`key.sub = value` 表示子键（字典） |
| 条款 | 建议 | 文字 | 会原样回给研发（"依据哪条"） |
| 说明 | 建议 | 文字 + `{占位符}` | 命中时给研发看的第一句 |
| 整改 | 建议 | 文字 + `{占位符}` | 怎么改；DDL 自动改写后的结果也在这 |
| 情形 | 可选 | `情形名 = 级别 \| 说明 \| 整改` | 同一规则多种命中形态（见 TB-01、AL-03、IX-05） |
| 自动改写 | 可选 | `true` / `false`（默认 false） | 声明"命中时服务是否直接改写语句" |
| 反问 | 可选 | `Q-DECIMAL (blocking); Q-COMMENT` | 反问目录见 `config/questions.yaml` |
| 停用 | 可选 | `true` / `false`（默认 false） | `true` = 本条规则不生效（**下线规则首选方式**） |
| 样例 | **强烈建议** | `命中 = <SQL>` / `通过 = <SQL>`（一行一条，可多条） | `check_specs.py` 会自动跑这些正反用例——**新增/修改规则必须带样例** |

**自动改写**必须由代码支持（`ddlchecks.py` 里实现了改写动作）；文档写 `true` 只是"允许服务改写"。
DML 恒为 `false`：本服务对 DML 只报错不改写，`sql_final` 恒等于 `sql_input`。

---

## 二、常见改法（都不用碰代码）

### 1) 调级别：强制 ↔ 推荐 ↔ 停用

```markdown
- **级别**：⚠️        # 原来是 ❌，改成提示，不影响研发提交
```

完全下线某条规则（保留条款便于追溯）：

```markdown
- **停用**：true
```

### 2) 改阈值数字

阈值**优先改「## 阈值」表**（DDL 的 `max_columns`、`big_table_rows`…），
其次改规则自己的 `**参数**`（如 TB-12 的 `max_columns = 30`）。规则参数 > 阈值表 > `settings.yaml` 兜底。

### 3) 改给研发看的话术

直接改 `**说明**` / `**整改**`（`{字段}` / `{阈值名}` 占位符会替换成实际值）。
反问话术改 `config/questions.yaml`（**改完即生效**，服务每次请求都会重读）。

### 4) 加一条"同类"规则（零代码）

已有判定原语可以复用，只要在文档里新写一个小节：

```markdown
### TB-17 禁用 GEOMETRY 类型

- **级别**：❌
- **适用**：create_table
- **检查**：forbidden_types
- **参数**：types = GEOMETRY, POINT, LINESTRING, POLYGON; fix_mode = none
- **条款**：空间类型字段需与 DBA 单独评估
- **说明**：禁止使用 {type} 类型
- **整改**：请与 DBA 确认空间字段方案
```

> 规则编号不能重复。`get_rules` 会立刻返回 TB-17，`selftest` 无需改。

### 5) 改必备字段 / 必备索引清单

`ddl.md` 的 TB-08 / TB-09 的 `**参数**`：

```markdown
- **参数**：columns = ID BIGINT | IS_DEL TINYINT | CREATE_TIME DATETIME | MODIFY_TIME DATETIME | __MODIFY_TIME__ DATETIME
- **参数**：indexes = PRIMARY ID | IDX_CREATE_TIME CREATE_TIME | IDX__MODIFY_TIME__ __MODIFY_TIME__
```

（`|` 分隔每一项，`名称 类型` 用空格分隔。）

### 6) 调整"哪些参数能自动改写"

改 `**自动改写**：true/false`。代码里已实现的改写动作才生效（TB-01 命名规范、TB-06 时间类型、
TB-08/TB-09 必备字段索引、TB-10 引擎字符集、TB-14 整型长度/CHAR、AL-01 去 AFTER/FIRST、
IX-01 索引命名、IX-02 前缀、IX-03 大文本移出索引、AL-05 新增字段规范化）。

### 7) 给规则写样例（**新增/改规则必须做**）

```markdown
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', AMOUNT FLOAT NOT NULL COMMENT 'a');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (...合规建表...);
```

* `命中` = 这条 SQL 跑出来**必须**有本规则的结论；`通过` = 跑出来**不得**有本规则的结论；
* 一行一条、可写多条；`check_specs.py` 每条都会真跑一遍（`review_sql`，不连库、不传 env）；
* 依赖表事实才能命中的规则（TB-16 / IX-02 / IX-03 / IX-06 / AL-03 / SC-01 / SC-02、
  DM-01 / DM-03 / DM-04 / DM-06 / DM-09 / DM-17 / DM-20 / DM-21 / DM-22）离线无法验证，可以暂不写样例，
  但**改这些规则的判定口径属"必须改代码"**，需人工 review。

> 为什么值得写：这样"只改 md 就上线"才不是裸奔——新加的规则下一次 `check_specs.py` 就被验证过了。

---

## 三、规则执行顺序 = 文档顺序（有依赖，别随意调）

`run_scope()` 严格按 `ddl.md` 里 `###` 小节出现的**先后顺序**执行，前一条的自动改写会影响后一条看到的事实。已知的真实依赖：

| 顺序依赖 | 原因 |
|---|---|
| `TB-08`（必备字段）要在 `TB-06`（时间类型）之前 | TB-08 会把必备字段的样例类型改成规范值；若 `CREATE_TIME` 写成 `TIMESTAMP`，TB-08 先改成了 `DATETIME`，TB-06 就不会再命中它（这是正确行为，但说明顺序有语义） |
| `TB-01`（命名）要在 `TB-16`（同名表查重）之前 | 查重要用**规范化后**的表名，否则大小写/非法字符会让查重失真 |
| `AL-01`（去 AFTER/FIRST）要在 `AL-02`（同表合并）之前 | 合并时子句顺序与 AFTER 已处理完毕 |
| `IX-01`（索引命名）要在 `IX-05`（索引数量/重名）之前 | 重名判定用规范化后的索引名 |
| `DX-0x` 放在 DDL 段最后 | 破坏性操作一旦命中即 `fatal`，前面结论仍要保留（便于一次性告知） |

**结论**：调级别/改文案/改参数都不用管顺序；**挪动小节位置属于高风险改动**，挪前先想清楚"后面那条会不会因此看到不同的事实"，并跑 `scripts/check_specs.py` 的样例回归。

---

## 四、什么情况必须改代码（只有两种情况）

| 场景 | 要改哪里 |
| --- | --- |
| **新的判定能力**：要检测一件现在读不出来的事（如"是否用了 FORCE INDEX"、"是否分区表"） | 新增一个检查函数 → `src/sql_review_mcp/ddlchecks.py`（DDL）或 `src/sql_review_mcp/dmfacts.py`（DML 事实） |
| **新的改写动作**：命中后要把 SQL 改成现在不会改的样子 | `src/sql_review_mcp/ddlchecks.py` 对应检查里实现 |

其余全部场景（级别、文案、阈值、清单、停用、加同类规则、改适用范围、改反问）**只改文档**。

---

## 五、DDL 可用检查（`- **检查**：` 后面能写的名字）

| 检查 | 用途 | 可填占位符 | 可填情形 |
| --- | --- | --- | --- |
| `forbid_ctas` | 禁止 CREATE TABLE ... SELECT | — | — |
| `required_columns` | 必备字段 | name, type, changed | missing, mismatch |
| `required_indexes` | 必备索引 | name, label, columns | missing, mismatch |
| `naming` | 表名/字段名命名 | name, kind, fixed, max_len, items | uppercase, reserved, length, charset, illegal, duplicate, no_business |
| `comments` | 表/字段注释 | name, comment, columns | table_missing, column_missing |
| `forbidden_types` | 禁用类型（参数化） | name, type, old, replace_with, note | — |
| `datetime_types` | 时间类型与默认值 | name, old, new, allowed, default, replace_default | type, default_value |
| `not_null` | 字段非空 | name, changed | — |
| `engine_charset` | 引擎与字符集 | engine, charset, changed | — |
| `table_type_naming` | 历史/归档/临时/备份表命名 | name, date, months, rules | bad_date, no_months, suspect |
| `max_columns` | 单表字段数上限 | count, max | — |
| `type_width` | 整型长度、CHAR 超长、VARCHAR 偏大 | name, old, new, length, max, hint | int_length, char_wide, varchar_hint |
| `db_name` | 库名前缀/大小写/长度 | db, prefix, max_len | — |
| `table_exists` | 目标库同名表 | table, rows | — |
| `foreign_key` | 禁外键 | name | — |
| `index_column_exists` | 索引字段必须存在 / 表索引是否存在 | name, columns, table | missing_col, table_missing, index_missing |
| `index_big_text` | 大文本禁入索引 | name, columns | — |
| `index_naming` | 索引命名 | name, want | — |
| `varchar_prefix` | 长 VARCHAR 前缀索引 | name, column, length, max | prefixed, unique_bad |
| `index_limits` | 索引数量/字段数/重名 | name, count, max | count, cols, unique_cols, duplicate |
| `after_first` | 禁 AFTER / FIRST | column, clause | — |
| `add_column` | ADD COLUMN 规范 | column, changed, type | hit, exists, big_object |
| `forbid_rename` | 禁改名/改字符集/删字段 | column, old, new, table, rows | rename_col, rename_index, rename_table, drop_col, drop_col_exists, charset, charset_convert |
| `modify_type` | MODIFY 原类型比对 | column, new, old, table | consistent, mismatch, unknown, no_column, table_missing |
| `varchar_instant` | VARCHAR 跨 255 字节 | column, length, old, new, bytes | add_cross, modify_cross, unknown |
| `algorithm_hint` | ALGORITHM / LOCK 提示 | table, hint | — |
| `always` | 无条件命中（破坏性操作） | name, table, db | — |
| `big_table` | 大表标准 | rows, size_gb, std_rows, std_gb | big, unknown |
| `modify_window` | MODIFY 执行窗口 | rows, max_rows, window | — |

> 用了未登记的检查名、写了不存在的占位符、情形名不属于该检查 → **规范预检直接报错并给出可用值**
> （`python scripts/check_specs.py`，服务启动时也会做同样校验）。

---

## 六、改完怎么验证（三步，30 秒）

```bash
cd /opt/mcpservers/sql-review-dml-mcp

# ① 预检：格式 + 检查名 + 占位符 + 阈值 + 允许项冲突 + **样例正反回归**（每条真跑一遍）
PYTHONPATH=src .venv/bin/python scripts/check_specs.py      # 退出码 0 = 通过
#    只想看结构不跑样例：加 --no-samples

# ② 引擎自检：规则加载 + 逐条命中 + 自动改写 + 四态结论 + 文档驱动热生效
PYTHONPATH=src .venv/bin/python scripts/selftest.py

# ③ 看服务是否用上了新规则（不用重启）——healthz 读的是**实时**规则集，不是启动时的缓存
curl -s http://127.0.0.1:8030/healthz | python3 -m json.tool
#    scope / rules_count / rules_version / standard_version（本服务只加载 dml.md）
```

**只改 md（和 `questions.yaml`、`STANDARD_VERSION`）不用重启**——服务按文件 mtime 热加载：

* 改对了 → 下一次调用即用新规则；`/healthz`、审计里的 `standard.*`、返回体的 `standard_version` 同步变新；
* 改坏了（格式错、占位符不存在、情形名不存在、允许项冲突）→ **不切换**，继续用上一版，
  返回体里带 `warnings` 与实际原因，线上不会因为一次手误变得不可用。

> 注意：本服务只加载 `dml.md`，它写坏且服务刚启动过 → 启动阶段会直接失败（fail-fast，退出码 2），
> 这是故意的：不允许"带着旧逻辑悄悄上线"。

---

## 七、变更流程（建议照此执行）

1. 改 `ddl.md` / `dml.md`（必要时改 `config/questions.yaml` 的话术）；
2. 跑 `scripts/check_specs.py`（必须 0）+ `scripts/selftest.py`（必须全绿）；
3. 升 `config/STANDARD_VERSION`（规范版本号），并在 `CHANGELOG.md` 追加一行（改了哪几条、抽到几条规则）；
4. 不需要重启、不需要发版；如需回滚，改回 md 即可（或从 `backup/` 取上一版）。
