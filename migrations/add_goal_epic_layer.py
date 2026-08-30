"""
Migration: add_goal_epic_layer
Description: Create goals/epics tables and tasks.epic_id column (P2.1 goal layer).
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

    if not _table_exists(connection, "goals"):
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE goals (
                    id INTEGER NOT NULL AUTO_INCREMENT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    created_by VARCHAR(100),
                    workspace_id INTEGER NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    description TEXT,
                    metrics JSON,
                    status ENUM('DRAFT','ACTIVE','PAUSED','ACHIEVED','ARCHIVED') NOT NULL DEFAULT 'DRAFT',
                    owner_id INTEGER,
                    due_date DATETIME,
                    PRIMARY KEY (id),
                    INDEX ix_goals_workspace_id (workspace_id),
                    INDEX ix_goals_owner_id (owner_id),
                    FOREIGN KEY(workspace_id) REFERENCES organizations (id),
                    FOREIGN KEY(owner_id) REFERENCES users (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        else:
            connection.execute(db.text("""
                CREATE TABLE goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME, updated_at DATETIME, created_by VARCHAR(100),
                    workspace_id INTEGER NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    description TEXT,
                    metrics TEXT,
                    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
                    owner_id INTEGER,
                    due_date DATETIME,
                    FOREIGN KEY(workspace_id) REFERENCES organizations(id),
                    FOREIGN KEY(owner_id) REFERENCES users(id)
                )
            """))
        print("Created goals table.")

    if not _table_exists(connection, "epics"):
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE epics (
                    id INTEGER NOT NULL AUTO_INCREMENT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    created_by VARCHAR(100),
                    goal_id INTEGER NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    description TEXT,
                    status ENUM('PROPOSED','ACCEPTED','IN_PROGRESS','DONE','DROPPED') NOT NULL DEFAULT 'PROPOSED',
                    order_index INTEGER NOT NULL DEFAULT 0,
                    agent_proposed BOOLEAN NOT NULL DEFAULT 0,
                    proposed_by_agent_id INTEGER,
                    decided_by_user_id INTEGER,
                    decided_at DATETIME,
                    PRIMARY KEY (id),
                    INDEX ix_epics_goal_id (goal_id),
                    INDEX ix_epics_proposed_by_agent_id (proposed_by_agent_id),
                    FOREIGN KEY(goal_id) REFERENCES goals (id),
                    FOREIGN KEY(proposed_by_agent_id) REFERENCES agents (id),
                    FOREIGN KEY(decided_by_user_id) REFERENCES users (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        else:
            connection.execute(db.text("""
                CREATE TABLE epics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME, updated_at DATETIME, created_by VARCHAR(100),
                    goal_id INTEGER NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    description TEXT,
                    status VARCHAR(20) NOT NULL DEFAULT 'PROPOSED',
                    order_index INTEGER NOT NULL DEFAULT 0,
                    agent_proposed BOOLEAN NOT NULL DEFAULT 0,
                    proposed_by_agent_id INTEGER,
                    decided_by_user_id INTEGER,
                    decided_at DATETIME,
                    FOREIGN KEY(goal_id) REFERENCES goals(id),
                    FOREIGN KEY(proposed_by_agent_id) REFERENCES agents(id),
                    FOREIGN KEY(decided_by_user_id) REFERENCES users(id)
                )
            """))
        print("Created epics table.")

    if _table_exists(connection, "tasks") and not _column_exists(connection, "tasks", "epic_id"):
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN epic_id INTEGER COMMENT 'Belonging epic (goal layer task graph)', "
                "ADD INDEX ix_tasks_epic_id (epic_id), "
                "ADD CONSTRAINT fk_tasks_epic FOREIGN KEY (epic_id) REFERENCES epics (id)"
            ))
        else:
            connection.execute(db.text("ALTER TABLE tasks ADD COLUMN epic_id INTEGER"))
            connection.execute(db.text("CREATE INDEX ix_tasks_epic_id ON tasks(epic_id)"))
        print("Added tasks.epic_id column.")


def downgrade(connection):
    if _table_exists(connection, "tasks") and _column_exists(connection, "tasks", "epic_id"):
        dialect = connection.dialect.name
        if dialect == "sqlite":
            print("SQLite: cannot drop column epic_id (manual step required).")
        else:
            connection.execute(db.text("ALTER TABLE tasks DROP COLUMN epic_id"))
            print("Dropped tasks.epic_id column.")

    for table in ("epics", "goals"):
        if _table_exists(connection, table):
            connection.execute(db.text(f"DROP TABLE {table}"))
            print(f"Dropped {table} table.")


def migrate():
    try:
        print("Running migration: add_goal_epic_layer...")
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
