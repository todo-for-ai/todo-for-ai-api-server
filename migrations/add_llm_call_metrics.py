"""
Migration: add_llm_call_metrics
Description: LLM API 调用指标表 llm_call_metrics —— agent-runtime 每次
             引擎真实调用上报一条（耗时/状态/token 用量/成本/模型/端点），
             支撑用户级与组织级用量可观测。幂等键 call_id 防重复摄取。
Created: 2026-09-17
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


def _index_exists(connection, table_name, index_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
            ),
            {"t": table_name, "i": index_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text(f"PRAGMA index_list({table_name})")
    ).fetchall()
    return any(row[1] == index_name for row in rows)


def upgrade(connection):
    dialect = connection.dialect.name

    if not _table_exists(connection, "llm_call_metrics"):
        print("➕ 创建表 llm_call_metrics ...")
        if dialect == "mysql":
            connection.execute(db.text("""
                CREATE TABLE llm_call_metrics (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    call_id VARCHAR(36) NOT NULL,
                    workspace_id INT NULL,
                    agent_id INT NULL,
                    owner_user_id INT NULL,
                    task_id BIGINT NULL,
                    attempt_id VARCHAR(64) NULL,
                    engine VARCHAR(32) NOT NULL DEFAULT '',
                    model VARCHAR(128) NOT NULL DEFAULT '',
                    base_url VARCHAR(255) NOT NULL DEFAULT '',
                    status VARCHAR(16) NOT NULL DEFAULT 'success',
                    duration_ms INT NOT NULL DEFAULT 0,
                    input_tokens INT NULL,
                    output_tokens INT NULL,
                    total_tokens INT NULL,
                    cache_read_tokens INT NULL,
                    cost_usd DOUBLE NULL,
                    error_code VARCHAR(64) NULL,
                    error_message VARCHAR(512) NULL,
                    created_by VARCHAR(100) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_llm_call_metrics_call_id (call_id),
                    KEY idx_llm_call_metrics_ws_created (workspace_id, created_at),
                    KEY idx_llm_call_metrics_owner_created (owner_user_id, created_at),
                    KEY idx_llm_call_metrics_agent_created (agent_id, created_at),
                    CONSTRAINT fk_llm_call_metrics_ws FOREIGN KEY (workspace_id) REFERENCES organizations(id),
                    CONSTRAINT fk_llm_call_metrics_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
                    CONSTRAINT fk_llm_call_metrics_owner FOREIGN KEY (owner_user_id) REFERENCES users(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        else:
            connection.execute(db.text("""
                CREATE TABLE llm_call_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id VARCHAR(36) NOT NULL UNIQUE,
                    workspace_id INTEGER REFERENCES organizations(id),
                    agent_id INTEGER REFERENCES agents(id),
                    owner_user_id INTEGER REFERENCES users(id),
                    task_id BIGINT,
                    attempt_id VARCHAR(64),
                    engine VARCHAR(32) NOT NULL DEFAULT '',
                    model VARCHAR(128) NOT NULL DEFAULT '',
                    base_url VARCHAR(255) NOT NULL DEFAULT '',
                    status VARCHAR(16) NOT NULL DEFAULT 'success',
                    duration_ms INT NOT NULL DEFAULT 0,
                    input_tokens INT,
                    output_tokens INT,
                    total_tokens INT,
                    cache_read_tokens INT,
                    cost_usd REAL,
                    error_code VARCHAR(64),
                    error_message VARCHAR(512),
                    created_by VARCHAR(100),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """))
            for ddl in (
                "CREATE INDEX idx_llm_call_metrics_ws_created ON llm_call_metrics (workspace_id, created_at)",
                "CREATE INDEX idx_llm_call_metrics_owner_created ON llm_call_metrics (owner_user_id, created_at)",
                "CREATE INDEX idx_llm_call_metrics_agent_created ON llm_call_metrics (agent_id, created_at)",
            ):
                connection.execute(db.text(ddl))
    else:
        print("⏭️  表 llm_call_metrics 已存在，跳过")


def downgrade(connection):
    connection.execute(db.text("DROP TABLE IF EXISTS llm_call_metrics"))
    print("🗑️  已删除表 llm_call_metrics")
