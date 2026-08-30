"""
Migration: expand_agent_audit_events_dimensions
Description: add structured dimensions and indexes for richer agent activity queries
Created: 2026-03-08T14:00:00
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


def _add_column_if_missing(connection, table_name, column_name, ddl):
    if _column_exists(connection, table_name, column_name):
        return
    connection.execute(text(ddl))


def _drop_column_if_exists(connection, table_name, column_name):
    if not _column_exists(connection, table_name, column_name):
        return
    connection.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))


def _create_index_if_missing(connection, table_name, index_name, ddl):
    if _index_exists(connection, table_name, index_name):
        return
    connection.execute(text(ddl))


def _drop_index_if_exists(connection, table_name, index_name):
    if not _index_exists(connection, table_name, index_name):
        return
    connection.execute(text(f"DROP INDEX {index_name} ON {table_name}"))


def upgrade(connection):
    if not _table_exists(connection, 'agent_audit_events'):
        return

    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'source',
        "ALTER TABLE agent_audit_events ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'api'"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'level',
        "ALTER TABLE agent_audit_events ADD COLUMN level VARCHAR(16) NOT NULL DEFAULT 'info'"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'correlation_id',
        "ALTER TABLE agent_audit_events ADD COLUMN correlation_id VARCHAR(64) NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'request_id',
        "ALTER TABLE agent_audit_events ADD COLUMN request_id VARCHAR(64) NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'run_id',
        "ALTER TABLE agent_audit_events ADD COLUMN run_id VARCHAR(64) NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'attempt_id',
        "ALTER TABLE agent_audit_events ADD COLUMN attempt_id VARCHAR(64) NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'task_id',
        "ALTER TABLE agent_audit_events ADD COLUMN task_id BIGINT NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'project_id',
        "ALTER TABLE agent_audit_events ADD COLUMN project_id INT NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'actor_agent_id',
        "ALTER TABLE agent_audit_events ADD COLUMN actor_agent_id INT NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'target_agent_id',
        "ALTER TABLE agent_audit_events ADD COLUMN target_agent_id INT NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'duration_ms',
        "ALTER TABLE agent_audit_events ADD COLUMN duration_ms INT NULL"
    )
    _add_column_if_missing(
        connection,
        'agent_audit_events',
        'error_code',
        "ALTER TABLE agent_audit_events ADD COLUMN error_code VARCHAR(64) NULL"
    )

    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_time',
        'CREATE INDEX idx_agent_audit_workspace_time ON agent_audit_events (workspace_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_source_time',
        'CREATE INDEX idx_agent_audit_workspace_source_time ON agent_audit_events (workspace_id, source, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_level_time',
        'CREATE INDEX idx_agent_audit_workspace_level_time ON agent_audit_events (workspace_id, level, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_task_time',
        'CREATE INDEX idx_agent_audit_workspace_task_time ON agent_audit_events (workspace_id, task_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_project_time',
        'CREATE INDEX idx_agent_audit_workspace_project_time ON agent_audit_events (workspace_id, project_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_run_time',
        'CREATE INDEX idx_agent_audit_workspace_run_time ON agent_audit_events (workspace_id, run_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_attempt_time',
        'CREATE INDEX idx_agent_audit_workspace_attempt_time ON agent_audit_events (workspace_id, attempt_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_actor_agent_time',
        'CREATE INDEX idx_agent_audit_workspace_actor_agent_time ON agent_audit_events (workspace_id, actor_agent_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_target_agent_time',
        'CREATE INDEX idx_agent_audit_workspace_target_agent_time ON agent_audit_events (workspace_id, target_agent_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'agent_audit_events',
        'idx_agent_audit_workspace_corr_time',
        'CREATE INDEX idx_agent_audit_workspace_corr_time ON agent_audit_events (workspace_id, correlation_id, occurred_at)'
    )


def downgrade(connection):
    if not _table_exists(connection, 'agent_audit_events'):
        return

    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_corr_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_target_agent_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_actor_agent_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_attempt_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_run_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_project_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_task_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_level_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_source_time')
    _drop_index_if_exists(connection, 'agent_audit_events', 'idx_agent_audit_workspace_time')

    _drop_column_if_exists(connection, 'agent_audit_events', 'error_code')
    _drop_column_if_exists(connection, 'agent_audit_events', 'duration_ms')
    _drop_column_if_exists(connection, 'agent_audit_events', 'target_agent_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'actor_agent_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'project_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'task_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'attempt_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'run_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'request_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'correlation_id')
    _drop_column_if_exists(connection, 'agent_audit_events', 'level')
    _drop_column_if_exists(connection, 'agent_audit_events', 'source')
