"""
Migration: add_agent_identity_tables
Description: add agent identity, key, session, connect link and audit tables
Created: 2026-03-05T12:00:00
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
        print(f"Index already exists, skip: {index_name}")
        return
    connection.execute(text(ddl))
    print(f"Created index: {index_name}")


def _drop_index_if_exists(connection, table_name, index_name):
    if not _index_exists(connection, table_name, index_name):
        print(f"Index not found, skip drop: {index_name}")
        return
    connection.execute(text(f"DROP INDEX {index_name} ON {table_name}"))
    print(f"Dropped index: {index_name}")


def _create_agents(connection):
    if _table_exists(connection, 'agents'):
        print('Table already exists: agents')
        return
    connection.execute(text("""
        CREATE TABLE agents (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            workspace_id INT NOT NULL,
            creator_user_id INT NOT NULL,
            name VARCHAR(128) NOT NULL,
            description TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            capability_tags JSON,
            allowed_project_ids JSON,
            CONSTRAINT fk_agents_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agents_creator FOREIGN KEY (creator_user_id) REFERENCES users(id)
        )
    """))
    print('Created table: agents')


def _create_agent_keys(connection):
    if _table_exists(connection, 'agent_keys'):
        print('Table already exists: agent_keys')
        return
    connection.execute(text("""
        CREATE TABLE agent_keys (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            created_by_user_id INT NOT NULL,
            name VARCHAR(128) NOT NULL,
            prefix VARCHAR(16) NOT NULL,
            key_hash VARCHAR(64) NOT NULL UNIQUE,
            key_encrypted TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            revoked_at DATETIME NULL,
            last_used_at DATETIME NULL,
            usage_count BIGINT NOT NULL DEFAULT 0,
            CONSTRAINT fk_agent_keys_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_keys_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_keys_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id)
        )
    """))
    print('Created table: agent_keys')


def _create_agent_sessions(connection):
    if _table_exists(connection, 'agent_sessions'):
        print('Table already exists: agent_sessions')
        return
    connection.execute(text("""
        CREATE TABLE agent_sessions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            token_prefix VARCHAR(16) NOT NULL,
            expires_at DATETIME NOT NULL,
            revoked_at DATETIME NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT fk_agent_sessions_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_sessions_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))
    print('Created table: agent_sessions')


def _create_links_and_audit(connection):
    if not _table_exists(connection, 'agent_connect_links'):
        connection.execute(text("""
            CREATE TABLE agent_connect_links (
                id INT AUTO_INCREMENT PRIMARY KEY,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                created_by VARCHAR(100),
                workspace_id INT NOT NULL,
                agent_id INT NOT NULL,
                key_id INT NOT NULL,
                created_by_user_id INT NOT NULL,
                url TEXT NOT NULL,
                signature VARCHAR(128) NOT NULL,
                expires_at DATETIME NOT NULL,
                CONSTRAINT fk_connect_links_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
                CONSTRAINT fk_connect_links_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
                CONSTRAINT fk_connect_links_key FOREIGN KEY (key_id) REFERENCES agent_keys(id),
                CONSTRAINT fk_connect_links_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id)
            )
        """))
        print('Created table: agent_connect_links')

    if not _table_exists(connection, 'agent_audit_events'):
        connection.execute(text("""
            CREATE TABLE agent_audit_events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                created_by VARCHAR(100),
                workspace_id INT NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                actor_type VARCHAR(32) NOT NULL,
                actor_id VARCHAR(64) NOT NULL,
                target_type VARCHAR(32) NOT NULL,
                target_id VARCHAR(64) NOT NULL,
                risk_score INT NOT NULL DEFAULT 0,
                payload JSON,
                ip VARCHAR(64),
                user_agent VARCHAR(512),
                occurred_at DATETIME NOT NULL
            )
        """))
        print('Created table: agent_audit_events')


def _create_indexes(connection):
    _create_index_if_missing(connection, 'agents', 'idx_agents_workspace_status', 'CREATE INDEX idx_agents_workspace_status ON agents (workspace_id, status)')
    _create_index_if_missing(connection, 'agent_keys', 'idx_agent_keys_agent_active', 'CREATE INDEX idx_agent_keys_agent_active ON agent_keys (agent_id, is_active, revoked_at)')
    _create_index_if_missing(connection, 'agent_sessions', 'idx_agent_sessions_agent_active', 'CREATE INDEX idx_agent_sessions_agent_active ON agent_sessions (agent_id, is_active, expires_at)')
    _create_index_if_missing(connection, 'agent_audit_events', 'idx_agent_audit_workspace_event_time', 'CREATE INDEX idx_agent_audit_workspace_event_time ON agent_audit_events (workspace_id, event_type, occurred_at)')


def upgrade(connection):
    _create_agents(connection)
    _create_agent_keys(connection)
    _create_agent_sessions(connection)
    _create_links_and_audit(connection)
    _create_indexes(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_event_time')
    _drop_index_if_exists(connection, 'agent_sessions', 'idx_agent_sessions_agent_active')
    _drop_index_if_exists(connection, 'agent_keys', 'idx_agent_keys_agent_active')
    _drop_index_if_exists(connection, 'agents', 'idx_agents_workspace_status')

    for table in ['agent_audit_events', 'agent_connect_links', 'agent_sessions', 'agent_keys', 'agents']:
        if _table_exists(connection, table):
            connection.execute(text(f"DROP TABLE {table}"))
            print(f"Dropped table: {table}")
