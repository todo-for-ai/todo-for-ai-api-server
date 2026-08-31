"""
Migration: add_memory_governance
Description: P3.4 记忆治理 - agent_soul_versions 增加记忆种类区分（memory_kind）与
             结构化快照列（snapshot_json），唯一约束放宽到 (agent_id, memory_kind, version)。
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


def _index_exists(connection, table_name, index_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text("SHOW INDEX FROM `%s` WHERE Key_name = :idx" % table_name),
            {"idx": index_name},
        ).first()
        return row is not None
    else:
        row = connection.execute(
            db.text("SELECT name FROM sqlite_master WHERE type='index' AND name = :idx"),
            {"idx": index_name},
        ).first()
        return row is not None


def upgrade(connection):
    dialect = connection.dialect.name

    if not _table_exists(connection, "agent_soul_versions"):
        return

    if not _column_exists(connection, "agent_soul_versions", "memory_kind"):
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions ADD COLUMN memory_kind VARCHAR(20) NOT NULL DEFAULT 'soul' "
                "COMMENT 'Memory kind: soul/skill_profile'"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions ADD COLUMN memory_kind VARCHAR(20) NOT NULL DEFAULT 'soul'"
            ))
        print("Added agent_soul_versions.memory_kind.")

    if not _column_exists(connection, "agent_soul_versions", "snapshot_json"):
        if dialect == "mysql":
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions ADD COLUMN snapshot_json TEXT "
                "COMMENT 'Structured snapshot (memory_kind=skill_profile)'"
            ))
        else:
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions ADD COLUMN snapshot_json TEXT"
            ))
        print("Added agent_soul_versions.snapshot_json.")

    # 唯一约束从 (agent_id, version) 放宽到 (agent_id, memory_kind, version)
    old_index = "uq_agent_soul_version_agent_ver"
    new_index = "uq_agent_memory_version"
    if dialect == "mysql":
        if _index_exists(connection, "agent_soul_versions", old_index):
            connection.execute(db.text(
                f"ALTER TABLE agent_soul_versions DROP INDEX {old_index}"
            ))
            print(f"Dropped {old_index}.")
        if not _index_exists(connection, "agent_soul_versions", new_index):
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions ADD UNIQUE KEY %s (agent_id, memory_kind, version)" % new_index
            ))
            print(f"Added {new_index}.")
    else:
        if _index_exists(connection, "agent_soul_versions", "uq_agent_soul_version_agent_ver"):
            print("SQLite: old unique constraint lives in table DDL; rebuild table manually if needed.")
        print("SQLite: new-model constraint uq_agent_memory_version applies to fresh create_all().")


def downgrade(connection):
    dialect = connection.dialect.name
    if _table_exists(connection, "agent_soul_versions"):
        for col in ("snapshot_json", "memory_kind"):
            if _column_exists(connection, "agent_soul_versions", col):
                if dialect == "sqlite":
                    print(f"SQLite: cannot drop {col} (manual step).")
                else:
                    connection.execute(db.text(
                        f"ALTER TABLE agent_soul_versions DROP COLUMN {col}"
                    ))
                    print(f"Dropped agent_soul_versions.{col}.")
        if dialect == "mysql" and _index_exists(connection, "agent_soul_versions", "uq_agent_memory_version"):
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions DROP INDEX uq_agent_memory_version"
            ))
            connection.execute(db.text(
                "ALTER TABLE agent_soul_versions ADD UNIQUE KEY uq_agent_soul_version_agent_ver (agent_id, version)"
            ))
            print("Restored uq_agent_soul_version_agent_ver.")


def migrate():
    try:
        print("Running migration: add_memory_governance...")
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
