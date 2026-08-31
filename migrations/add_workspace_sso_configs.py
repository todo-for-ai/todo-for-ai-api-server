"""
Migration: add_workspace_sso_configs
Description: Phase 4 企业能力 - workspace_sso_configs 表（OIDC/SAML 配置化单点登录）。
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

    if _table_exists(connection, "workspace_sso_configs"):
        return

    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE workspace_sso_configs (
                id INTEGER NOT NULL AUTO_INCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                workspace_id INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL DEFAULT 'oidc',
                enabled BOOLEAN NOT NULL DEFAULT 0,
                issuer VARCHAR(500),
                client_id VARCHAR(200),
                client_secret_encrypted VARCHAR(2000),
                authorize_url VARCHAR(500),
                token_url VARCHAR(500),
                userinfo_url VARCHAR(500),
                redirect_uri VARCHAR(500),
                idp_metadata_url VARCHAR(500),
                idp_entity_id VARCHAR(255),
                default_role VARCHAR(32) NOT NULL DEFAULT 'member',
                PRIMARY KEY (id),
                UNIQUE KEY uq_workspace_sso_workspace (workspace_id),
                INDEX ix_wssoc_workspace_id (workspace_id),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    else:
        connection.execute(db.text("""
            CREATE TABLE workspace_sso_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                workspace_id INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL DEFAULT 'oidc',
                enabled BOOLEAN NOT NULL DEFAULT 0,
                issuer VARCHAR(500),
                client_id VARCHAR(200),
                client_secret_encrypted VARCHAR(2000),
                authorize_url VARCHAR(500),
                token_url VARCHAR(500),
                userinfo_url VARCHAR(500),
                redirect_uri VARCHAR(500),
                idp_metadata_url VARCHAR(500),
                idp_entity_id VARCHAR(255),
                default_role VARCHAR(32) NOT NULL DEFAULT 'member',
                UNIQUE (workspace_id),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id)
            )
        """))
    print("Created workspace_sso_configs table.")


def downgrade(connection):
    if _table_exists(connection, "workspace_sso_configs"):
        connection.execute(db.text("DROP TABLE workspace_sso_configs"))
        print("Dropped workspace_sso_configs table.")


def migrate():
    try:
        print("Running migration: add_workspace_sso_configs...")
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
