"""
Migration: add_agent_runtime_tables
Description: add agent task attempts, leases, events and dedup tables
Created: 2026-03-05T12:10:00
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


def _create_attempts(connection):
    if _table_exists(connection, 'agent_task_attempts'):
        print('Table already exists: agent_task_attempts')
        return
    connection.execute(text("""
        CREATE TABLE agent_task_attempts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            attempt_id VARCHAR(64) NOT NULL UNIQUE,
            task_id BIGINT NOT NULL,
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            state VARCHAR(20) NOT NULL DEFAULT 'CREATED',
            lease_id VARCHAR(64) NOT NULL,
            started_at DATETIME NOT NULL,
            ended_at DATETIME NULL,
            failure_code VARCHAR(64),
            failure_reason TEXT,
            CONSTRAINT fk_attempts_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_attempts_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_attempts_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))
    print('Created table: agent_task_attempts')


def _create_leases(connection):
    if _table_exists(connection, 'agent_task_leases'):
        print('Table already exists: agent_task_leases')
        return
    connection.execute(text("""
        CREATE TABLE agent_task_leases (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            lease_id VARCHAR(64) NOT NULL UNIQUE,
            task_id BIGINT NOT NULL,
            attempt_id VARCHAR(64) NOT NULL,
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            expires_at DATETIME NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            version INT NOT NULL DEFAULT 1,
            CONSTRAINT uq_agent_task_leases_task_active UNIQUE (task_id, active),
            CONSTRAINT fk_leases_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_leases_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_leases_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))
    print('Created table: agent_task_leases')


def _create_events(connection):
    if _table_exists(connection, 'agent_task_events'):
        print('Table already exists: agent_task_events')
        return
    connection.execute(text("""
        CREATE TABLE agent_task_events (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            task_id BIGINT NOT NULL,
            attempt_id VARCHAR(64) NOT NULL,
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            event_type VARCHAR(32) NOT NULL,
            seq INT NOT NULL DEFAULT 1,
            event_timestamp DATETIME NOT NULL,
            payload JSON,
            message TEXT,
            CONSTRAINT fk_events_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_events_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_events_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))
    print('Created table: agent_task_events')


def _create_dedup(connection):
    if _table_exists(connection, 'agent_result_dedup'):
        print('Table already exists: agent_result_dedup')
        return
    connection.execute(text("""
        CREATE TABLE agent_result_dedup (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            idempotency_key VARCHAR(128) NOT NULL UNIQUE,
            task_id BIGINT NOT NULL,
            attempt_id VARCHAR(64) NOT NULL,
            agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            committed_at DATETIME NOT NULL,
            CONSTRAINT fk_dedup_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_dedup_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_dedup_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))
    print('Created table: agent_result_dedup')


def _create_indexes(connection):
    _create_index_if_missing(connection, 'agent_task_attempts', 'idx_agent_task_attempts_task_state', 'CREATE INDEX idx_agent_task_attempts_task_state ON agent_task_attempts (task_id, state)')
    _create_index_if_missing(connection, 'agent_task_leases', 'idx_agent_task_leases_task_active_exp', 'CREATE INDEX idx_agent_task_leases_task_active_exp ON agent_task_leases (task_id, active, expires_at)')
    _create_index_if_missing(connection, 'agent_task_events', 'idx_agent_task_events_task_created', 'CREATE INDEX idx_agent_task_events_task_created ON agent_task_events (task_id, created_at)')


def upgrade(connection):
    _create_attempts(connection)
    _create_leases(connection)
    _create_events(connection)
    _create_dedup(connection)
    _create_indexes(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, 'agent_task_events', 'idx_agent_task_events_task_created')
    _drop_index_if_exists(connection, 'agent_task_leases', 'idx_agent_task_leases_task_active_exp')
    _drop_index_if_exists(connection, 'agent_task_attempts', 'idx_agent_task_attempts_task_state')

    for table in ['agent_result_dedup', 'agent_task_events', 'agent_task_leases', 'agent_task_attempts']:
        if _table_exists(connection, table):
            connection.execute(text(f"DROP TABLE {table}"))
            print(f"Dropped table: {table}")
