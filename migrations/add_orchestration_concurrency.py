"""
Migration: add_orchestration_concurrency
Description: workspace_runtime_settings 增加 max_concurrent_agents 列——
             工作区「同时干活」Agent 数上限（按未过期活跃租约去重计数），
             供多 Agent 编排派发门禁使用；NULL=系统默认，0=不限。
Created: 2026-09-10
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
    if _column_exists(connection, "workspace_runtime_settings", "max_concurrent_agents"):
        print("⏭️  列 workspace_runtime_settings.max_concurrent_agents 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 添加列 workspace_runtime_settings.max_concurrent_agents ...")
    if dialect == "mysql":
        connection.execute(
            db.text(
                "ALTER TABLE workspace_runtime_settings ADD COLUMN max_concurrent_agents INT NULL "
                "COMMENT '同时干活的 Agent 数上限；NULL=系统默认，0=不限'"
            )
        )
    else:
        connection.execute(
            db.text(
                "ALTER TABLE workspace_runtime_settings ADD COLUMN max_concurrent_agents INTEGER"
            )
        )
    print("✅ workspace_runtime_settings.max_concurrent_agents 就绪")


def downgrade(connection):
    if _column_exists(connection, "workspace_runtime_settings", "max_concurrent_agents"):
        connection.execute(
            db.text("ALTER TABLE workspace_runtime_settings DROP COLUMN max_concurrent_agents")
        )
