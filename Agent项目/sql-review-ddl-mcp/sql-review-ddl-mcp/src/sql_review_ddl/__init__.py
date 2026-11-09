"""sql-review-ddl-mcp：SQL Review DDL 服务（建表 / 变更表 / 索引 / 破坏性操作）。

由 sql-review-mcp v1.4.0 **拆分**而来：v1.0.0 是拆分后的第一个版本，
判定引擎与规则内容与 v1.4.0 完全一致，只裁剪了工具面：

  * 工具面 8 个：review_ddl / review_create_table / review_alter_table /
    review_index_change / review_destructive / get_table_structure /
    check_table_conflict / get_rules
  * **不含 review_dml** —— DML 审核在独立的 SQL Review DML 服务
    （sql-review-dml-mcp，8030）里
  * 规范只加载 standards/ddl.md；standards/mysql.md 是供人读的总纲，不参与判定

引擎沿用 v1.4 的"规则全文档化"：

  * 级别 / 说明 / 整改 / 情形文案 / 阈值 / 参数 / 适用范围 / 停用 / 反问 —— 全在 md 里；
  * 代码只保留"判定原语"：``ddlchecks.py`` 的结构化检查（判定 + 必要时改写）；
  * 改规则只改 standards/ddl.md，**不用改代码、不用重启**
    （按 mtime 热加载；写坏不切换，继续用上一版并带 warning）。
  * v1.0.1：审计额外记录**调用方身份**（网关透传的机器 IP + AgentID），
    用于追溯"哪台机器、哪个 Agent 调了哪个工具"；只落日志，不参与任何判定。
"""

from pathlib import Path

__version__ = "1.0.1"


def _read_version() -> str:
    try:
        return ((Path(__file__).resolve().parents[2] / "VERSION")
                .read_text(encoding="utf-8").strip() or __version__)
    except OSError:
        return __version__


version = _read_version()
