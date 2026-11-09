"""sql-review-dml-mcp：SQL Review DML 服务（INSERT / UPDATE 审核，DELETE / REPLACE 驳回）。

由 sql-review-mcp v1.4.0 **拆分**而来：v1.0.0 是拆分后的第一个版本，
判定引擎与规则内容与 v1.4.0 完全一致，只裁剪了工具面：

  * 工具面 4 个：review_dml / get_table_structure / check_table_conflict / get_rules
  * **不含 DDL 工具** —— DDL 审核在独立的 SQL Review DDL 服务
    （sql-review-ddl-mcp，8010）里
  * 规范只加载 standards/dml.md；standards/mysql.md 是供人读的总纲，不参与判定
  * **只报错不代劳**：sql_final 恒等于 sql_input；DELETE / REPLACE INTO 一律驳回（DM-12 fatal）

引擎沿用 v1.4 的"规则全文档化"：

  * 级别 / 说明 / 整改 / 阈值 / 参数 / 适用范围 / 停用 / 反问 —— 全在 dml.md 里；
  * 代码只保留"判定原语"：``dmfacts.py`` 的事实 + ``engine.py`` 的表达式求值；
  * 改规则只改 standards/dml.md，**不用改代码、不用重启**
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
