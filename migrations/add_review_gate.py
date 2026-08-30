"""
Migration: add_review_gate
Description: P2.4 reviewer gate - binding review config + orchestration role_assignments + review evidence type.
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

    if _table_exists(connection, "project_repo_bindings"):
        if not _column_exists(connection, "project_repo_bindings", "require_agent_review"):
            if dialect == "mysql":
                connection.execute(db.text(
                    "ALTER TABLE project_repo_bindings ADD COLUMN require_agent_review BOOLEAN NOT NULL DEFAULT 0 "
                    "COMMENT 'Require agent reviewer gate before merge'"
                ))
            else:
                connection.execute(db.text(
                    "ALTER TABLE project_repo_bindings ADD COLUMN require_agent_review BOOLEAN NOT NULL DEFAULT 0"
                ))
            print("Added project_repo_bindings.require_agent_review.")
        if not _column_exists(connection, "project_repo_bindings", "reviewer_agent_id"):
            if dialect == "mysql":
                connection.execute(db.text(
                    "ALTER TABLE project_repo_bindings ADD COLUMN reviewer_agent_id INTEGER "
                    "COMMENT 'Designated reviewer agent', "
                    "ADD CONSTRAINT fk_prb_reviewer FOREIGN KEY (reviewer_agent_id) REFERENCES agents (id)"
                ))
            else:
                connection.execute(db.text(
                    "ALTER TABLE project_repo_bindings ADD COLUMN reviewer_agent_id INTEGER"
                ))
            print("Added project_repo_bindings.reviewer_agent_id.")

    if _table_exists(connection, "team_task_orchestrations") and not _column_exists(
        connection, "team_task_orchestrations", "role_assignments"
    ):
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE team_task_orchestrations ADD COLUMN role_assignments JSON "
                "COMMENT 'Role -> Agent map (developer/reviewer/tester)'"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE team_task_orchestrations ADD COLUMN role_assignments TEXT"
            ))
        print("Added team_task_orchestrations.role_assignments.")

    # review 证据类型提示：TaskEvidenceRecord.TYPES 常量级扩展，无 schema 变更
    print("Review gate migration complete (review evidence type is constant-level).")


def downgrade(connection):
    dialect = connection.dialect.name
    if _table_exists(connection, "project_repo_bindings"):
        for col in ("reviewer_agent_id", "require_agent_review"):
            if _column_exists(connection, "project_repo_bindings", col):
                if dialect == "sqlite":
                    print(f"SQLite: cannot drop {col} (manual step).")
                else:
                    connection.execute(db.text(
                        f"ALTER TABLE project_repo_bindings DROP COLUMN {col}"
                    ))
                    print(f"Dropped project_repo_bindings.{col}.")
    if _table_exists(connection, "team_task_orchestrations") and _column_exists(
        connection, "team_task_orchestrations", "role_assignments"
    ):
        if dialect == "sqlite":
            print("SQLite: cannot drop role_assignments (manual step).")
        else:
            connection.execute(db.text(
                "ALTER TABLE team_task_orchestrations DROP COLUMN role_assignments"
            ))
            print("Dropped team_task_orchestrations.role_assignments.")


def migrate():
    try:
        print("Running migration: add_review_gate...")
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
