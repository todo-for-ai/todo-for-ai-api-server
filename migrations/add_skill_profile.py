"""
Migration: add_skill_profile
Description: P3.1 SOUL v2 - agents.skill_profile JSON + skill_profile_updated_at.
Created: 2026-08-31
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

    if _table_exists(connection, "agents"):
        if not _column_exists(connection, "agents", "skill_profile"):
            if dialect == "mysql":
                connection.execute(db.text(
                    "ALTER TABLE agents ADD COLUMN skill_profile JSON "
                    "COMMENT 'P3.1 skill profile aggregated from run history'"
                ))
            else:
                connection.execute(db.text(
                    "ALTER TABLE agents ADD COLUMN skill_profile TEXT"
                ))
            print("Added agents.skill_profile.")
        if not _column_exists(connection, "agents", "skill_profile_updated_at"):
            if dialect == "mysql":
                connection.execute(db.text(
                    "ALTER TABLE agents ADD COLUMN skill_profile_updated_at DATETIME "
                    "COMMENT 'Last skill profile rebuild time'"
                ))
            else:
                connection.execute(db.text(
                    "ALTER TABLE agents ADD COLUMN skill_profile_updated_at DATETIME"
                ))
            print("Added agents.skill_profile_updated_at.")


def downgrade(connection):
    dialect = connection.dialect.name
    if _table_exists(connection, "agents"):
        for col in ("skill_profile_updated_at", "skill_profile"):
            if _column_exists(connection, "agents", col):
                if dialect == "sqlite":
                    print(f"SQLite: cannot drop {col} (manual step).")
                else:
                    connection.execute(db.text(
                        f"ALTER TABLE agents DROP COLUMN {col}"
                    ))
                    print(f"Dropped agents.{col}.")


def migrate():
    try:
        print("Running migration: add_skill_profile...")
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
