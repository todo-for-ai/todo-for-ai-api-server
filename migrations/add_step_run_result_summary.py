"""
Migration: add_step_run_result_summary
Description: workflow_step_runs 增加 result_summary 列——子工作流完成回传
             父步骤的关联载体（_propagate_sub_workflow_completion 以
             LIKE '%sub_workflow_run:{id}%' 定位父步骤；此前该列缺失，
             子工作流完成回传与工作流正常完成路径必 AttributeError）。
Created: 2026-09-11
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402

_TABLE = "workflow_step_runs"
_COLUMN = "result_summary"


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t "
                "AND column_name = :c"
            ),
            {"t": table_name, "c": column_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text(f"PRAGMA table_info({table_name})")
    ).fetchall()
    return any(row[1] == column_name for row in rows)


def upgrade(connection):
    if _column_exists(connection, _TABLE, _COLUMN):
        print(f"⏭️  列 {_TABLE}.{_COLUMN} 已存在，跳过")
        return
    connection.execute(
        db.text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} TEXT NULL")
    )
    print(f"✅ {_TABLE}.{_COLUMN} 已添加")


def downgrade(connection):
    dialect = connection.dialect.name
    if dialect == "mysql":
        if _column_exists(connection, _TABLE, _COLUMN):
            connection.execute(
                db.text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")
            )
    else:
        print("⚠️  SQLite 不支持 DROP COLUMN，跳过回滚（保留列不影响运行）")
    print("✅ result_summary 回滚完成")
