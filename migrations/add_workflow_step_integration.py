"""
Migration: add_workflow_step_integration
Description: workflow_steps 增加 integration_config 列——外部工作流平台
             （Dify / Coze）连接器配置。配置了该列的步骤由平台直接调用
             远端工作流 API 执行，不再创建 Agent 任务。
             api_key 以 encrypt_str 密文入库，API 返回时脱敏。
Created: 2026-09-17
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402

_TABLE = "workflow_steps"
_COLUMN = "integration_config"


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
    # MySQL 用原生 JSON；SQLite 无 JSON 类型，TEXT 承载 SQLAlchemy JSON 序列化
    col_type = "JSON" if connection.dialect.name == "mysql" else "TEXT"
    connection.execute(
        db.text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} {col_type} NULL")
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
    print("✅ integration_config 回滚完成")
