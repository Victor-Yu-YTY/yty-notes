# MySQL DDL 审核标准

**规则版本**：2026-11-03

本文件是本服务 **DDL 审核的判定依据（唯一来源）**。规则改这里就生效，不用改代码、不用重启。

> **怎么读这份文件**
> - 每条规则一个小节：`### 编号 标题`，下面是固定字段；
> - **级别**决定结论形态：❌ = 驳回/必须整改，⚠️ = 提示（不阻塞提交），✅ = 建议；
> - **检查** = 代码里的判定原语名（`src/sql_review_mcp/ddlchecks.py`），
>   **参数** = 该原语的输入（禁用类型、必备字段、阈值…）。改参数就能改判定口径；
> - **说明** / **整改** 是回给研发的原话，支持 `{占位符}`（可用字段见各检查的声明，或 readme.md）；
> - **情形** 用于"同一条规则多种命中形态"：`情形名 = 级别 | 说明 | 整改`；
> - **自动改写**：`true` 表示命中时服务会直接改写语句（改后的语句见 `sql_final`）；`false` 只给建议；
> - **停用**：`true` 表示本条规则不生效（改这里即下线规则，不用删小节、不用改代码）；
> - 规则按**文档顺序**执行，同一语句内顺序有依赖时请勿随意调整。

条文正文与总纲 `standards/mysql.md` 保持一致，便于两边对照。

## 阈值

| 名称 | 值 | 说明 |
| --- | --- | --- |
| table_name_max_len | 64 | 表名/字段名长度上限 |
| max_columns | 30 | 单表字段数建议上限 |
| max_indexes | 5 | 单表索引数建议上限 |
| max_index_cols | 5 | 组合索引字段数建议上限 |
| max_unique_cols | 3 | 唯一索引字段数建议上限 |
| varchar_prefix_max | 8 | 长 VARCHAR 前缀索引长度上限 |
| varchar_prefix_threshold | 64 | 超过该长度的 VARCHAR 建索引必须用前缀 |
| varchar_instant_bytes | 255 | VARCHAR 跨 255 字节边界不可 INSTANT |
| big_table_rows | 5000000 | 大表标准：≥500 万行 |
| big_table_size_gb | 3 | 大表标准：>3G |
| modify_column_rows | 8000000 | MODIFY COLUMN 执行窗口阈值 |

## 规则

### TB-13 禁用 CREATE TABLE ... SELECT（CTAS）

- **级别**：❌
- **适用**：create_table
- **检查**：forbid_ctas
- **条款**：不允许使用 create table xx select 语法，如有初始化数据需求请分成两步走
- **说明**：禁止使用 CREATE TABLE ... SELECT（CTAS）
- **整改**：请拆成两步：先 CREATE TABLE，再用 INSERT ... SELECT 迁移数据
- **样例**：命中 = CREATE TABLE DEMO_X SELECT * FROM DEMO_Y;

### TB-08 五个必备字段

- **级别**：❌
- **适用**：create_table
- **检查**：required_columns
- **参数**：columns = ID BIGINT | IS_DEL TINYINT | CREATE_TIME DATETIME | MODIFY_TIME DATETIME | __MODIFY_TIME__ DATETIME; on_update = MODIFY_TIME, __MODIFY_TIME__
- **自动改写**：true
- **条款**：每张表必须包含的字段：ID、CREATE_TIME、MODIFY_TIME、IS_DEL、__MODIFY_TIME__
- **情形**：missing = ❌ | 缺少规范必备字段 {name} | 已按规范补 `{name}` {type}
- **情形**：mismatch = ❌ | 必备字段 {name} 定义与规范样例不一致 | {changed}
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT 'x');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-09 三个必备索引

- **级别**：❌
- **适用**：create_table
- **检查**：required_indexes
- **参数**：indexes = PRIMARY ID | IDX_CREATE_TIME CREATE_TIME | IDX__MODIFY_TIME__ __MODIFY_TIME__
- **自动改写**：true
- **条款**：每张表必须包含 PRIMARY KEY (ID)、IDX_CREATE_TIME、IDX__MODIFY_TIME__ 三个索引
- **情形**：missing = ❌ | 缺少规范必备索引 {label} | 已补 `{label}`
- **情形**：mismatch = ❌ | 必备索引 {name} 的字段不正确 | 已改为 ({columns})
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-01 表名与字段名命名

- **级别**：❌
- **适用**：create_table
- **检查**：naming
- **参数**：uppercase = true; max_len = 64; require_business_prefix = true
- **自动改写**：true
- **条款**：表名、字段名使用大写字母、'_' 下划线分割，表名最前面加上业务名；禁止使用 MySQL 保留字
- **情形**：uppercase = ❌ | {kind}必须使用大写字母 | 已改为 {fixed}
- **情形**：reserved = ❌ | {kind}使用了 MySQL 保留字 | 请改用业务含义命名
- **情形**：length = ❌ | {kind}长度 {max_len} 超过上限 | 请缩短 {name}
- **情形**：charset = ❌ | {kind}只能包含大写字母、数字和下划线，且以字母开头 | 已改为 {fixed}
- **情形**：illegal = ❌ | {kind}含非法字符，无法自动改写 | 请手工改名
- **情形**：duplicate = ❌ | {kind}重复 | 请重命名其中一个
- **情形**：no_business = ❌ | 表名最前面缺少业务名（规范要求「业务名_对象描述」） | 不擅自补业务名，请改为 <业务名>_{name}
- **样例**：命中 = CREATE TABLE t_order (ID BIGINT NOT NULL COMMENT 'x');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-02 表与字段必须有注释

- **级别**：❌
- **适用**：create_table
- **检查**：comments
- **自动改写**：true
- **条款**：表及字段必须有注释，修改字段含义和更新状态值时，及时更新注释
- **情形**：table_missing = ❌ | 表缺少 COMMENT | 已补占位注释 '{comment}'，请补充真实含义
- **情形**：column_missing = ❌ | 字段缺少 COMMENT | 已补占位注释 '{comment}'，请补充真实含义
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL AUTO_INCREMENT);
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-03 禁用 FLOAT / DOUBLE 存浮点

- **级别**：❌
- **适用**：create_table
- **检查**：forbidden_types
- **参数**：types = FLOAT, DOUBLE, REAL; fix_mode = decimal; replace_with = DECIMAL(12,2)
- **自动改写**：true
- **条款**：存储浮点数必须使用 DECIMAL，禁止使用 FLOAT 和 DOUBLE
- **说明**：禁止使用 {old} 存储浮点数，必须用 DECIMAL
- **整改**：已改为 {replace_with}，请确认精度
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', AMOUNT FLOAT NOT NULL COMMENT 'a');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-04 禁用 ENUM / SET

- **级别**：❌
- **适用**：create_table
- **检查**：forbidden_types
- **参数**：types = ENUM, SET; fix_mode = enum; replace_with = TINYINT
- **自动改写**：true
- **条款**：禁止使用 ENUM，SET 类型，使用 TINYINT 来代替
- **说明**：禁止使用 {type} 类型
- **整改**：已改为 {replace_with}，并写入注释「{note}」
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', STATUS ENUM('a','b') NOT NULL COMMENT 's');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-05 禁用 VARBINARY / BLOB 存文件

- **级别**：❌
- **适用**：create_table
- **检查**：forbidden_types
- **参数**：types = VARBINARY, TINYBLOB, BLOB, MEDIUMBLOB, LONGBLOB; fix_mode = none; store = 文件服务器
- **自动改写**：false
- **条款**：禁止在数据库中使用 VARBINARY、BLOB 存储图片、文件，使用文件服务器存储
- **说明**：禁止使用 {type} 在库里存图片/文件
- **整改**：改用 {store} 存储，表里只保留路径字段
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', FILE_BODY BLOB NOT NULL COMMENT 'f');

### TB-06 时间字段统一 DATETIME

- **级别**：❌
- **适用**：create_table
- **检查**：datetime_types
- **参数**：allowed = DATETIME; forbid_defaults = 0000-00-00 00:00:00, 0000-00-00; replace_default = 2000-01-01 00:00:00
- **自动改写**：true
- **条款**：时间字段统一使用 DATETIME，不允许为 "0000-00-00 00:00:00" 或 "0000-00-00"
- **情形**：type = ❌ | 时间字段必须统一使用 {allowed} | 已将 {old} 改为 {new}
- **情形**：default_value = ❌ | 默认值不允许 0000-00-00 系列 | 已改为 {default}
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', LOGIN_TIME TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'l');

### TB-07 所有字段 NOT NULL

- **级别**：❌
- **适用**：create_table
- **检查**：not_null
- **自动改写**：true
- **条款**：所有字段都定义为 NOT NULL，默认值也不应该为 NULL
- **说明**：字段 {name} 必须定义为 NOT NULL，默认值不为 NULL
- **整改**：已改写：{changed}（请补充业务默认值）
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', REMARK VARCHAR(10) DEFAULT NULL COMMENT 'r');

### IX-04 禁用外键

- **级别**：❌
- **适用**：create_table, alter_table, create_index
- **检查**：foreign_key
- **自动改写**：false
- **条款**：禁止使用外键，应放到程序端实现
- **说明**：禁止使用外键 {name}
- **整改**：请删除外键，关联校验放在程序端实现
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', FOREIGN KEY (ID) REFERENCES DEMO_Y (ID));

### IX-06 索引引用字段必须存在

- **级别**：❌
- **适用**：create_table, alter_table, create_index, drop_index
- **检查**：index_column_exists
- **条款**：索引引用的字段必须存在于表中，否则语句执行失败
- **情形**：missing_col = ❌ | 索引 {name} 引用了不存在的字段 {columns}（该语句在 MySQL 上会失败） | 请修正字段名或删除该索引
- **情形**：table_missing = ❌ | 目标表 {table} 不存在 | 请确认库名/表名
- **情形**：index_missing = ❌ | 目标表不存在该索引 {name}（DROP INDEX 会执行失败） | 请确认索引名

### IX-03 大文本字段禁入索引

- **级别**：❌
- **适用**：create_table, alter_table, create_index
- **检查**：index_big_text
- **参数**：types = TINYTEXT, TEXT, MEDIUMTEXT, LONGTEXT, TINYBLOB, BLOB, MEDIUMBLOB, LONGBLOB
- **自动改写**：true
- **条款**：禁止大文本字段出现在普通索引、唯一索引、组合索引中
- **说明**：大文本字段不得出现在索引中：{columns}
- **整改**：已从索引 {name} 中移除这些字段

### IX-01 索引命名

- **级别**：❌
- **适用**：create_table, alter_table, create_index
- **检查**：index_naming
- **参数**：idx_prefix = IDX_; uniq_prefix = UNIQ_
- **自动改写**：true
- **条款**：非唯一索引必须按 "IDX_字段名[_字段名]" 命名，唯一索引必须按 "UNIQ_字段名[_字段名]" 命名，且索引名必须大写
- **说明**：索引命名必须为 IDX_字段[_字段] / UNIQ_字段[_字段] 且大写（当前 {name}）
- **整改**：已改为 {want}
- **样例**：命中 = CREATE INDEX idx_order ON DEMO_T (COL_A);

### IX-02 长 VARCHAR 必须前缀索引

- **级别**：❌
- **适用**：create_table, alter_table, create_index
- **检查**：varchar_prefix
- **自动改写**：true
- **条款**：对长度过长的 VARCHAR 字段建立索引时，必须添加前缀索引，前缀索引不应该超过 8 个字符
- **情形**：prefixed = ❌ | 长 VARCHAR({length}) 建索引必须使用前缀索引（≤{max}） | 已改为 `{column}`({max})
- **情形**：unique_bad = ❌ | 唯一索引不能对长 VARCHAR({length}) 加前缀（会破坏唯一性） | 请缩短字段长度，或不使用唯一索引

### IX-05 索引数量与冗余

- **级别**：⚠️
- **适用**：create_table, alter_table, create_index
- **检查**：index_limits
- **条款**：索引中的字段数建议不超过 5 个；单张表索引数量控制在 5 个以内；唯一索引字段数控制在 3 个以下
- **情形**：count = ⚠️ | 索引数量 {count} 超过建议上限 {max} | 建议合并或删除低效索引
- **情形**：cols = ⚠️ | 索引字段数 {count} 超过建议上限 {max} | 建议缩短组合索引
- **情形**：unique_cols = ⚠️ | 唯一索引字段数 {count} 超过建议上限 {max} | 建议缩短唯一键
- **情形**：duplicate = ❌ | 目标表已存在同名索引 {name}（重名会执行失败） | 请换个索引名，或确认是要重建索引
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', A1 INT NOT NULL COMMENT 'a', A2 INT NOT NULL COMMENT 'a', A3 INT NOT NULL COMMENT 'a', A4 INT NOT NULL COMMENT 'a', A5 INT NOT NULL COMMENT 'a', A6 INT NOT NULL COMMENT 'a', KEY IDX_A1 (A1), KEY IDX_A2 (A2), KEY IDX_A3 (A3), KEY IDX_A4 (A4), KEY IDX_A5 (A5), KEY IDX_A6 (A6));

### AL-01 禁用 AFTER / FIRST

- **级别**：❌
- **适用**：alter_table
- **检查**：after_first
- **自动改写**：true
- **条款**：禁止修改字段顺序（ALTER 时禁止使用 AFTER / FIRST）
- **说明**：禁止使用 AFTER / FIRST（会导致无法 INSTANT 秒加字段）
- **整改**：已移除 {clause} 子句
- **样例**：命中 = ALTER TABLE DEMO_T ADD COLUMN A INT NOT NULL DEFAULT 0 COMMENT 'a' AFTER ID;

### AL-02 同表多个 ALTER 必须合并为一条

- **级别**：❌
- **适用**：alter_table
- **检查**：merge_alter
- **自动改写**：true
- **条款**：ALTER 操作时（如加字段、修改字段、加索引等），如果对同一张表的多个操作，应合并到一条语句中一起执行
- **情形**：merged = ❌ | 对同一张表有 {count} 条 ALTER 语句，必须合并为一条 | 已自动合并为一条 ALTER（子句顺序保持原样）
- **情形**：merged_into = ✅ | 已合并到第 {index} 条语句，本条不再单独提交 | （无需改动）
- **样例**：命中 = ALTER TABLE DEMO_T ADD COLUMN A1 INT NOT NULL DEFAULT 0 COMMENT 'a'; ALTER TABLE DEMO_T ADD COLUMN A2 INT NOT NULL DEFAULT 0 COMMENT 'b';

### AL-05 ADD COLUMN 规范

- **级别**：❌
- **适用**：alter_table
- **检查**：add_column
- **自动改写**：true
- **条款**：新增字段必须带注释、非空、有默认值；不得使用 FLOAT/ENUM/BLOB；不得带 AFTER/FIRST
- **情形**：hit = ❌ | 新增字段 {column} 不符合规范 | 已改写：{changed}
- **情形**：exists = ❌ | 目标表已存在同名字段（{type}），ADD COLUMN 会执行失败 | 请改用 MODIFY COLUMN，或换个字段名
- **情形**：big_object = ❌ | 禁止使用 BLOB / VARBINARY 存储文件（{column}） | 请使用文件服务器，库里只存路径
- **样例**：命中 = ALTER TABLE DEMO_T ADD COLUMN A INT;

### AL-04 禁改名 / 改字符集 / 删字段

- **级别**：❌
- **适用**：alter_table
- **检查**：forbid_rename
- **条款**：禁止修改字符集、字段名、索引名，禁止修改字段顺序，禁止删除字段
- **情形**：rename_col = ❌ | 禁止修改字段名（{old} → {new}） | 请改回原字段名，或走新增字段方案
- **情形**：rename_index = ❌ | 禁止修改索引名（{old} → {new}） | 如需重建索引，请走 DROP + ADD（且需审批）
- **情形**：rename_table = ⚠️ | 表重命名需 DBA 确认（{table} → {new}） | 请确认下游依赖已同步
- **情形**：drop_col = ❌ | 禁止删除字段 {column} | 请保留字段；确认无业务使用后由 DBA 评估下线
- **情形**：drop_col_exists = ⚠️ | 目标表确实存在该字段 {column}，删除前请确认无业务使用（行数约 {rows}） | 建议保留字段，或走 DBA 评估流程
- **情形**：charset = ❌ | 禁止修改字符集 | 字符集统一 utf8mb4，不支持变更
- **情形**：charset_convert = ❌ | 禁止修改字符集（CONVERT TO CHARACTER SET） | 字符集统一 utf8mb4
- **样例**：命中 = ALTER TABLE DEMO_T DROP COLUMN A;

### AL-03 MODIFY / CHANGE 必须声明原类型

- **级别**：❌
- **适用**：alter_table
- **检查**：modify_type
- **条款**：MODIFY / CHANGE 必须声明原类型，并与现网结构一致；不一致需人工确认
- **情形**：consistent = ✅ | 与原类型一致（{old}） | （无需整改）
- **情形**：mismatch = ⚠️ | 声明类型 {new} 与现网原类型 {old} 不一致 | 如确需变更类型请说明原因；如非本意请按原类型填写
- **情形**：unknown = ⏸ | 缺少目标表现有结构，无法判定原类型是否一致 | 传 env + database 即可自动取结构后复审
- **情形**：no_column = ❌ | 目标表中不存在该字段 {column}（MODIFY/CHANGE 只能改已有字段，新增请用 ADD COLUMN） | 请改用 ADD COLUMN，或确认字段名
- **情形**：table_missing = ❌ | 目标表 {table} 不存在（MODIFY/CHANGE 无法执行） | 请确认库名/表名；新增表请用 CREATE TABLE

### AL-07 VARCHAR 跨 255 字节不可 INSTANT

- **级别**：⚠️
- **适用**：alter_table
- **检查**：varchar_instant
- **条款**：VARCHAR 长度跨越 255 字节边界时无法 INSTANT 加字段，需评估执行方式
- **情形**：add_cross = ⚠️ | 新增 VARCHAR({length}) 超过 {bytes} 字节，无法 INSTANT，需评估执行方式 | 建议缩短长度，或与 DBA 确认变更窗口
- **情形**：modify_cross = ⚠️ | VARCHAR({old}) → VARCHAR({new}) 跨越 {bytes} 字节边界，无法 INSTANT | 请与 DBA 确认执行窗口（建议 21:00~00:00）
- **情形**：unknown = ⏸ | 缺少原字段定义，无法判断 VARCHAR 是否跨 {bytes} 字节边界 | 传 env + database 后复审
- **样例**：命中 = ALTER TABLE DEMO_T ADD COLUMN A VARCHAR(300) NOT NULL DEFAULT '' COMMENT 'a';

### AL-08 建议显式声明 ALGORITHM / LOCK

- **级别**：✅
- **适用**：alter_table
- **检查**：algorithm_hint
- **参数**：hint = ALGORITHM=INPLACE LOCK=NONE
- **条款**：执行 DDL 时建议显式声明 ALGORITHM / LOCK，便于 DBA 判断执行方式
- **说明**：【推荐】显式声明 {hint}
- **整改**：便于 DBA 判断执行方式；MySQL 8.0 多数 ADD COLUMN 可 INSTANT，但 MODIFY 需评估
- **样例**：命中 = ALTER TABLE DEMO_T MODIFY COLUMN A INT NOT NULL DEFAULT 0 COMMENT 'a';

### TB-10 显式指定 InnoDB 与 utf8mb4

- **级别**：⚠️
- **适用**：create_table
- **检查**：engine_charset
- **参数**：engine = InnoDB; charset = utf8mb4
- **自动改写**：true
- **条款**：建表时需显式指定 InnoDB 存储引擎，字符集统一使用 utf8mb4
- **说明**：【推荐】显式指定 {engine} 与 {charset}
- **整改**：{changed}
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x') ENGINE=MyISAM;
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-11 历史 / 归档 / 临时 / 备份表命名

- **级别**：❌
- **适用**：create_table
- **检查**：table_type_naming
- **参数**：suffixes.历史表 = _HISTORY; suffixes.临时表 = _TMP_创建日期_保留月份; suffixes.备份表 = _BAK_创建日期_保留月份; prefixes.归档表 = ARCHIVER_
- **条款**：历史表名必须以 '_HISTORY' 为后缀；归档表名必须以 'ARCHIVER_' 为前缀；临时表名必须以 '_TMP_创建日期_保留月份' 为后缀；备份表名必须以 '_BAK_创建日期_保留月份' 为后缀
- **情形**：bad_date = ❌ | 临时/备份表名中的日期应为 YYYYMMDD（当前 {date}） | 请修正日期部分（如 _BAK_20260101_3）
- **情形**：no_months = ❌ | 临时/备份表必须标注保留月数（当前 {months}） | 请补保留月数
- **情形**：suspect = ❌ | 疑似历史/备份/临时/归档表，但命名不规范（{name}） | {rules}
- **样例**：命中 = CREATE TABLE DEMO_CITY_TMP_20190101 (ID BIGINT NOT NULL COMMENT 'x');

### TB-12 数量约束

- **级别**：⚠️
- **适用**：create_table
- **检查**：max_columns
- **条款**：单表字段数控制在 30 以内
- **说明**：单表字段数 {count} 超过建议上限 {max}
- **整改**：建议按业务拆分，或评估是否真的需要这么多字段

### TB-14 整型不带长度 / CHAR 超长改 VARCHAR

- **级别**：⚠️
- **适用**：create_table
- **检查**：type_width
- **参数**：forbid_int_length = true; char_max = 20; varchar_hint = 1024
- **自动改写**：true
- **条款**：整型定义中不添加长度（如使用 INT 而非 INT(4)）；如果定义的 CHAR 字段超过 CHAR(20)，则改为 VARCHAR
- **情形**：int_length = ⚠️ | 整型定义不写长度 | 已改为 {new}（原 {old}）
- **情形**：char_wide = ⚠️ | CHAR({length}) 超过 CHAR({max}) 应改 VARCHAR | 已改为 {new}
- **情形**：varchar_hint = ⚠️ | VARCHAR({length}) 偏大，请按实际宽度定义（建议 ≤{hint}） | 请确认实际最大长度
- **样例**：命中 = CREATE TABLE DEMO_T (ID BIGINT NOT NULL COMMENT 'x', N INT(11) NOT NULL COMMENT 'n');
- **样例**：通过 = CREATE TABLE DEMO_ORDER (ID BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键', IS_DEL TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除', CREATE_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间', MODIFY_TIME DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间', __MODIFY_TIME__ DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'BI', ORDER_NO VARCHAR(32) NOT NULL DEFAULT '' COMMENT '订单号', PRIMARY KEY (ID), KEY IDX_CREATE_TIME (CREATE_TIME), KEY IDX__MODIFY_TIME__ (__MODIFY_TIME__)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单变更'

### TB-15 库名前缀与长度

- **级别**：❌
- **适用**：create_table, alter_table
- **检查**：db_name
- **参数**：prefix = demo_; max_len = 64
- **条款**：命名规范："demo_" + 业务描述简写，总长度不超过 64；库名小写，禁止使用 MySQL 保留字
- **说明**：库名 {db} 必须为 {prefix} + 业务描述简写，全小写且长度 ≤{max_len}
- **整改**：请确认库名（第三方库可不按此规范）
- **样例**：命中 = CREATE TABLE bad_db.DEMO_T (ID BIGINT NOT NULL COMMENT 'x');

### TB-16 目标库不得存在同名表

- **级别**：❌
- **适用**：create_table
- **检查**：table_exists
- **条款**：建表前必须确认目标库不存在同名表
- **说明**：目标库已存在同名表（{table}，约 {rows} 行）
- **整改**：请换个表名，或确认是要变更现有表（ALTER）

### DX-01 禁止 DROP TABLE

- **级别**：❌
- **适用**：drop_table
- **检查**：always
- **条款**：禁止物理删除数据（除数据归档、迁移外）
- **说明**：禁止物理删除表（DROP TABLE {name}）
- **整改**：除归档/迁移外一律驳回；请走数据归档/备份流程，或改走工单系统
- **样例**：命中 = DROP TABLE DEMO_T;

### DX-02 禁止 TRUNCATE

- **级别**：❌
- **适用**：truncate
- **检查**：always
- **条款**：禁止物理删除数据（除数据归档、迁移外）
- **说明**：禁止物理清空表（TRUNCATE {name}）
- **整改**：请走数据归档/迁移并留痕，或改走工单系统
- **样例**：命中 = TRUNCATE TABLE DEMO_T;

### DX-03 禁止 DROP DATABASE

- **级别**：❌
- **适用**：drop_database
- **检查**：always
- **条款**：禁止物理删除数据（除数据归档、迁移外）
- **说明**：禁止 DROP DATABASE / DROP SCHEMA（{name}）
- **整改**：如需下线库请走 DBA 归档流程
- **样例**：命中 = DROP DATABASE bad_db;

### DX-04 索引删除需审批

- **级别**：⚠️
- **适用**：drop_index, alter_table
- **检查**：always
- **条款**：索引不建议删除，如果有需求，审批严格执行，且责任划分清楚
- **说明**：【推荐】索引不建议删除；确需删除需审批严格执行并明确责任人（{name}）
- **整改**：请在工单说明删除原因与影响评估
- **样例**：命中 = DROP INDEX IDX_A ON DEMO_T;

### SC-01 大表标准（≥500 万行或 >3G）

- **级别**：⚠️
- **适用**：create_table, alter_table, create_index
- **检查**：big_table
- **条款**：对大表（≥500 万行记录，或表大小超过 3G）或影响行数超 1 万行的操作，需提前一天与 DBA 沟通执行可行性，确认 canal 消费无积压，并与 BI 确认影响
- **情形**：big = ⚠️ | 目标表约 {rows} 行 / {size_gb} GB，触发大表标准（≥{std_rows} 行或 ≥{std_gb}G） | 需提前一天与 DBA 沟通可行性；确认 canal 消费无积压、与 BI 确认影响
- **情形**：unknown = ⏸ | 缺少目标表行数/容量，无法判断是否触发大表标准 | 传 env + database + table 即可自动取 information_schema 统计值

### SC-02 大表 MODIFY COLUMN 执行窗口

- **级别**：⚠️
- **适用**：alter_table
- **检查**：modify_window
- **参数**：window = 21:00~00:00
- **条款**：大表 MODIFY COLUMN 需限定执行窗口（21:00~00:00）
- **说明**：目标表约 {rows} 行（≥{max_rows}），MODIFY COLUMN 需限定执行窗口
- **整改**：执行窗口建议 {window}，需 DBA 确认

## 不在本服务审核范围（仅披露）

以下条款属于本服务职责之外（查询类、流程类），不产生规则，列在这里以便对照总纲：

* **【强制】** 禁止在 SELECT 语句中出现 `SELECT * FROM`
* **【强制】** 不要使用 COUNT(列名) 或 COUNT(常量) 来替代 COUNT(*)
* **【强制】** 日常数据查询、数据修正通过 SQL 审核平台申请执行
* **【强制】** 上线单中的 SQL、或风险较高的数据库操作，通过工单系统申请执行

---

## 附录：规则编号与检查对照

| 编号段 | 类别 | 覆盖 |
| --- | --- | --- |
| TB-xx | 建表 | 命名、注释、五个必备字段、三个必备索引、禁用类型、NOT NULL、DATETIME、数量约束、CTAS、库名 |
| IX-xx | 索引 | 命名、长 VARCHAR 前缀、大字段禁入索引、禁外键、数量与冗余、字段必须存在 |
| AL-xx | 变更 | 禁 AFTER/FIRST、同表合并、MODIFY 原类型、禁改名/改字符集/删字段、ADD COLUMN 规范、VARCHAR 跨 255、ALGORITHM 提示 |
| DX-xx | 破坏性 | 禁 DROP TABLE / TRUNCATE / DROP DATABASE、索引删除需审批 |
| SC-xx | 规模 | 大表标准、MODIFY COLUMN 执行窗口 |
