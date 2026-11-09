# SQL-Executor 执行前置判定标准

**标准版本**：2026-11-04
**规则版本**：1
**引擎**：exec-v1
**模式**：live

> 本文件是本服务「此刻在这个环境能不能执行」的判定依据（**唯一事实源**）。
> **改本文件 = 改标准**。改完下一次请求即生效（按文件 mtime 热加载，不用重启服务），
> 但**必须**先跑通 `python scripts/selfcheck.py`，用例与该文件一起提交。
>
> 与 sql-review-mcp 的分工：本服务**只判落地可行性**，不判规范符合性
> （命名 / 类型 / 注释 / 索引规范 / SQL 写法都归审核服务）。本服务也不调用审核服务。
>
> 库级隔离**不在这里判**：由 MySQL 执行账号的 GRANT 范围承担，
> 账号必须按库授权（`ON \`具体库\`.*`），禁止 `ON *.*`，详见 `config/targets.yaml`。
>
> 字段含义、判定表达式语法、加规则/改规则/停用规则的完整操作步骤见 `standards/README.md`。

---

## 阈值

改这里等于改所有引用该阈值的规则，不用逐条改。

| 名称 | 值 | 说明 |
| --- | --- | --- |
| max_statements | 20 | 单次请求的语句数上限（与 SQL 字节数一起兜住批量提交） |
| max_sql_bytes | 20000 | 单次请求 SQL 的字节上限 |
| big_table_rows | 5000000 | 大表行数阈值，与审核 SC-01 对齐 |
| big_table_size_gb | 3 | 大表体积阈值（GB），与审核 SC-01 对齐 |
| modify_window_rows | 8000000 | 超大表 MODIFY COLUMN 的执行窗口阈值，与审核 SC-02 对齐 |
| max_insert_rows | 10000 | 单条 INSERT 写入行数上限，与审核 DM-01 对齐 |
| max_affected_rows | 10000 | UPDATE 影响行数上限，与审核 DM-09 对齐 |
| ddl_disk_factor | 1.5 | COPY 算法所需临时空间 = 表大小 × 该系数 |
| ddl_disk_floor_gb | 5 | COPY 算法所需临时空间的保底值（GB） |
| replica_lag_seconds | 10 | 从库延迟告警阈值（秒） |
| long_trx_seconds | 60 | 长事务判定阈值（秒） |
| lock_wait_timeout | 10 | 执行会话的 MDL / 行锁等待上限（秒） |
| exec_timeout_seconds | 30 | 单次执行超时（秒）；dry_run=rollback 时同时设 max_execution_time |
| count_timeout_seconds | 3 | 判定阶段 COUNT 估算影响行数的超时（秒），超时即视为取不到 |
| recent_dup_window_minutes | 30 | 疑似重复执行的回看窗口（分钟） |
| max_table_name_len | 64 | 表名长度上限（MySQL 限制） |

## 环境

**库级隔离不在这里配**：`allowed_types` 只决定「本环境开放哪几类语句」，
能碰哪个库由执行账号的 GRANT 决定（见 `config/targets.yaml`）。
`开放执行 = false` 的环境一条语句都不放行（判定阶段即 BLOCKED，连库都不连）。

| 环境 | 开放执行 | 允许类型 | 执行窗口 |
| --- | --- | --- | --- |
| dev | true | create_table, alter_table, create_index, insert, update | - |
| test | true | create_table, alter_table, create_index, insert, update | - |
| stage | true | create_table, alter_table, create_index, insert, update | 21:00-23:59 |
| prod | false | - | - |

> `执行窗口` 只对「超大表 MODIFY COLUMN」生效（见 EX-A06），不是全环境宵禁。

---

## 通用规则

### EX-G01 语句类型不在开放范围

- **级别**：block
- **适用**：all
- **判定**：stmt_kind not in $allowed_types
- **说明**：{stmt_kind} 未开放执行，本版只执行 CREATE TABLE / ALTER TABLE / CREATE INDEX / INSERT / UPDATE
- **原因**：本服务只承接可回滚或可精确界定的写入；DELETE / REPLACE / TRUNCATE / DROP 等一律不在本服务职责内
- **样例**：命中 = DELETE FROM DEMO_T WHERE ID = 1;

### EX-G02 环境未启用

- **级别**：block
- **适用**：all
- **判定**：env_enabled = false
- **说明**：环境 {env} 未开放执行
- **样例**：命中 = 在 environments 里把 test 改成「开放执行 = false」后提交任意语句

### EX-G04 语句数超上限

- **级别**：block
- **适用**：all
- **判定**：statement_count > $max_statements
- **说明**：本批 {statement_count} 条，超过单次上限 {max_statements}
- **样例**：命中 = 一次提交 21 条 INSERT

### EX-G05 SQL 过大

- **级别**：block
- **适用**：all
- **判定**：sql_bytes > $max_sql_bytes
- **说明**：SQL {sql_bytes} 字节，超过上限 {max_sql_bytes}

### EX-G06 DDL 与 DML 混批

- **级别**：block
- **适用**：all
- **判定**：batch_has_ddl = true and batch_has_dml = true
- **说明**：同批含 DDL 与 DML，DDL 隐式提交会让事务失效，请拆成两批
- **样例**：命中 = CREATE TABLE t2 (...); INSERT INTO t2 (ID) VALUES (1);

### EX-G07 执行账号权限不足

- **级别**：block
- **适用**：all
- **判定**：missing_privileges is not empty
- **说明**：账号缺少权限：{missing_privileges}
- **原因**：权限取不到时按 fail-closed 处理，宁可拦住也不带病执行
- **样例**：命中 = 执行账号未授予 ALTER 却提交 ALTER TABLE

### EX-G08 复制延迟偏高

- **级别**：warn
- **适用**：all
- **判定**：replica_lag_seconds > $replica_lag_seconds
- **说明**：从库延迟 {replica_lag_seconds}s，DDL 重放会进一步拉大

### EX-G09 存在长事务

- **级别**：warn
- **适用**：all
- **判定**：long_trx_count > 0
- **说明**：有 {long_trx_count} 个超过 {long_trx_seconds}s 的事务，DDL 可能被 MDL 阻塞

### EX-G10 疑似重复执行

- **级别**：warn
- **适用**：all
- **判定**：recent_same_sql_count > 0
- **说明**：近 {recent_dup_window_minutes} 分钟内已执行过 {recent_same_sql_count} 次相同 SQL

---

## 建表规则

### EX-T01 目标表已存在

- **级别**：block
- **适用**：create_table
- **判定**：table_exists = true
- **说明**：表 {target_table} 已存在，CREATE TABLE 会失败
- **样例**：命中 = 对已存在的 DEMO_T 再执行一次 CREATE TABLE DEMO_T

### EX-T02 目标库不存在

- **级别**：block
- **适用**：create_table
- **判定**：database_exists = false
- **说明**：库 {database} 不存在

### EX-T03 表名超长

- **级别**：block
- **适用**：create_table
- **判定**：table_name_length > $max_table_name_len
- **说明**：表名 {table_name_length} 字符，超过 MySQL 上限 {max_table_name_len}

### EX-T04 建表未显式指定 InnoDB

- **级别**：warn
- **适用**：create_table
- **判定**：create_table_engine not in (InnoDB)
- **说明**：未显式指定 ENGINE=InnoDB，将取实例默认引擎 {default_engine}
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x');

---

## 变更表规则

### EX-A01 目标表不存在

- **级别**：block
- **适用**：alter_table
- **判定**：table_exists = false
- **说明**：表 {target_table} 不存在，ALTER 会失败

### EX-A02 ADD COLUMN 列已存在

- **级别**：block
- **适用**：alter_table
- **判定**：add_columns_existing is not empty
- **说明**：以下列已存在：{add_columns_existing}
- **样例**：命中 = ALTER TABLE DEMO_T ADD COLUMN ID BIGINT NOT NULL COMMENT 'x';

### EX-A03 被变更的列不存在

- **级别**：block
- **适用**：alter_table
- **判定**：modify_columns_missing is not empty
- **说明**：以下列不存在：{modify_columns_missing}

### EX-A04 索引名已被占用

- **级别**：block
- **适用**：alter_table
- **判定**：index_names_existing is not empty
- **说明**：索引名已存在：{index_names_existing}

### EX-A05 大表 DDL

- **级别**：warn
- **适用**：alter_table
- **判定**：table_rows >= $big_table_rows or table_size_gb > $big_table_size_gb
- **说明**：表约 {table_rows} 行 / {table_size_gb}G，属大表，需提前与 DBA 沟通窗口

### EX-A06 超大表 MODIFY 不在执行窗口

- **级别**：block
- **适用**：alter_table
- **判定**：alter_ops intersects (MODIFY_COLUMN) and table_rows >= $modify_window_rows and in_window = false
- **说明**：表约 {table_rows} 行，MODIFY COLUMN 只允许在 {window} 执行
- **样例**：命中 = ALTER TABLE DEMO_T MODIFY COLUMN C VARCHAR(200) NOT NULL DEFAULT '' COMMENT 'c';

### EX-A07 磁盘空间不足

- **级别**：block
- **适用**：alter_table, create_index
- **判定**：disk_headroom_gb < $required_disk_gb
- **缺失**：skip
- **说明**：预计需要 {required_disk_gb}G 临时空间，当前可用 {disk_headroom_gb}G
- **原因**：MySQL 没有内置剩余磁盘查询，取不到时不拦（否则所有 ALTER 都会被误杀），由 DBA 侧监控兜底

### EX-A08 DDL 走 COPY 算法

- **级别**：warn
- **适用**：alter_table
- **判定**：ddl_algorithm = COPY
- **说明**：该变更无法 INSTANT/INPLACE 完成，将重建整表，预估 {estimated_minutes} 分钟

### EX-A09 含表名变更（RENAME）

- **级别**：block
- **适用**：alter_table
- **判定**：alter_ops intersects (RENAME_TABLE)
- **说明**：ALTER TABLE ... RENAME 会改变表标识，后续语句与回滚都失去锚点，不在开放操作内

### EX-A10 ALTER 含无法识别的操作

- **级别**：block
- **适用**：alter_table
- **判定**：alter_ops intersects (OTHER)
- **说明**：解析器无法识别该 ALTER 子句，既判不了风险也生成不了回滚，需要人工执行
- **样例**：命中 = ALTER TABLE DEMO_T ALTER COLUMN C SET DEFAULT 'x';

---

## 建索引规则

### EX-I01 目标表不存在

- **级别**：block
- **适用**：create_index
- **判定**：table_exists = false
- **说明**：表 {target_table} 不存在

### EX-I02 索引名已存在

- **级别**：block
- **适用**：create_index
- **判定**：index_names_existing is not empty
- **说明**：索引名已存在：{index_names_existing}

### EX-I03 索引列不存在

- **级别**：block
- **适用**：create_index
- **判定**：index_columns_missing is not empty
- **说明**：以下列不存在：{index_columns_missing}

### EX-I04 与现有索引前缀重复

- **级别**：warn
- **适用**：create_index, alter_table
- **判定**：redundant_index_hit = true
- **说明**：新索引列与现有索引 {existing_index} 前缀相同，可能冗余

### EX-I05 大表加索引

- **级别**：warn
- **适用**：create_index
- **判定**：table_rows >= $big_table_rows
- **说明**：表约 {table_rows} 行，加索引耗时较长，注意 MDL 等待

---

## 插入规则

### EX-N01 目标表不存在

- **级别**：block
- **适用**：insert
- **判定**：table_exists = false
- **说明**：表 {target_table} 不存在

### EX-N02 插入列名不存在

- **级别**：block
- **适用**：insert
- **判定**：insert_columns_missing is not empty
- **说明**：以下列不存在：{insert_columns_missing}

### EX-N03 NOT NULL 列未给值

- **级别**：block
- **适用**：insert
- **判定**：notnull_columns_unfilled is not empty
- **说明**：以下 NOT NULL 列未提供值：{notnull_columns_unfilled}

### EX-N04 未显式给出主键列

- **级别**：warn
- **适用**：insert
- **判定**：insert_has_pk_column = false
- **说明**：未给出主键列，回滚 SQL 只能按自增区间估算，需人工核对

### EX-N05 写入行数超审核阈值

- **级别**：warn
- **适用**：insert
- **判定**：insert_rows > $max_insert_rows
- **说明**：写入 {insert_rows} 行，超过审核阈值 {max_insert_rows}，确认审核结论是否已覆盖

### EX-N06 INSERT IGNORE

- **级别**：block
- **适用**：insert
- **判定**：insert_ignore = true
- **说明**：INSERT IGNORE 会把冲突行静默跳过，实际写入行数与 VALUES 行数不一致，undo 无法界定范围
- **样例**：命中 = INSERT IGNORE INTO DEMO_T (NAME) VALUES ('a');

### EX-N07 ON DUPLICATE KEY UPDATE

- **级别**：block
- **适用**：insert
- **判定**：insert_on_duplicate = true
- **说明**：存在 ON DUPLICATE KEY UPDATE 时会改动本已存在的行，按 DELETE 回滚会误删原有数据

### EX-N08 写入行数无法预知

- **级别**：warn
- **适用**：insert
- **判定**：insert_is_select = true
- **说明**：INSERT ... SELECT 写入行数只能执行后才知道，undo 只能按自增区间估算

---

## 更新规则

### EX-U01 目标表不存在

- **级别**：block
- **适用**：update
- **判定**：table_exists = false
- **说明**：表 {target_table} 不存在

### EX-U02 SET 列不存在

- **级别**：block
- **适用**：update
- **判定**：set_columns_missing is not empty
- **说明**：以下列不存在：{set_columns_missing}

### EX-U03 UPDATE 缺少 WHERE

- **级别**：block
- **适用**：update
- **判定**：has_where = false
- **说明**：无 WHERE 条件，会更新全表
- **样例**：命中 = UPDATE DEMO_T SET NAME = 'x';

### EX-U04 影响行数超上限

- **级别**：block
- **适用**：update
- **判定**：rows_estimate > $max_affected_rows
- **缺失**：skip
- **说明**：预计影响 {rows_estimate} 行，超过上限 {max_affected_rows}
- **原因**：「取不到」由 EX-U05 负责，这里只在拿到值时才判，避免重复拦截

### EX-U05 影响行数无法确定

- **级别**：block
- **适用**：update
- **判定**：rows_estimate is null
- **说明**：COUNT 估算失败或超时，无法确认影响面
- **原因**：拿不准影响面就不执行，这是本服务最核心的 fail-closed 之一

### EX-U06 WHERE 未命中索引

- **级别**：warn
- **适用**：update
- **判定**：where_index_hit = false
- **说明**：EXPLAIN 显示全表扫描，WHERE 未命中索引

### EX-U07 目标表存在触发器

- **级别**：warn
- **适用**：update, insert
- **判定**：table_triggers is not empty
- **说明**：表有触发器 {table_triggers}，实际影响行数可能超出 ROW_COUNT()

### EX-U08 多表 UPDATE

- **级别**：block
- **适用**：update
- **判定**：update_multi_table = true
- **说明**：多表 UPDATE 无法采集单表 before-image，回滚 SQL 无从生成
- **样例**：命中 = UPDATE t1 JOIN t2 ON t1.ID = t2.ID SET t1.A = 1 WHERE t1.ID > 0;
