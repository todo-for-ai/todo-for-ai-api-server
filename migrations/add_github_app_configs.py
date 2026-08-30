"""
Migration: add_github_app_configs
Description: Create github_app_configs table (GitHub App credentials + installation state).
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


def upgrade(connection):
    if _table_exists(connection, "github_app_configs"):
        return

    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE github_app_configs (
                id INTEGER NOT NULL AUTO_INCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100),
                app_id VARCHAR(64),
                slug VARCHAR(255),
                installation_id VARCHAR(64),
                account_login VARCHAR(255),
                private_key_encrypted TEXT,
                webhook_secret_encrypted VARCHAR(2000),
                installed BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    else:
        connection.execute(db.text("""
            CREATE TABLE github_app_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100),
                app_id VARCHAR(64),
                slug VARCHAR(255),
                installation_id VARCHAR(64),
                account_login VARCHAR(255),
                private_key_encrypted TEXT,
                webhook_secret_encrypted VARCHAR(2000),
                installed BOOLEAN NOT NULL DEFAULT 0
            )
        """))
    print("Created github_app_configs table.")


def downgrade(connection):
    if _table_exists(connection, "github_app_configs"):
        connection.execute(db.text("DROP TABLE github_app_configs"))
        print("Dropped github_app_configs table.")


def migrate():
    try:
        print("Running migration: add_github_app_configs...")
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
