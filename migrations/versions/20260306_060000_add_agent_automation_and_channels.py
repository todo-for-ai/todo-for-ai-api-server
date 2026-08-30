"""
Migration: add_agent_automation_and_channels
Description: add agent triggers, runs, task event outbox, channel configs and runner fields
Created: 2026-03-06T06:00:00
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
        ('execution_mode', 'ALTER TABLE agents ADD COLUMN execution_mode VARCHAR(32) NOT NULL DEFAULT "external_pull"'),
        ('runner_enabled', 'ALTER TABLE agents ADD COLUMN runner_enabled BOOLEAN NOT NULL DEFAULT FALSE'),
        ('sandbox_profile', 'ALTER TABLE agents ADD COLUMN sandbox_profile VARCHAR(64) NOT NULL DEFAULT "standard"'),
        ('sandbox_policy', 'ALTER TABLE agents ADD COLUMN sandbox_policy JSON NULL'),
        ('runner_config_version', 'ALTER TABLE agents ADD COLUMN runner_config_version INT NOT NULL DEFAULT 1'),
    ]
    for column_name, ddl in columns:
        if not _column_exists(connection, 'agents', column_name):
            connection.execute(text(ddl))


def _create_agent_triggers(connection):
    if _table_exists(connection, 'agent_triggers'):
        return

    connection.execute(text("""
        CREATE TABLE agent_triggers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            workspace_id INT NOT NULL,
            agent_id INT NOT NULL,
            name VARCHAR(128) NOT NULL,
            trigger_type VARCHAR(16) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            priority INT NOT NULL DEFAULT 100,
            task_event_types JSON,
            task_filter JSON,
            cron_expr VARCHAR(64),
            timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
            misfire_policy VARCHAR(24) NOT NULL DEFAULT 'catch_up_once',
            catch_up_window_seconds INT NOT NULL DEFAULT 300,
            dedup_window_seconds INT NOT NULL DEFAULT 60,
            last_triggered_at DATETIME NULL,
            next_fire_at DATETIME NULL,
            CONSTRAINT fk_agent_triggers_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_triggers_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT uq_agent_triggers_agent_name UNIQUE (agent_id, name)
        )
    """))


def _create_agent_runs(connection):
    if _table_exists(connection, 'agent_runs'):
        return

    connection.execute(text("""
        CREATE TABLE agent_runs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            run_id VARCHAR(64) NOT NULL UNIQUE,
            workspace_id INT NOT NULL,
            agent_id INT NOT NULL,
            trigger_id INT NOT NULL,
            trigger_reason VARCHAR(64) NOT NULL,
            input_payload JSON,
            state VARCHAR(16) NOT NULL DEFAULT 'queued',
            scheduled_at DATETIME NOT NULL,
            started_at DATETIME NULL,
            ended_at DATETIME NULL,
            lease_id VARCHAR(64) NULL,
            lease_expires_at DATETIME NULL,
            attempt_count INT NOT NULL DEFAULT 0,
            failure_code VARCHAR(64) NULL,
            failure_reason TEXT,
            idempotency_key VARCHAR(128) NOT NULL UNIQUE,
            CONSTRAINT fk_agent_runs_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_runs_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_runs_trigger FOREIGN KEY (trigger_id) REFERENCES agent_triggers(id)
        )
    """))


def _create_task_event_outbox(connection):
    if _table_exists(connection, 'task_event_outbox'):
        return

    connection.execute(text("""
        CREATE TABLE task_event_outbox (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            event_id VARCHAR(64) NOT NULL UNIQUE,
            event_type VARCHAR(64) NOT NULL,
            task_id BIGINT NOT NULL,
            project_id INT NOT NULL,
            workspace_id INT NULL,
            payload JSON,
            occurred_at DATETIME NOT NULL,
            processed_at DATETIME NULL,
            CONSTRAINT fk_task_event_outbox_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_task_event_outbox_project FOREIGN KEY (project_id) REFERENCES projects(id),
            CONSTRAINT fk_task_event_outbox_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))


def _create_notification_channels(connection):
    if _table_exists(connection, 'notification_channels'):
        return

    connection.execute(text("""
        CREATE TABLE notification_channels (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            scope_type VARCHAR(16) NOT NULL,
            scope_id INT NOT NULL,
            name VARCHAR(128) NOT NULL,
            channel_type VARCHAR(16) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            events JSON,
            config JSON,
            created_by_user_id INT NOT NULL,
            updated_by_user_id INT NOT NULL,
            CONSTRAINT fk_notification_channels_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id),
            CONSTRAINT fk_notification_channels_updater FOREIGN KEY (updated_by_user_id) REFERENCES users(id)
        )
    """))


def _create_notification_deliveries(connection):
    if _table_exists(connection, 'notification_deliveries'):
        return

    connection.execute(text("""
        CREATE TABLE notification_deliveries (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            event_type VARCHAR(64) NOT NULL,
            event_id VARCHAR(64) NOT NULL,
            channel_id INT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INT NOT NULL DEFAULT 0,
            next_retry_at DATETIME NULL,
            response_code INT NULL,
            response_excerpt TEXT,
            CONSTRAINT fk_notification_deliveries_channel FOREIGN KEY (channel_id) REFERENCES notification_channels(id)
        )
    """))


def _create_indexes(connection):
    _create_index_if_missing(connection, 'agent_triggers', 'idx_agent_triggers_workspace_agent_enabled', 'CREATE INDEX idx_agent_triggers_workspace_agent_enabled ON agent_triggers (workspace_id, agent_id, enabled)')
    _create_index_if_missing(connection, 'agent_triggers', 'idx_agent_triggers_type_fire_enabled', 'CREATE INDEX idx_agent_triggers_type_fire_enabled ON agent_triggers (trigger_type, next_fire_at, enabled)')

    _create_index_if_missing(connection, 'agent_runs', 'idx_agent_runs_state_scheduled', 'CREATE INDEX idx_agent_runs_state_scheduled ON agent_runs (state, scheduled_at)')
    _create_index_if_missing(connection, 'agent_runs', 'idx_agent_runs_agent_state_scheduled', 'CREATE INDEX idx_agent_runs_agent_state_scheduled ON agent_runs (agent_id, state, scheduled_at)')

    _create_index_if_missing(connection, 'task_event_outbox', 'idx_task_event_outbox_processed_occurred', 'CREATE INDEX idx_task_event_outbox_processed_occurred ON task_event_outbox (processed_at, occurred_at)')
    _create_index_if_missing(connection, 'task_event_outbox', 'idx_task_event_outbox_event_type_occurred', 'CREATE INDEX idx_task_event_outbox_event_type_occurred ON task_event_outbox (event_type, occurred_at)')

    _create_index_if_missing(connection, 'notification_channels', 'idx_notification_channels_scope_enabled', 'CREATE INDEX idx_notification_channels_scope_enabled ON notification_channels (scope_type, scope_id, enabled)')
    _create_index_if_missing(connection, 'notification_channels', 'idx_notification_channels_scope_type_default', 'CREATE INDEX idx_notification_channels_scope_type_default ON notification_channels (scope_type, scope_id, channel_type, is_default)')

    _create_index_if_missing(connection, 'notification_deliveries', 'idx_notification_deliveries_status_retry', 'CREATE INDEX idx_notification_deliveries_status_retry ON notification_deliveries (status, next_retry_at)')


def upgrade(connection):
    _add_agent_columns(connection)
    _create_agent_triggers(connection)
    _create_agent_runs(connection)
    _create_task_event_outbox(connection)
    _create_notification_channels(connection)
    _create_notification_deliveries(connection)
    _create_indexes(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, 'notification_deliveries', 'idx_notification_deliveries_status_retry')
    _drop_index_if_exists(connection, 'notification_channels', 'idx_notification_channels_scope_type_default')
    _drop_index_if_exists(connection, 'notification_channels', 'idx_notification_channels_scope_enabled')
    _drop_index_if_exists(connection, 'task_event_outbox', 'idx_task_event_outbox_event_type_occurred')
    _drop_index_if_exists(connection, 'task_event_outbox', 'idx_task_event_outbox_processed_occurred')
    _drop_index_if_exists(connection, 'agent_runs', 'idx_agent_runs_agent_state_scheduled')
    _drop_index_if_exists(connection, 'agent_runs', 'idx_agent_runs_state_scheduled')
    _drop_index_if_exists(connection, 'agent_triggers', 'idx_agent_triggers_type_fire_enabled')
    _drop_index_if_exists(connection, 'agent_triggers', 'idx_agent_triggers_workspace_agent_enabled')

    for table in ['notification_deliveries', 'notification_channels', 'task_event_outbox', 'agent_runs', 'agent_triggers']:
        if _table_exists(connection, table):
            connection.execute(text(f'DROP TABLE {table}'))

    drop_columns = ['runner_config_version', 'sandbox_policy', 'sandbox_profile', 'runner_enabled', 'execution_mode']
    for column_name in drop_columns:
        if _column_exists(connection, 'agents', column_name):
            connection.execute(text(f'ALTER TABLE agents DROP COLUMN {column_name}'))
