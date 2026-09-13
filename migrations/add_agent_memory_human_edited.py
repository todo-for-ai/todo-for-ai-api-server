"""
Migration: add_agent_memory_human_edited
Description: agent_memories 增加 human_edited 列——标记被人工创建/编辑
             过的记忆（用户开放编辑后，召回排序加权 + 治理审计）。
Created: 2026-09-13
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
            ),
            {"t": table_name, "c": column_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text(f"PRAGMA table_info({table_name})")
    ).fetchall()
    return any(row[1] == column_name for row in rows)


def upgrade(connection):
    if _column_exists(connection, "agent_memories", "human_edited"):
        print("⏭️  列 agent_memories.human_edited 已存在，跳过")
        return
    dialect = connection.dialect.name
    print("➕ 添加列 agent_memories.human_edited ...")
    if dialect == "mysql":
        connection.execute(db.text(
            "ALTER TABLE agent_memories ADD COLUMN human_edited INT NOT NULL DEFAULT 0 "
            "COMMENT '1=被人工创建/编辑过（召回排序加权 + 治理审计）'"
        ))
    else:
        connection.execute(db.text(
            "ALTER TABLE agent_memories ADD COLUMN human_edited INTEGER NOT NULL DEFAULT 0"
        ))
    print("✅ agent_memories.human_edited 就绪")


def downgrade(connection):
    if _column_exists(connection, "agent_memories", "human_edited"):
        connection.execute(db.text(
            "ALTER TABLE agent_memories DROP COLUMN human_edited"
        ))
