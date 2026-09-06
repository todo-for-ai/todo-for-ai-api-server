"""
Migration: add_workspace_runtime_settings
Description: 工作区运行时配额与回收策略表——云端多 Agent 执行的工作区级
             护栏（同时在岗 Pod 上限、空闲回收阈值），Phase 2 交付。
Created: 2026-09-07
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _table_exists(connection, table_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name},
    ).fetchall()
    return bool(rows)


def upgrade(connection):
    if _table_exists(connection, "workspace_runtime_settings"):
        print("⏭️  表 workspace_runtime_settings 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 创建表 workspace_runtime_settings ...")
    if dialect == "mysql":
        connection.execute(
            db.text(
                """
                CREATE TABLE workspace_runtime_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    workspace_id INT NOT NULL,
                    max_pods INT NULL COMMENT '同时在岗 Agent Pod 上限；NULL=系统默认',
                    idle_timeout_minutes INT NULL COMMENT 'Pod 空闲回收阈值（分钟）；NULL=系统默认',
                    created_by INT NULL COMMENT '创建人用户ID',
                    created_at DATETIME NULL,
                    updated_at DATETIME NULL,
                    CONSTRAINT uq_wrs_workspace UNIQUE (workspace_id)
                )
                """
            )
        )
        connection.execute(
            db.text(
                "CREATE INDEX ix_wrs_workspace_id ON workspace_runtime_settings (workspace_id)"
            )
        )
    else:
        connection.execute(
            db.text(
                """
                CREATE TABLE workspace_runtime_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL,
                    max_pods INTEGER,
                    idle_timeout_minutes INTEGER,
                    created_by INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_wrs_workspace UNIQUE (workspace_id)
                )
                """
            )
        )
        connection.execute(
            db.text(
                "CREATE INDEX ix_wrs_workspace_id ON workspace_runtime_settings (workspace_id)"
            )
        )
    print("✅ workspace_runtime_settings 就绪")


def downgrade(connection):
    if _table_exists(connection, "workspace_runtime_settings"):
        connection.execute(db.text("DROP TABLE workspace_runtime_settings"))
