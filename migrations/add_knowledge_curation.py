"""
Migration: add_knowledge_curation
Description: P3.2 项目知识库自动策展 - project_knowledge_proposals 表（提案 → 人工确认 → 项目知识条目）。
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
    dialect = connection.dialect.name

    if _table_exists(connection, "project_knowledge_proposals"):
        return

    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE project_knowledge_proposals (
                id INTEGER NOT NULL AUTO_INCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                project_id INTEGER NOT NULL,
                workspace_id INTEGER,
                proposal_type VARCHAR(50) NOT NULL DEFAULT 'failure_lesson',
                title VARCHAR(500) NOT NULL,
                content TEXT NOT NULL,
                source_type VARCHAR(50) NOT NULL DEFAULT 'failure_attribution',
                source_ref JSON,
                status VARCHAR(20) NOT NULL DEFAULT 'proposed',
                dedupe_key VARCHAR(128),
                proposed_by_agent_id INTEGER,
                decided_by_user_id INTEGER,
                decided_at DATETIME,
                knowledge_entry_id INTEGER,
                dismissal_reason VARCHAR(500),
                PRIMARY KEY (id),
                UNIQUE KEY uq_knowledge_proposal_project_dedupe (project_id, dedupe_key),
                INDEX ix_kkp_project_id (project_id),
                INDEX ix_kkp_workspace_id (workspace_id),
                INDEX ix_kkp_status (status),
                INDEX ix_kkp_dedupe_key (dedupe_key),
                FOREIGN KEY(project_id) REFERENCES projects (id),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id),
                FOREIGN KEY(proposed_by_agent_id) REFERENCES agents (id),
                FOREIGN KEY(decided_by_user_id) REFERENCES users (id),
                FOREIGN KEY(knowledge_entry_id) REFERENCES knowledge_entries (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    else:
        connection.execute(db.text("""
            CREATE TABLE project_knowledge_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                project_id INTEGER NOT NULL,
                workspace_id INTEGER,
                proposal_type VARCHAR(50) NOT NULL DEFAULT 'failure_lesson',
                title VARCHAR(500) NOT NULL,
                content TEXT NOT NULL,
                source_type VARCHAR(50) NOT NULL DEFAULT 'failure_attribution',
                source_ref TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'proposed',
                dedupe_key VARCHAR(128),
                proposed_by_agent_id INTEGER,
                decided_by_user_id INTEGER,
                decided_at DATETIME,
                knowledge_entry_id INTEGER,
                dismissal_reason VARCHAR(500),
                CONSTRAINT uq_knowledge_proposal_project_dedupe UNIQUE (project_id, dedupe_key),
                FOREIGN KEY(project_id) REFERENCES projects (id),
                FOREIGN KEY(workspace_id) REFERENCES organizations (id),
                FOREIGN KEY(proposed_by_agent_id) REFERENCES agents (id),
                FOREIGN KEY(decided_by_user_id) REFERENCES users (id),
                FOREIGN KEY(knowledge_entry_id) REFERENCES knowledge_entries (id)
            )
        """))
    print("Created project_knowledge_proposals table.")


def downgrade(connection):
    if _table_exists(connection, "project_knowledge_proposals"):
        connection.execute(db.text("DROP TABLE project_knowledge_proposals"))
        print("Dropped project_knowledge_proposals table.")


def migrate():
    try:
        print("Running migration: add_knowledge_curation...")
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
