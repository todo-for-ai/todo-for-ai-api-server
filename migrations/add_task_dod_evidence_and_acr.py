"""
Migration: add_task_dod_evidence_and_acr
Description: Add tasks.dod / tasks.human_intervention_count columns and task_evidences table.
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
        result = connection.execute(
            db.text(f"PRAGMA table_info({table_name})")
        ).fetchall()
        return any(row[1] == column_name for row in result)


def upgrade(connection):
    """Add tasks.dod, tasks.human_intervention_count and task_evidences table."""
    # 1. tasks.dod
    if _table_exists(connection, "tasks") and not _column_exists(connection, "tasks", "dod"):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN dod JSON COMMENT 'Definition of Done (JSON array)'"
            ))
        elif dialect == "postgresql":
            connection.execute(db.text("ALTER TABLE tasks ADD COLUMN dod JSON"))
        else:
            connection.execute(db.text("ALTER TABLE tasks ADD COLUMN dod TEXT"))
        print("Added tasks.dod column.")

    # 2. tasks.human_intervention_count
    if _table_exists(connection, "tasks") and not _column_exists(connection, "tasks", "human_intervention_count"):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN human_intervention_count INTEGER NOT NULL DEFAULT 0 "
                "COMMENT 'Human intervention count for ACR metric'"
            ))
        elif dialect == "postgresql":
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN human_intervention_count INTEGER NOT NULL DEFAULT 0"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE tasks ADD COLUMN human_intervention_count INTEGER NOT NULL DEFAULT 0"
            ))
        print("Added tasks.human_intervention_count column.")

    # 3. task_evidences table
    if not _table_exists(connection, "task_evidences"):
        dialect = connection.dialect.name
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE task_evidences (
                    id INTEGER NOT NULL AUTO_INCREMENT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    created_by VARCHAR(100),
                    task_id BIGINT NOT NULL,
                    attempt_id VARCHAR(64),
                    agent_id INTEGER,
                    evidence_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    summary VARCHAR(500),
                    detail JSON,
                    url VARCHAR(1000),
                    verified_at DATETIME,
                    PRIMARY KEY (id),
                    INDEX ix_task_evidences_task_id (task_id),
                    INDEX ix_task_evidences_attempt_id (attempt_id),
                    INDEX ix_task_evidences_agent_id (agent_id),
                    FOREIGN KEY(task_id) REFERENCES tasks (id),
                    FOREIGN KEY(agent_id) REFERENCES agents (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        elif dialect == "postgresql":
            connection.execute(db.text("""
                CREATE TABLE task_evidences (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    created_by VARCHAR(100),
                    task_id BIGINT NOT NULL REFERENCES tasks(id),
                    attempt_id VARCHAR(64),
                    agent_id INTEGER REFERENCES agents(id),
                    evidence_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    summary VARCHAR(500),
                    detail JSON,
                    url VARCHAR(1000),
                    verified_at TIMESTAMP
                )
            """))
            connection.execute(db.text("CREATE INDEX ix_task_evidences_task_id ON task_evidences(task_id)"))
            connection.execute(db.text("CREATE INDEX ix_task_evidences_attempt_id ON task_evidences(attempt_id)"))
            connection.execute(db.text("CREATE INDEX ix_task_evidences_agent_id ON task_evidences(agent_id)"))
        else:
            connection.execute(db.text("""
                CREATE TABLE task_evidences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    created_by VARCHAR(100),
                    task_id BIGINT NOT NULL,
                    attempt_id VARCHAR(64),
                    agent_id INTEGER,
                    evidence_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    summary VARCHAR(500),
                    detail TEXT,
                    url VARCHAR(1000),
                    verified_at DATETIME,
                    FOREIGN KEY(task_id) REFERENCES tasks(id),
                    FOREIGN KEY(agent_id) REFERENCES agents(id)
                )
            """))
        print("Created task_evidences table.")


def downgrade(connection):
    """Remove task_evidences table and tasks.dod / human_intervention_count columns."""
    if _table_exists(connection, "task_evidences"):
        connection.execute(db.text("DROP TABLE task_evidences"))
        print("Dropped task_evidences table.")

    if _table_exists(connection, "tasks") and _column_exists(connection, "tasks", "human_intervention_count"):
        dialect = connection.dialect.name
        if dialect == "sqlite":
            print("SQLite: cannot drop column human_intervention_count from tasks (manual step required).")
        else:
            connection.execute(db.text("ALTER TABLE tasks DROP COLUMN human_intervention_count"))
            print("Dropped tasks.human_intervention_count column.")

    if _table_exists(connection, "tasks") and _column_exists(connection, "tasks", "dod"):
        dialect = connection.dialect.name
        if dialect == "sqlite":
            print("SQLite: cannot drop column dod from tasks (manual step required).")
        else:
            connection.execute(db.text("ALTER TABLE tasks DROP COLUMN dod"))
            print("Dropped tasks.dod column.")


def migrate():
    """Run the migration."""
    try:
        print("Running migration: add_task_dod_evidence_and_acr...")
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
