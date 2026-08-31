"""
Migration: add_external_connector_configs
Description: Phase 4 互操作写回侧 - external_connector_configs 表（Linear/GitLab/Jira 连接器凭据与映射）。
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


def upgrade(connection):
    dialect = connection.dialect.name

    if _table_exists(connection, "external_connector_configs"):
        return

    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE external_connector_configs (
                id INTEGER NOT NULL AUTO_INCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                workspace_id INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                secret_encrypted VARCHAR(2000),
                default_project_id INTEGER,
                last_synced_at DATETIME,
                PRIMARY KEY (id),
                UNIQUE KEY uq_connector_workspace_provider (workspace_id, provider),
                INDEX ix_ecc_workspace_id (workspace_id),
                INDEX ix_ecc_provider (provider),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id),
                FOREIGN KEY(default_project_id) REFERENCES projects (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    else:
        connection.execute(db.text("""
            CREATE TABLE external_connector_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                workspace_id INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                secret_encrypted VARCHAR(2000),
                default_project_id INTEGER,
                last_synced_at DATETIME,
                UNIQUE (workspace_id, provider),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id),
                FOREIGN KEY(default_project_id) REFERENCES projects (id)
            )
        """))
    print("Created external_connector_configs table.")


def downgrade(connection):
    if _table_exists(connection, "external_connector_configs"):
        connection.execute(db.text("DROP TABLE external_connector_configs"))
        print("Dropped external_connector_configs table.")


def migrate():
    try:
        print("Running migration: add_external_connector_configs...")
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
