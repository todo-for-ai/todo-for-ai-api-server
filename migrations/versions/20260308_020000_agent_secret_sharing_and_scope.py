"""
Migration: agent_secret_sharing_and_scope
Description: add secret scope metadata and secret sharing table for multi-agent collaboration
Created: 2026-03-08T02:00:00
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


def _constraint_exists(connection, table_name, constraint_name):
    result = connection.execute(
        text(
            """
            SELECT COUNT(1) AS cnt
            FROM information_schema.table_constraints
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND constraint_name = :constraint_name
            """
        ),
        {"table_name": table_name, "constraint_name": constraint_name},
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


def _add_agent_secret_columns(connection):
    if not _column_exists(connection, 'agent_secrets', 'secret_type'):
        connection.execute(text("ALTER TABLE agent_secrets ADD COLUMN secret_type VARCHAR(32) NOT NULL DEFAULT 'api_key'"))

    if not _column_exists(connection, 'agent_secrets', 'scope_type'):
        connection.execute(text("ALTER TABLE agent_secrets ADD COLUMN scope_type VARCHAR(32) NOT NULL DEFAULT 'agent_private'"))

    if not _column_exists(connection, 'agent_secrets', 'project_id'):
        connection.execute(text("ALTER TABLE agent_secrets ADD COLUMN project_id INT NULL"))

    if not _column_exists(connection, 'agent_secrets', 'description'):
        connection.execute(text("ALTER TABLE agent_secrets ADD COLUMN description TEXT NULL"))

    if _column_exists(connection, 'agent_secrets', 'project_id') and not _constraint_exists(connection, 'agent_secrets', 'fk_agent_secrets_project'):
        connection.execute(text(
            "ALTER TABLE agent_secrets ADD CONSTRAINT fk_agent_secrets_project FOREIGN KEY (project_id) REFERENCES projects(id)"
        ))


def _create_agent_secret_shares(connection):
    if _table_exists(connection, 'agent_secret_shares'):
        return

    connection.execute(text("""
        CREATE TABLE agent_secret_shares (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            secret_id INT NOT NULL,
            workspace_id INT NOT NULL,
            owner_agent_id INT NOT NULL,
            target_agent_id INT NOT NULL,
            access_mode VARCHAR(32) NOT NULL DEFAULT 'read',
            expires_at DATETIME NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            granted_reason TEXT NULL,
            granted_by_user_id INT NOT NULL,
            revoked_by_user_id INT NULL,
            CONSTRAINT fk_agent_secret_shares_secret FOREIGN KEY (secret_id) REFERENCES agent_secrets(id),
            CONSTRAINT fk_agent_secret_shares_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_secret_shares_owner_agent FOREIGN KEY (owner_agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_secret_shares_target_agent FOREIGN KEY (target_agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_secret_shares_granted_by FOREIGN KEY (granted_by_user_id) REFERENCES users(id),
            CONSTRAINT fk_agent_secret_shares_revoked_by FOREIGN KEY (revoked_by_user_id) REFERENCES users(id)
        )
    """))


def upgrade(connection):
    _add_agent_secret_columns(connection)
    _create_agent_secret_shares(connection)

    _create_index_if_missing(
        connection,
        'agent_secrets',
        'idx_agent_secrets_scope_project',
        'CREATE INDEX idx_agent_secrets_scope_project ON agent_secrets (scope_type, project_id)'
    )
    _create_index_if_missing(
        connection,
        'agent_secrets',
        'idx_agent_secrets_secret_type',
        'CREATE INDEX idx_agent_secrets_secret_type ON agent_secrets (secret_type)'
    )
    _create_index_if_missing(
        connection,
        'agent_secret_shares',
        'idx_agent_secret_shares_owner_secret_active',
        'CREATE INDEX idx_agent_secret_shares_owner_secret_active ON agent_secret_shares (owner_agent_id, secret_id, is_active)'
    )
    _create_index_if_missing(
        connection,
        'agent_secret_shares',
        'idx_agent_secret_shares_target_active_expires',
        'CREATE INDEX idx_agent_secret_shares_target_active_expires ON agent_secret_shares (target_agent_id, is_active, expires_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_secret_shares',
        'idx_agent_secret_shares_workspace_secret',
        'CREATE INDEX idx_agent_secret_shares_workspace_secret ON agent_secret_shares (workspace_id, secret_id)'
    )


def downgrade(connection):
    _drop_index_if_exists(connection, 'agent_secret_shares', 'idx_agent_secret_shares_workspace_secret')
    _drop_index_if_exists(connection, 'agent_secret_shares', 'idx_agent_secret_shares_target_active_expires')
    _drop_index_if_exists(connection, 'agent_secret_shares', 'idx_agent_secret_shares_owner_secret_active')
    _drop_index_if_exists(connection, 'agent_secrets', 'idx_agent_secrets_secret_type')
    _drop_index_if_exists(connection, 'agent_secrets', 'idx_agent_secrets_scope_project')

    if _table_exists(connection, 'agent_secret_shares'):
        connection.execute(text('DROP TABLE agent_secret_shares'))

    if _constraint_exists(connection, 'agent_secrets', 'fk_agent_secrets_project'):
        connection.execute(text('ALTER TABLE agent_secrets DROP FOREIGN KEY fk_agent_secrets_project'))

    if _column_exists(connection, 'agent_secrets', 'description'):
        connection.execute(text('ALTER TABLE agent_secrets DROP COLUMN description'))
    if _column_exists(connection, 'agent_secrets', 'project_id'):
        connection.execute(text('ALTER TABLE agent_secrets DROP COLUMN project_id'))
    if _column_exists(connection, 'agent_secrets', 'scope_type'):
        connection.execute(text('ALTER TABLE agent_secrets DROP COLUMN scope_type'))
    if _column_exists(connection, 'agent_secrets', 'secret_type'):
        connection.execute(text('ALTER TABLE agent_secrets DROP COLUMN secret_type'))
