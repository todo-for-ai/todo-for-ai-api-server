"""
Migration: add_agent_secret_grants
Description: add short-lived grant table for agent secret capability delegation
Created: 2026-03-21T23:51:00
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


def _create_table(connection):
    if _table_exists(connection, 'agent_secret_grants'):
        return

    connection.execute(
        text(
            """
            CREATE TABLE agent_secret_grants (
                id INT AUTO_INCREMENT PRIMARY KEY,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                created_by VARCHAR(100) NULL,

                grant_id VARCHAR(64) NOT NULL,
                secret_id INT NOT NULL,
                workspace_id INT NOT NULL,
                from_agent_id INT NOT NULL,
                to_agent_id INT NOT NULL,

                chain_id INT NULL,
                task_id BIGINT NULL,
                attempt_id VARCHAR(64) NULL,

                grant_mode VARCHAR(32) NOT NULL DEFAULT 'ephemeral',
                max_uses INT NULL,
                used_count INT NOT NULL DEFAULT 0,
                expires_at DATETIME NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                granted_reason TEXT NULL,
                last_used_at DATETIME NULL,

                granted_by_user_id INT NULL,
                revoked_by_user_id INT NULL,
                revoked_by_agent_id INT NULL,

                CONSTRAINT uq_agent_secret_grants_grant_id UNIQUE (grant_id),
                CONSTRAINT fk_agent_secret_grants_secret FOREIGN KEY (secret_id) REFERENCES agent_secrets(id),
                CONSTRAINT fk_agent_secret_grants_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
                CONSTRAINT fk_agent_secret_grants_from_agent FOREIGN KEY (from_agent_id) REFERENCES agents(id),
                CONSTRAINT fk_agent_secret_grants_to_agent FOREIGN KEY (to_agent_id) REFERENCES agents(id),
                CONSTRAINT fk_agent_secret_grants_task FOREIGN KEY (task_id) REFERENCES tasks(id),
                CONSTRAINT fk_agent_secret_grants_granted_by_user FOREIGN KEY (granted_by_user_id) REFERENCES users(id),
                CONSTRAINT fk_agent_secret_grants_revoked_by_user FOREIGN KEY (revoked_by_user_id) REFERENCES users(id),
                CONSTRAINT fk_agent_secret_grants_revoked_by_agent FOREIGN KEY (revoked_by_agent_id) REFERENCES agents(id)
            )
            """
        )
    )


def _create_indexes(connection):
    _create_index_if_missing(
        connection,
        'agent_secret_grants',
        'idx_agent_secret_grants_workspace_to_status_expires',
        'CREATE INDEX idx_agent_secret_grants_workspace_to_status_expires ON agent_secret_grants (workspace_id, to_agent_id, status, expires_at)',
    )
    _create_index_if_missing(
        connection,
        'agent_secret_grants',
        'idx_agent_secret_grants_workspace_from_status',
        'CREATE INDEX idx_agent_secret_grants_workspace_from_status ON agent_secret_grants (workspace_id, from_agent_id, status)',
    )
    _create_index_if_missing(
        connection,
        'agent_secret_grants',
        'idx_agent_secret_grants_secret_status',
        'CREATE INDEX idx_agent_secret_grants_secret_status ON agent_secret_grants (secret_id, status)',
    )
    _create_index_if_missing(
        connection,
        'agent_secret_grants',
        'idx_agent_secret_grants_task_attempt',
        'CREATE INDEX idx_agent_secret_grants_task_attempt ON agent_secret_grants (task_id, attempt_id, status)',
    )


def upgrade(connection):
    _create_table(connection)
    _create_indexes(connection)


def downgrade(connection):
    if not _table_exists(connection, 'agent_secret_grants'):
        return

    _drop_index_if_exists(connection, 'agent_secret_grants', 'idx_agent_secret_grants_task_attempt')
    _drop_index_if_exists(connection, 'agent_secret_grants', 'idx_agent_secret_grants_secret_status')
    _drop_index_if_exists(connection, 'agent_secret_grants', 'idx_agent_secret_grants_workspace_from_status')
    _drop_index_if_exists(connection, 'agent_secret_grants', 'idx_agent_secret_grants_workspace_to_status_expires')
    connection.execute(text('DROP TABLE agent_secret_grants'))

