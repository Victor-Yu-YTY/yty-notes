# MySQL DML 审核标准

**规则版本**：2026-11-03

本文件是本服务 **DML 审核的判定依据（INSERT / UPDATE）**。规则改这里就生效，不用改代码、不用重启。

> **怎么读这份文件**
> - 每条规则一个小节：`### 编号 标题`，下面是固定字段（与 `ddl.md` 完全同构）；
> - **级别**：❌ = 驳回/必须整改，⚠️ = 提示（不阻塞提交），✅ = 建议；
> - **判定** = 一行布尔表达式，由「事实名 + 操作符 + 值」组成，语法见文末附录 A；
> - **依赖** = 该规则需要的事实（缺了标 ⏸ 未判定，不猜、不跳过）；
> - **说明** / **整改** 是回给研发的原话，支持 `{事实名}` / `{阈值名}` 占位符；
> - **自动改写**：DML 恒为 `false` —— 本服务只报错不改写，`sql_final` 恒等于 `sql_input`；
> - **停用**：`true` 表示本条规则不生效（改这里即下线规则，不用改代码）。

- 通用总纲：`standards/mysql.md`（含 DML 相关条款的服务侧完整表述）
- DDL 细则：`standards/ddl.md`

## 适用范围

| 语句 | 处理方式 |
| --- | --- |
| INSERT | 按本文件全部 insert 规则审 |
| UPDATE | 按本文件全部 update 规则审 |
| DELETE / REPLACE INTO | 只命中 DM-12 一律驳回，不进入其它规则 |
| SELECT 等查询 | 不在本服务范围，走 dba-mcp |

**只报错，不代劳**：本服务不产出整改后的 SQL，`sql_final` 恒等于 `sql_input`。

## 阈值

| 名称 | 值 | 说明 |
| --- | --- | --- |
| max_insert_rows | 10000 | 单条 INSERT 一次写入行数上限 |
| max_affected_rows | 10000 | 单条 UPDATE 影响行数上限 |
| batch_split_rows | 10000000 | 目标表达该行数时 UPDATE 必须按 ID 分批 |

## 允许项

允许项声明「这样做是合规的」，防止后续加规则误伤。加载时会做一致性检查：若出现判定条件恰好等于允许项 `判定` 的 error 规则，文件直接被拒绝加载。

### DM-P01 允许单表 INSERT / UPDATE 使用 LIMIT

- **适用**：insert, update
- **判定**：has_limit = true
- **条款**：单表 UPDATE 允许 LIMIT；INSERT 的 LIMIT 语义由 DM-09 按影响行数约束
- **说明**：MySQL 8.0 官方：单表 UPDATE 语法支持 ORDER BY / LIMIT；多表 UPDATE 两者都不支持（见 DM-02）

### DM-P02 允许 INSERT 插入 NULL 值

- **适用**：insert, update
- **判定**：insert_has_null = true
- **条款**：目标列允许 NULL 时可显式插入 NULL
- **说明**：不因出现 NULL 字面量本身报错；只有向 NOT NULL 列插 NULL 才由 DM-21 拦

## 规则

### DM-01 INSERT 单次写入行数上限

- **级别**：❌
- **适用**：insert
- **判定**：insert_rows > $max_insert_rows
- **条款**：单条 INSERT 一次写入不超过 {max_insert_rows} 行
- **说明**：单条 INSERT 写入 {insert_rows} 行，超过上限 {max_insert_rows} 行
- **整改**：拆成多条 INSERT，每条不超过 {max_insert_rows} 行分批提交

### DM-02 多表 UPDATE 禁止 ORDER BY / LIMIT

- **级别**：❌
- **适用**：update
- **判定**：multi_table = true and (has_order_by = true or has_limit = true)
- **条款**：MySQL 多表 UPDATE 语法不支持 ORDER BY 与 LIMIT
- **说明**：多表 UPDATE 带 ORDER BY / LIMIT，MySQL 直接报语法错误
- **整改**：改为单表 UPDATE 后再决定是否加 LIMIT
- **样例**：命中 = UPDATE DEMO_T a JOIN DEMO_Y b ON a.ID = b.ID SET a.X = 1 LIMIT 1;

### DM-03 禁止隐式类型转换

- **级别**：❌
- **适用**：insert, update
- **依赖**：column_types
- **判定**：implicit_conversion_hits is not empty
- **条款**：比较与赋值两侧类型必须一致，字符串列不得与数字常量比较
- **说明**：检测到隐式类型转换：{implicit_conversion_hits}
- **整改**：让两侧类型一致：字符串列用带引号的值，数字列用数字字面量

### DM-04 INSERT 列名必须存在于目标表

- **级别**：❌
- **适用**：insert
- **依赖**：column_names
- **判定**：missing_insert_columns is not empty
- **条款**：INSERT 列名必须与目标表实际结构一致
- **说明**：以下列在目标表中不存在：{missing_insert_columns}（语句会执行失败）
- **整改**：按 SHOW CREATE TABLE 的实际列名修正

### DM-05 UPDATE 必须带 WHERE

- **级别**：❌
- **适用**：update
- **判定**：has_where = false
- **条款**：UPDATE 必须有 WHERE 条件，禁止全表更新
- **说明**：UPDATE 缺少 WHERE，会更新全表
- **整改**：补 WHERE 条件；确需全表更新请走 DBA 评估流程
- **样例**：命中 = UPDATE DEMO_T SET A = 1;

### DM-06 WHERE 条件必须命中索引

- **级别**：❌
- **适用**：update
- **依赖**：index_first_columns
- **判定**：has_where = true and where_complex = false and where_index_hit = false
- **条款**：WHERE 中至少一个条件列必须是某索引的首列，否则全表扫描
- **说明**：WHERE 字段 {where_columns} 均未命中任何索引首列，会走全表扫描
- **整改**：改用有索引的字段做条件（如主键或唯一键），或先评估补索引

### DM-07 禁止 DML 使用 ORDER BY

- **级别**：❌
- **适用**：update
- **判定**：has_order_by = true
- **条款**：UPDATE 不得使用 ORDER BY 子句
- **说明**：UPDATE 带 ORDER BY，排序无业务意义且执行计划不可控
- **整改**：去掉 ORDER BY；如需控制顺序请按业务主键条件精确圈选
- **样例**：命中 = UPDATE DEMO_T SET A = 1 WHERE ID = 1 ORDER BY ID;

### DM-08 禁止在 DML 中使用子查询

- **级别**：❌
- **适用**：update
- **判定**：has_subquery = true
- **条款**：UPDATE 的 SET / WHERE 不得包含 SELECT 子句
- **说明**：UPDATE 中包含子查询
- **整改**：拆成两步：先查出中间结果，再用明确的键值做 UPDATE
- **样例**：命中 = UPDATE DEMO_T SET A = (SELECT MAX(X) FROM DEMO_Y) WHERE ID = 1;

### DM-09 DML 影响行数上限

- **级别**：❌
- **适用**：update
- **判定**：affected_rows_upper > $max_affected_rows
- **条款**：单条 UPDATE 影响行数不超过 {max_affected_rows} 行
- **说明**：预计影响行数上界 {affected_rows_upper}，超过上限 {max_affected_rows}
- **整改**：补 ID 范围条件分批执行，每批不超过 {max_affected_rows} 行

### DM-10 DML 必须以事务包裹

- **级别**：❌
- **适用**：batch
- **判定**：batch_has_dml = true and batch_in_transaction = false
- **条款**：提交的 DML 必须整体包在 START TRANSACTION … COMMIT 中
- **说明**：批次含 DML 但未显式开启事务，失败只能单条回滚，无法整体撤销
- **整改**：首条语句前加 START TRANSACTION，末条后加 COMMIT；失败可整体回滚
- **反问**：Q-TRANSACTION
- **样例**：命中 = UPDATE DEMO_T SET A = 1 WHERE ID = 1;
- **样例**：通过 = START TRANSACTION; UPDATE DEMO_T SET A = 1 WHERE ID = 1; COMMIT;

### DM-11 INSERT 必须显式列出列名

- **级别**：❌
- **适用**：insert
- **判定**：insert_columns is empty
- **条款**：禁止 INSERT INTO t VALUES (...)，必须写明列名
- **说明**：INSERT 未指定列名，依赖表的物理列顺序
- **整改**：改为 INSERT INTO t (COL1, COL2) VALUES (...)，后续加字段也不会写错
- **样例**：命中 = INSERT INTO DEMO_T VALUES (1, 'x');
- **样例**：通过 = INSERT INTO DEMO_T (ID) VALUES (1);

### DM-12 DELETE / REPLACE INTO 一律驳回

- **级别**：❌
- **适用**：delete, replace
- **判定**：stmt_kind in (delete, replace)
- **参数**：fatal = true
- **条款**：本通道不审数据删除类语句，一律驳回改走工单
- **说明**：{stmt_kind} 属不可逆写入，本通道不提供审核结论
- **整改**：改走工单系统，由 DBA 审核并执行；删除请一并提供归档方案
- **样例**：命中 = DELETE FROM DEMO_T WHERE ID = 1;

### DM-13 禁止多表 JOIN 的 UPDATE

- **级别**：❌
- **适用**：update
- **判定**：multi_table = true
- **条款**：只允许单表 UPDATE；多表 UPDATE 不可控
- **说明**：多表 JOIN 更新
- **整改**：拆成按主键的多条单表 UPDATE
- **样例**：命中 = UPDATE DEMO_T a JOIN DEMO_Y b ON a.ID = b.ID SET a.X = 1;

### DM-14 禁止 IGNORE / LOW_PRIORITY 修饰符

- **级别**：❌
- **适用**：update
- **判定**：ignore_modifier = true
- **条款**：DML 不得使用 IGNORE、LOW_PRIORITY 等修饰符
- **说明**：使用 IGNORE / LOW_PRIORITY：错误被静默吞掉，且 IGNORE 在 statement 复制下被标记为 unsafe
- **整改**：去掉修饰符，让错误正常抛出
- **样例**：命中 = UPDATE IGNORE DEMO_T SET A = 1 WHERE ID = 1;

### DM-15 子查询不得引用被更新的同表

- **级别**：❌
- **适用**：update
- **判定**：subquery_target_self = true
- **条款**：子查询不能引用 UPDATE 的目标表
- **说明**：子查询引用了正在更新的同一张表，MySQL 会报 ERROR 1093
- **整改**：改用 JOIN 派生表，或先把中间结果落到临时表再更新
- **样例**：命中 = UPDATE DEMO_T SET A = 1 WHERE ID IN (SELECT ID FROM DEMO_T WHERE B = 2);

### DM-16 DDL 与 DML 不得混在同一批次

- **级别**：❌
- **适用**：batch
- **判定**：batch_has_dml = true and batch_has_ddl = true
- **条款**：DDL 会隐式提交事务，混合提交会让 DM-10 的事务失效
- **说明**：同一批次同时包含 DDL 与 DML，DDL 的隐式提交会打断事务
- **整改**：拆成两张工单：先执行 DDL，确认生效后再执行 DML
- **样例**：命中 = CREATE TABLE DEMO_X (ID BIGINT NOT NULL COMMENT 'x'); UPDATE DEMO_X SET ID = 2 WHERE ID = 1;

### DM-17 索引列禁止函数运算与前置模糊匹配

- **级别**：⚠️
- **适用**：update
- **依赖**：index_first_columns
- **判定**：indexed_col_func_calls is not empty
- **条款**：WHERE 中不得对索引列做函数运算，不得使用前置 %LIKE，否则索引失效
- **说明**：以下索引列写法会让索引失效：{indexed_col_func_calls}
- **整改**：把函数移到常量侧（如 CREATE_TIME >= '2026-01-01'），模糊匹配改后置 '%x'

### DM-18 禁止非确定函数写入

- **级别**：⚠️
- **适用**：insert, update
- **判定**：nondeterministic_calls is not empty
- **条款**：SET / VALUES 中不得使用 NOW / CURDATE / RAND / UUID 等非确定函数
- **说明**：使用了非确定函数：{nondeterministic_calls}
- **整改**：由应用层生成具体值后再写入，便于复现与对账
- **样例**：命中 = UPDATE DEMO_T SET A = NOW() WHERE ID = 1;

### DM-19 写入行数不可静态确定，需人工确认

- **级别**：⚠️
- **适用**：insert
- **判定**：insert_select = true or on_duplicate_key = true
- **条款**：INSERT ... SELECT / ON DUPLICATE KEY UPDATE 需确认影响面
- **说明**：写入行数无法静态确定，或会触发唯一键冲突覆盖
- **整改**：在工单中给出预计行数与冲突处理策略
- **样例**：命中 = INSERT INTO DEMO_T (ID) SELECT ID FROM DEMO_Y;

### DM-20 大表批量更新须按 ID 分批

- **级别**：⚠️
- **适用**：update
- **依赖**：table_rows
- **判定**：table_rows >= $batch_split_rows and where_has_id = false
- **条款**：目标表达到 {batch_split_rows} 行时，UPDATE 必须带 ID 范围条件
- **说明**：目标表约 {table_rows} 行，WHERE 未见 ID 或 xxx_ID 条件
- **整改**：按 ID 区间分批，每批单独提交，便于失败重试与回滚定位

### DM-21 禁止向 NOT NULL 列插入 NULL

- **级别**：❌
- **适用**：insert, update
- **依赖**：column_nullable
- **判定**：null_into_notnull is not empty
- **条款**：目标列 NOT NULL 时不得插入 NULL（严格模式下直接报错）
- **说明**：以下 NOT NULL 列被赋 NULL：{null_into_notnull}
- **整改**：改为该列的业务默认值，或确认该列是否应允许 NULL

### DM-22 目标表必须存在

- **级别**：❌
- **适用**：insert, update
- **依赖**：table_exists
- **判定**：table_exists = false
- **条款**：INSERT / UPDATE 的目标表必须已在目标库中存在
- **说明**：目标表 {target_table} 在目标库中不存在，语句会直接报错
- **整改**：确认库名与表名；新建表请先走 CREATE TABLE 工单

---

## 附录 A：判定表达式语法

每条规则的 `**判定**` 行写一个布尔表达式，由「事实名 + 操作符 + 值」组成。

| 写法 | 含义 |
| --- | --- |
| `事实 = 值` `事实 == 值` | 相等 |
| `事实 != 值` `事实 <> 值` | 不等 |
| `事实 > 值` `>=` `<` `<=` | 大小比较（数值） |
| `事实 in (a, b)` | 属于集合 |
| `事实 not in (a, b)` | 不属于集合 |
| `事实 is null` / `is not null` | 事实取不到 / 取得到 |
| `事实 is empty` / `is not empty` | 列表为空 / 非空 |
| `事实 is true` / `is false` | 布尔 |
| `A and B` / `A or B` / `not A` | 组合，支持括号 |
| `$阈值名` | 引用「阈值」表里的数字 |

值可以写数字、`true` / `false`、不带引号的标识符（如 `delete`）、`$阈值名`。

**三值逻辑**：事实取不到时表达式结果为「未知」，该条规则标 ⏸ 未判定，不会当成通过。
某条规则需要表结构才能判时，在 `**依赖**` 里列出事实名，缺了就直接标 ⏸，并提示补齐 env + database。

## 附录 B：可用事实清单（代码固定，只能引用这里有的名字）

| 事实 | 类型 | 含义 |
| --- | --- | --- |
| stmt_kind | 文本 | insert / update / delete / replace |
| target_db / target_table | 文本 | 目标库、表 |
| multi_table | 布尔 | 表引用数 > 1（JOIN 或多表更新） |
| has_where / where_complex | 布尔 | 是否有 WHERE；WHERE 是否复杂到抽不准谓词 |
| where_columns | 列表 | WHERE 顶层谓词涉及的列 |
| has_order_by / has_limit / limit_value | 布尔 / 数值 | ORDER BY、LIMIT |
| insert_rows | 数值 | VALUES 元组数；INSERT ... SELECT 取不到 |
| insert_columns | 列表 | 显式列名 |
| insert_has_null / insert_select / on_duplicate_key | 布尔 | INSERT 形态 |
| ignore_modifier | 布尔 | IGNORE / LOW_PRIORITY |
| set_targets | 列表 | SET 左侧的列 |
| has_subquery / subquery_target_self | 布尔 | 子查询、子查询是否引用同表 |
| nondeterministic_calls | 列表 | NOW / CURDATE / RAND / UUID 等 |
| table_exists / table_rows | 布尔 / 数值 | 目标表是否存在、估算行数（需 env + database） |
| column_names / column_types / column_nullable | 列表 / 映射 | 目标表列信息（需 env + database） |
| index_first_columns | 列表 | 各索引首列（需 env + database） |
| missing_insert_columns | 列表 | 派生：INSERT 列名中表里没有的 |
| null_into_notnull | 列表 | 派生：被赋 NULL 但列 NOT NULL |
| implicit_conversion_hits | 列表 | 派生：可能触发隐式转换的写法 |
| where_index_hit / where_has_id | 布尔 | 派生：WHERE 是否命中索引、是否带 ID 条件 |
| indexed_col_func_calls | 列表 | 派生：索引列上的函数 / 前置 %LIKE |
| affected_rows_upper | 数值 | 派生：影响行数上界 |
| batch_has_dml / batch_has_ddl / batch_in_transaction | 布尔 | 批次级 |

新增事实（要检测一件现在读不出来的事）需要改 `src/sql_review_mcp/dmfacts.py`，这是本文件唯一管不到的地方。
