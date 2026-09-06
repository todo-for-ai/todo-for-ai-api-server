"""
Migration: add_goal_loop_time_budget
Description: goal_loops 增加 time_budget_hours——多日续航护栏。循环超过时长
             预算后不再推进新轮次并进入终态（轮数上限之外的第三条护栏），
             支持 Agent 连续跑几天攻克一个目标。
Created: 2026-09-06
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402
from migrations.add_budgets import _table_exists  # noqa: E402  (reuse helper)


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
        db.text("PRAGMA table_info(:t)"), {"t": table_name}
    ).fetchall()
    return any(r[1] == column_name for r in rows)


def upgrade(connection):
    if not _table_exists(connection, "goal_loops"):
        print("⏭️  表 goal_loops 不存在，跳过")
        return
    if _column_exists(connection, "goal_loops", "time_budget_hours"):
        print("⏭️  列 goal_loops.time_budget_hours 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 添加列 goal_loops.time_budget_hours ...")
    if dialect == "mysql":
        connection.execute(
            db.text(
                "ALTER TABLE goal_loops ADD COLUMN time_budget_hours INT NULL "
                "COMMENT '时长预算（小时），NULL=不限时；超时后不再推进新轮次' "
                "AFTER rounds_limit"
            )
        )
    else:
        connection.execute(
            db.text("ALTER TABLE goal_loops ADD COLUMN time_budget_hours INTEGER")
        )
    print("✅ time_budget_hours 列就绪")


def downgrade(connection):
    if _table_exists(connection, "goal_loops") and _column_exists(
        connection, "goal_loops", "time_budget_hours"
    ):
        connection.execute(
            db.text("ALTER TABLE goal_loops DROP COLUMN time_budget_hours")
        )
