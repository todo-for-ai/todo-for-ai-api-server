"""
Migration: add_marketplace_publishing
Description: Phase 4 数字员工市场 - agent_role_templates 增加
             published_to_marketplace / published_at，模板可发布到市场被跨工作区安装。
Created: 2026-09-01
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


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


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        return connection.execute(
            db.text("SHOW COLUMNS FROM `%s` LIKE :col" % table_name),
            {"col": column_name},
        ).first() is not None
    else:
        result = connection.execute(
            db.text(f"PRAGMA table_info({table_name})")
        ).fetchall()
        return any(row[1] == column_name for row in result)


def upgrade(connection):
    dialect = connection.dialect.name

    if not _table_exists(connection, "agent_role_templates"):
        return

    if not _column_exists(connection, "agent_role_templates", "published_to_marketplace"):
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE agent_role_templates ADD COLUMN published_to_marketplace BOOLEAN NOT NULL DEFAULT 0 "
                "COMMENT 'Published to digital-employee marketplace'"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE agent_role_templates ADD COLUMN published_to_marketplace BOOLEAN NOT NULL DEFAULT 0"
            ))
        print("Added agent_role_templates.published_to_marketplace.")

    if not _column_exists(connection, "agent_role_templates", "published_at"):
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE agent_role_templates ADD COLUMN published_at DATETIME "
                "COMMENT 'Publish time'"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE agent_role_templates ADD COLUMN published_at DATETIME"
            ))
        print("Added agent_role_templates.published_at.")


def downgrade(connection):
    dialect = connection.dialect.name
    if _table_exists(connection, "agent_role_templates"):
        for col in ("published_at", "published_to_marketplace"):
            if _column_exists(connection, "agent_role_templates", col):
                if dialect == "sqlite":
                    print(f"SQLite: cannot drop {col} (manual step).")
                else:
                    connection.execute(db.text(
                        f"ALTER TABLE agent_role_templates DROP COLUMN {col}"
                    ))
                    print(f"Dropped agent_role_templates.{col}.")


def migrate():
    try:
        print("Running migration: add_marketplace_publishing...")
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
