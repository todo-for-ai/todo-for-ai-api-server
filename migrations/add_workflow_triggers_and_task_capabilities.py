"""
Migration: add_workflow_triggers_and_task_capabilities
Description: Add workflow_triggers table and tasks.required_capabilities column.
Created: 2026-06-29
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
        # SQLite
        return connection.execute(
            db.text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table_name"),
            {"table_name": table_name},
        ).first() is not None


def _column_exists(connection, table_name, column_name):
    """Check if a column exists in a table."""
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
        # SQLite
        result = connection.execute(
            db.text(f"PRAGMA table_info({table_name})")
        ).fetchall()
        return any(row[1] == column_name for row in result)


def upgrade(connection):
    """Add workflow_triggers table and tasks.required_capabilities column."""
    # 1. Create workflow_triggers table if not exists
    if not _table_exists(connection, "workflow_triggers"):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE workflow_triggers (
                    id INTEGER NOT NULL AUTO_INCREMENT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    workflow_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    cron_expr VARCHAR(100),
                    one_shot_at DATETIME,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    project_id INTEGER,
                    root_task_id BIGINT,
                    context_override JSON,
                    last_fired_at DATETIME,
                    next_fire_at DATETIME,
                    fire_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (id),
                    INDEX ix_workflow_triggers_workflow_id (workflow_id),
                    INDEX ix_workflow_triggers_owner_id (owner_id),
                    INDEX ix_workflow_triggers_is_active (is_active),
                    FOREIGN KEY(workflow_id) REFERENCES workflows (id),
                    FOREIGN KEY(owner_id) REFERENCES users (id),
                    FOREIGN KEY(project_id) REFERENCES projects (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        elif dialect == "postgresql":
            connection.execute(db.text("""
                CREATE TABLE workflow_triggers (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
                    owner_id INTEGER NOT NULL REFERENCES users(id),
                    name VARCHAR(200) NOT NULL,
                    cron_expr VARCHAR(100),
                    one_shot_at TIMESTAMP,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    project_id INTEGER REFERENCES projects(id),
                    root_task_id BIGINT,
                    context_override JSON,
                    last_fired_at TIMESTAMP,
                    next_fire_at TIMESTAMP,
                    fire_count INTEGER NOT NULL DEFAULT 0
                )
            """))
            connection.execute(db.text("CREATE INDEX ix_workflow_triggers_workflow_id ON workflow_triggers(workflow_id)"))
            connection.execute(db.text("CREATE INDEX ix_workflow_triggers_owner_id ON workflow_triggers(owner_id)"))
            connection.execute(db.text("CREATE INDEX ix_workflow_triggers_is_active ON workflow_triggers(is_active)"))
        else:
            # SQLite
            connection.execute(db.text("""
                CREATE TABLE workflow_triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    workflow_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    cron_expr VARCHAR(100),
                    one_shot_at DATETIME,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    project_id INTEGER,
                    root_task_id BIGINT,
                    context_override TEXT,
                    last_fired_at DATETIME,
                    next_fire_at DATETIME,
                    fire_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id),
                    FOREIGN KEY(owner_id) REFERENCES users(id),
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                )
            """))
        print("Created workflow_triggers table.")

    # 2. Add required_capabilities column to tasks if not exists
    if _table_exists(connection, "tasks") and not _column_exists(connection, "tasks", "required_capabilities"):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN required_capabilities JSON COMMENT 'Agent capability requirements (JSON array)'"
            ))
        elif dialect == "postgresql":
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN required_capabilities JSON"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN required_capabilities TEXT"
            ))
        print("Added required_capabilities column to tasks table.")


def downgrade(connection):
    """Remove workflow_triggers table and tasks.required_capabilities column."""
    if _table_exists(connection, "workflow_triggers"):
        connection.execute(db.text("DROP TABLE workflow_triggers"))
        print("Dropped workflow_triggers table.")

    if _table_exists(connection, "tasks") and _column_exists(connection, "tasks", "required_capabilities"):
        dialect = connection.dialect.name
        if dialect == "sqlite":
            # SQLite doesn't support DROP COLUMN easily; skip
            print("SQLite: cannot drop column required_capabilities from tasks (manual step required).")
        else:
            connection.execute(db.text("ALTER TABLE tasks DROP COLUMN required_capabilities"))
            print("Dropped required_capabilities column from tasks table.")


def migrate():
    """Run the migration."""
    try:
        print("Running migration: add_workflow_triggers_and_task_capabilities...")
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
