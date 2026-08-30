"""
Migration: add_project_repo_bindings
Description: Create project_repo_bindings table (P1.1 code plane: project <-> repo binding).
Created: 2026-08-31
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _table_exists(connection, table_name):
    """Check if a table exists (MySQL/PostgreSQL/SQLite compatible)."""
    dialect = connection.dialect.name
    if dialect == "mysql":
        return connection.execute(
            db.text("SHOW TABLES LIKE :table_name"), {"table_name": table_name}
        ).first() is not None
    elif dialect == "postgresql":
        return connection.execute(
            db.text("SELECT 1 FROM information_schema.tables WHERE table_name = :table_name"),
            {"table_name": table_name},
        ).first() is not None
    else:
        return connection.execute(
            db.text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table_name"),
            {"table_name": table_name},
        ).first() is not None


def upgrade(connection):
    """Create project_repo_bindings table."""
    if _table_exists(connection, "project_repo_bindings"):
        return

    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE project_repo_bindings (
                id INTEGER NOT NULL AUTO_INCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100),
                project_id INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL DEFAULT 'github',
                repo_owner VARCHAR(255) NOT NULL,
                repo_name VARCHAR(255) NOT NULL,
                default_branch VARCHAR(255) NOT NULL DEFAULT 'main',
                token_encrypted VARCHAR(2000),
                PRIMARY KEY (id),
                UNIQUE INDEX ix_project_repo_bindings_project_id (project_id),
                FOREIGN KEY(project_id) REFERENCES projects (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    elif dialect == "postgresql":
        connection.execute(db.text("""
            CREATE TABLE project_repo_bindings (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                created_by VARCHAR(100),
                project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id),
                provider VARCHAR(20) NOT NULL DEFAULT 'github',
                repo_owner VARCHAR(255) NOT NULL,
                repo_name VARCHAR(255) NOT NULL,
                default_branch VARCHAR(255) NOT NULL DEFAULT 'main',
                token_encrypted VARCHAR(2000)
            )
        """))
        connection.execute(db.text(
            "CREATE INDEX ix_project_repo_bindings_project_id ON project_repo_bindings(project_id)"
        ))
    else:
        connection.execute(db.text("""
            CREATE TABLE project_repo_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100),
                project_id INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL DEFAULT 'github',
                repo_owner VARCHAR(255) NOT NULL,
                repo_name VARCHAR(255) NOT NULL,
                default_branch VARCHAR(255) NOT NULL DEFAULT 'main',
                token_encrypted VARCHAR(2000),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
        """))
        connection.execute(db.text(
            "CREATE UNIQUE INDEX ix_project_repo_bindings_project_id ON project_repo_bindings(project_id)"
        ))
    print("Created project_repo_bindings table.")


def downgrade(connection):
    if _table_exists(connection, "project_repo_bindings"):
        connection.execute(db.text("DROP TABLE project_repo_bindings"))
        print("Dropped project_repo_bindings table.")


def migrate():
    """Run the migration."""
    try:
        print("Running migration: add_project_repo_bindings...")
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
