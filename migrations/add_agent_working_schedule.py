"""
Migration: add_agent_working_schedule
Description: agents 表新增 working_schedule JSON 列——Agent 工作时间区间
             （includes/excludes 时间窗，支持日/周/月循环、日期界限与时区，
             语义见 services/agent_working_schedule.py）。
Created: 2026-09-09
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
            ),
            {"t": table_name, "c": column_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text(f"PRAGMA table_info({table_name})")
    ).fetchall()
    return any(row[1] == column_name for row in rows)


def upgrade(connection):
    if _column_exists(connection, "agents", "working_schedule"):
        print("⏭️  列 agents.working_schedule 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 添加列 agents.working_schedule ...")
    if dialect == "mysql":
        connection.execute(
            db.text(
                "ALTER TABLE agents ADD COLUMN working_schedule JSON NULL "
                "COMMENT '工作时间区间配置'"
            )
        )
    else:
        connection.execute(
            db.text("ALTER TABLE agents ADD COLUMN working_schedule TEXT")
        )
    print("✅ agents.working_schedule 就绪")


def downgrade(connection):
    if _column_exists(connection, "agents", "working_schedule"):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(
                db.text("ALTER TABLE agents DROP COLUMN working_schedule")
            )
        else:
            # SQLite 3.35+ 支持 DROP COLUMN
            connection.execute(
                db.text("ALTER TABLE agents DROP COLUMN working_schedule")
            )
