"""
Migration: add_agent_activity_events
Description: add unified append-only activity event table for agents
Created: 2026-03-21T23:50:00
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
    if _table_exists(connection, 'agent_activity_events'):
        return

    connection.execute(
        text(
            """
            CREATE TABLE agent_activity_events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                created_by VARCHAR(100) NULL,

                workspace_id INT NOT NULL,
                agent_id INT NULL,
                source VARCHAR(32) NOT NULL DEFAULT 'agent_audit',
                event_type VARCHAR(64) NOT NULL,
                level VARCHAR(16) NOT NULL DEFAULT 'info',
                message VARCHAR(512) NULL,
                payload JSON NULL,
                occurred_at DATETIME NOT NULL,

                task_id BIGINT NULL,
                project_id INT NULL,
                run_id VARCHAR(64) NULL,
                attempt_id VARCHAR(64) NULL,
                correlation_id VARCHAR(64) NULL,
                request_id VARCHAR(64) NULL,

                actor_type VARCHAR(32) NULL,
                actor_id VARCHAR(64) NULL,
                target_type VARCHAR(32) NULL,
                target_id VARCHAR(64) NULL
            )
            """
        )
    )


def _create_indexes(connection):
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_time',
        'CREATE INDEX idx_agent_activity_workspace_time ON agent_activity_events (workspace_id, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_agent_time',
        'CREATE INDEX idx_agent_activity_workspace_agent_time ON agent_activity_events (workspace_id, agent_id, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_source_time',
        'CREATE INDEX idx_agent_activity_workspace_source_time ON agent_activity_events (workspace_id, source, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_level_time',
        'CREATE INDEX idx_agent_activity_workspace_level_time ON agent_activity_events (workspace_id, level, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_event_time',
        'CREATE INDEX idx_agent_activity_workspace_event_time ON agent_activity_events (workspace_id, event_type, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_task_time',
        'CREATE INDEX idx_agent_activity_workspace_task_time ON agent_activity_events (workspace_id, task_id, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_project_time',
        'CREATE INDEX idx_agent_activity_workspace_project_time ON agent_activity_events (workspace_id, project_id, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_run_time',
        'CREATE INDEX idx_agent_activity_workspace_run_time ON agent_activity_events (workspace_id, run_id, occurred_at, id)',
    )
    _create_index_if_missing(
        connection,
        'agent_activity_events',
        'idx_agent_activity_workspace_attempt_time',
        'CREATE INDEX idx_agent_activity_workspace_attempt_time ON agent_activity_events (workspace_id, attempt_id, occurred_at, id)',
    )


def upgrade(connection):
    _create_table(connection)
    _create_indexes(connection)


def downgrade(connection):
    if not _table_exists(connection, 'agent_activity_events'):
        return

    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_attempt_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_run_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_project_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_task_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_event_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_level_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_source_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_agent_time')
    _drop_index_if_exists(connection, 'agent_activity_events', 'idx_agent_activity_workspace_time')
    connection.execute(text('DROP TABLE agent_activity_events'))

