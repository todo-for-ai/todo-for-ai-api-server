"""
Migration: add_agent_memory
Description: 新建 agent_memories 表——多维度作用域记忆（organization/
             project/agent/user/session 五维度 + scope_id），每行强制
             organization_id 硬租户隔离；(org, scope, scope_id, dedupe_key)
             唯一索引做同作用域幂等去重。
Created: 2026-09-13
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _table_exists(connection, table_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table_name},
        ).first()
        return bool(row and row[0])
    row = connection.execute(
        db.text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name},
    ).first()
    return bool(row and row[0])


def upgrade(connection):
    if _table_exists(connection, "agent_memories"):
        print("⏭️  表 agent_memories 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 创建表 agent_memories ...")
    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE agent_memories (
                id INT AUTO_INCREMENT PRIMARY KEY,
                organization_id INT NOT NULL,
                scope_type VARCHAR(32) NOT NULL,
                scope_id INT NOT NULL,
                kind VARCHAR(32) NULL,
                title VARCHAR(500) NOT NULL,
                content TEXT NOT NULL,
                source_type VARCHAR(50) NULL,
                source_task_id INT NULL,
                agent_id INT NULL,
                confidence INT NULL,
                is_valid INT NOT NULL DEFAULT 1,
                access_count INT NOT NULL DEFAULT 0,
                last_accessed_at DATETIME NULL,
                expires_at DATETIME NULL,
                dedupe_key VARCHAR(64) NOT NULL,
                created_at DATETIME NULL,
                updated_at DATETIME NULL,
                created_by VARCHAR(100) NULL,
                CONSTRAINT fk_am_org FOREIGN KEY (organization_id) REFERENCES organizations (id),
                CONSTRAINT fk_am_task FOREIGN KEY (source_task_id) REFERENCES tasks (id),
                CONSTRAINT fk_am_agent FOREIGN KEY (agent_id) REFERENCES agents (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
    else:
        connection.execute(db.text("""
            CREATE TABLE agent_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                scope_type VARCHAR(32) NOT NULL,
                scope_id INTEGER NOT NULL,
                kind VARCHAR(32),
                title VARCHAR(500) NOT NULL,
                content TEXT NOT NULL,
                source_type VARCHAR(50),
                source_task_id INTEGER,
                agent_id INTEGER,
                confidence INTEGER,
                is_valid INTEGER NOT NULL DEFAULT 1,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at DATETIME,
                expires_at DATETIME,
                dedupe_key VARCHAR(64) NOT NULL,
                created_at DATETIME,
                updated_at DATETIME,
                created_by VARCHAR(100)
            )
        """))

    connection.execute(db.text(
        "CREATE INDEX ix_am_org ON agent_memories (organization_id)"
    ))
    connection.execute(db.text(
        "CREATE INDEX ix_am_scope ON agent_memories (scope_type)"
    ))
    connection.execute(db.text(
        "CREATE INDEX ix_am_scope_id ON agent_memories (scope_id)"
    ))
    connection.execute(db.text(
        "CREATE INDEX ix_am_valid ON agent_memories (is_valid)"
    ))
    connection.execute(db.text(
        "CREATE UNIQUE INDEX uq_am_scope_dedupe "
        "ON agent_memories (organization_id, scope_type, scope_id, dedupe_key)"
    ))
    print("✅ agent_memories 就绪")


def downgrade(connection):
    if _table_exists(connection, "agent_memories"):
        connection.execute(db.text("DROP TABLE agent_memories"))
