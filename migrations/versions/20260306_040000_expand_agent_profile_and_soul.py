"""
Migration: expand_agent_profile_and_soul
Description: add advanced agent profile fields, soul versions and secrets tables
Created: 2026-03-06T04:00:00
"""

from sqlalchemy import text


def _table_exists(connection, table_name):
    result = connection.execute(
        text(
            """
            SELECT COUNT(1) AS cnt
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    ).scalar()
    return bool(result)


def _column_exists(connection, table_name, column_name):
    result = connection.execute(
        text(
            """
            SELECT COUNT(1) AS cnt
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar()
    return bool(result)


def _index_exists(connection, table_name, index_name):
    result = connection.execute(
        text(
            """
            SELECT COUNT(1) AS cnt
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND index_name = :index_name
            """
        ),
        {"table_name": table_name, "index_name": index_name},
    ).scalar()
    return bool(result)


def _create_index_if_missing(connection, table_name, index_name, ddl):
    if _index_exists(connection, table_name, index_name):
        return
    connection.execute(text(ddl))


def _drop_index_if_exists(connection, table_name, index_name):
    if not _index_exists(connection, table_name, index_name):
        return
    connection.execute(text(f"DROP INDEX {index_name} ON {table_name}"))


def _add_agent_columns(connection):
    columns = [
        ('display_name', 'ALTER TABLE agents ADD COLUMN display_name VARCHAR(128) NULL'),
        ('avatar_url', 'ALTER TABLE agents ADD COLUMN avatar_url VARCHAR(512) NULL'),
        ('homepage_url', 'ALTER TABLE agents ADD COLUMN homepage_url VARCHAR(512) NULL'),
        ('contact_email', 'ALTER TABLE agents ADD COLUMN contact_email VARCHAR(255) NULL'),
        ('llm_provider', 'ALTER TABLE agents ADD COLUMN llm_provider VARCHAR(64) NULL'),
        ('llm_model', 'ALTER TABLE agents ADD COLUMN llm_model VARCHAR(128) NULL'),
        ('temperature', 'ALTER TABLE agents ADD COLUMN temperature DECIMAL(4,3) NULL DEFAULT 0.700'),
        ('top_p', 'ALTER TABLE agents ADD COLUMN top_p DECIMAL(4,3) NULL DEFAULT 1.000'),
        ('max_output_tokens', 'ALTER TABLE agents ADD COLUMN max_output_tokens INT NULL'),
        ('context_window_tokens', 'ALTER TABLE agents ADD COLUMN context_window_tokens INT NULL'),
        ('reasoning_mode', 'ALTER TABLE agents ADD COLUMN reasoning_mode VARCHAR(32) NULL DEFAULT "balanced"'),
        ('system_prompt', 'ALTER TABLE agents ADD COLUMN system_prompt TEXT NULL'),
        ('soul_markdown', 'ALTER TABLE agents ADD COLUMN soul_markdown TEXT NULL'),
        ('response_style', 'ALTER TABLE agents ADD COLUMN response_style JSON NULL'),
        ('tool_policy', 'ALTER TABLE agents ADD COLUMN tool_policy JSON NULL'),
        ('memory_policy', 'ALTER TABLE agents ADD COLUMN memory_policy JSON NULL'),
        ('handoff_policy', 'ALTER TABLE agents ADD COLUMN handoff_policy JSON NULL'),
        ('max_concurrency', 'ALTER TABLE agents ADD COLUMN max_concurrency INT NOT NULL DEFAULT 1'),
        ('max_retry', 'ALTER TABLE agents ADD COLUMN max_retry INT NOT NULL DEFAULT 2'),
        ('timeout_seconds', 'ALTER TABLE agents ADD COLUMN timeout_seconds INT NOT NULL DEFAULT 1800'),
        ('heartbeat_interval_seconds', 'ALTER TABLE agents ADD COLUMN heartbeat_interval_seconds INT NOT NULL DEFAULT 20'),
        ('soul_version', 'ALTER TABLE agents ADD COLUMN soul_version INT NOT NULL DEFAULT 1'),
        ('config_version', 'ALTER TABLE agents ADD COLUMN config_version INT NOT NULL DEFAULT 1'),
    ]
    for column_name, ddl in columns:
        if not _column_exists(connection, 'agents', column_name):
            connection.execute(text(ddl))


def _create_soul_versions(connection):
    if _table_exists(connection, 'agent_soul_versions'):
        return

    connection.execute(text("""
        CREATE TABLE agent_soul_versions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            version INT NOT NULL,
            soul_markdown TEXT NOT NULL,
            change_summary VARCHAR(255),
            edited_by_user_id INT NOT NULL,
            CONSTRAINT uq_agent_soul_version_agent_ver UNIQUE (agent_id, version),
            CONSTRAINT fk_agent_soul_versions_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_soul_versions_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_soul_versions_editor FOREIGN KEY (edited_by_user_id) REFERENCES users(id)
        )
    """))


def _create_agent_secrets(connection):
    if _table_exists(connection, 'agent_secrets'):
        return

    connection.execute(text("""
        CREATE TABLE agent_secrets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            name VARCHAR(128) NOT NULL,
            secret_hash VARCHAR(64) NOT NULL,
            secret_encrypted TEXT NOT NULL,
            prefix VARCHAR(12) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            last_used_at DATETIME NULL,
            usage_count BIGINT NOT NULL DEFAULT 0,
            created_by_user_id INT NOT NULL,
            updated_by_user_id INT NOT NULL,
            CONSTRAINT fk_agent_secrets_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_secrets_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_secrets_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id),
            CONSTRAINT fk_agent_secrets_updater FOREIGN KEY (updated_by_user_id) REFERENCES users(id)
        )
    """))


def upgrade(connection):
    _add_agent_columns(connection)
    _create_soul_versions(connection)
    _create_agent_secrets(connection)

    _create_index_if_missing(connection, 'agent_soul_versions', 'idx_agent_soul_versions_agent_created', 'CREATE INDEX idx_agent_soul_versions_agent_created ON agent_soul_versions (agent_id, created_at)')
    _create_index_if_missing(connection, 'agent_secrets', 'idx_agent_secrets_agent_name_active', 'CREATE INDEX idx_agent_secrets_agent_name_active ON agent_secrets (agent_id, name, is_active)')


def downgrade(connection):
    _drop_index_if_exists(connection, 'agent_secrets', 'idx_agent_secrets_agent_name_active')
    _drop_index_if_exists(connection, 'agent_soul_versions', 'idx_agent_soul_versions_agent_created')

    if _table_exists(connection, 'agent_secrets'):
        connection.execute(text('DROP TABLE agent_secrets'))

    if _table_exists(connection, 'agent_soul_versions'):
        connection.execute(text('DROP TABLE agent_soul_versions'))

    drop_columns = [
        'config_version', 'soul_version', 'heartbeat_interval_seconds', 'timeout_seconds',
        'max_retry', 'max_concurrency', 'handoff_policy', 'memory_policy', 'tool_policy',
        'response_style', 'soul_markdown', 'system_prompt', 'reasoning_mode', 'context_window_tokens',
        'max_output_tokens', 'top_p', 'temperature', 'llm_model', 'llm_provider', 'contact_email',
        'homepage_url', 'avatar_url', 'display_name',
    ]
    for column_name in drop_columns:
        if _column_exists(connection, 'agents', column_name):
            connection.execute(text(f'ALTER TABLE agents DROP COLUMN {column_name}'))
