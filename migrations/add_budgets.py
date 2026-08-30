"""
Migration: add_budgets
Description: Create budgets table for P2.6 budget/quota enforcement (agent/project/workspace scopes).
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
    if _table_exists(connection, "budgets"):
        return

    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE budgets (
                id INTEGER NOT NULL AUTO_INCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100),
                scope_type VARCHAR(20) NOT NULL,
                agent_id INTEGER,
                project_id INTEGER,
                workspace_id INTEGER NOT NULL,
                resource VARCHAR(30) NOT NULL,
                limit_value BIGINT NOT NULL,
                period VARCHAR(20) NOT NULL DEFAULT 'total',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                PRIMARY KEY (id),
                INDEX ix_budgets_agent_id (agent_id),
                INDEX ix_budgets_project_id (project_id),
                INDEX ix_budgets_workspace_id (workspace_id),
                UNIQUE KEY uq_budget_scope_resource_period (
                    scope_type, agent_id, project_id, workspace_id, resource, period
                ),
                FOREIGN KEY(agent_id) REFERENCES agents (id),
                FOREIGN KEY(project_id) REFERENCES projects (id),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    else:
        connection.execute(db.text("""
            CREATE TABLE budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100),
                scope_type VARCHAR(20) NOT NULL,
                agent_id INTEGER,
                project_id INTEGER,
                workspace_id INTEGER NOT NULL,
                resource VARCHAR(30) NOT NULL,
                limit_value BIGINT NOT NULL,
                period VARCHAR(20) NOT NULL DEFAULT 'total',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                FOREIGN KEY(agent_id) REFERENCES agents(id),
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(workspace_id) REFERENCES organizations(id),
                UNIQUE (scope_type, agent_id, project_id, workspace_id, resource, period)
            )
        """))
    print("Created budgets table.")


def downgrade(connection):
    if _table_exists(connection, "budgets"):
        connection.execute(db.text("DROP TABLE budgets"))
        print("Dropped budgets table.")


def migrate():
    try:
        print("Running migration: add_budgets...")
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
