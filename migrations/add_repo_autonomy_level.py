"""
Migration: add_repo_autonomy_level
Description: Add project_repo_bindings.autonomy_level for progressive PR approval (L0-L2).
Created: 2026-08-31
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        return connection.execute(
            db.text("SHOW COLUMNS FROM `%s` LIKE :col" % table_name),
            {"col": column_name},
        ).first() is not None
    elif dialect == "postgresql":
        return connection.execute(
            db.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table_name AND column_name = :col"
            ),
            {"table_name": table_name, "col": column_name},
        ).first() is not None
    else:
        result = connection.execute(
            db.text(f"PRAGMA table_info({table_name})")
        ).fetchall()
        return any(row[1] == column_name for row in result)


def _table_exists(connection, table_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        return connection.execute(
            db.text("SHOW TABLES LIKE :table_name"), {"table_name": table_name}
        ).first() is not None
    else:
        return connection.execute(
            db.text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table_name"),
            {"table_name": table_name},
        ).first() is not None


def upgrade(connection):
    # AgentTaskEvent.agent_id 放宽为可空（平台/系统发起的 PR 审批事件无 agent）
    if _table_exists(connection, "agent_task_events") and _column_exists(
        connection, "agent_task_events", "agent_id"
    ):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE agent_task_events MODIFY agent_id INTEGER NULL "
                "COMMENT 'Agent ID (nullable for platform-originated approval events)'"
            ))
            print("Relaxed agent_task_events.agent_id to nullable.")

    if _table_exists(connection, "project_repo_bindings") and not _column_exists(
        connection, "project_repo_bindings", "autonomy_level"
    ):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE project_repo_bindings ADD COLUMN autonomy_level INTEGER NOT NULL DEFAULT 0 "
                "COMMENT 'Autonomy level: 0=approve all, 1=auto PR + manual merge, 2=auto merge when evidence passes'"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE project_repo_bindings ADD COLUMN autonomy_level INTEGER NOT NULL DEFAULT 0"
            ))
        print("Added project_repo_bindings.autonomy_level column.")


def downgrade(connection):
    if _table_exists(connection, "project_repo_bindings") and _column_exists(
        connection, "project_repo_bindings", "autonomy_level"
    ):
        dialect = connection.dialect.name
        if dialect == "sqlite":
            print("SQLite: cannot drop column autonomy_level (manual step required).")
        else:
            connection.execute(db.text(
                "ALTER TABLE project_repo_bindings DROP COLUMN autonomy_level"
            ))
            print("Dropped project_repo_bindings.autonomy_level column.")


def migrate():
    try:
        print("Running migration: add_repo_autonomy_level...")
        with db.engine.connect() as connection:
            upgrade(connection)
            connection.commit()
        print("Migration completed successfully.")
        return True
    except Exception as e:
        print(f"Migration failed: {e}")
        db.session.rollback()
        return False


if __name__ == "__main__":
    from app import create_app

    app = create_app()
    with app.app_context():
        migrate()
