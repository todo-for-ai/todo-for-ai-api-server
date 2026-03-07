"""
Migration: add_notification_event_pipeline
Description: add canonical notification events and delivery reliability fields
Created: 2026-03-07T15:00:00
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
    connection.execute(text(f'DROP INDEX {index_name} ON {table_name}'))


def _create_notification_events(connection):
    if _table_exists(connection, 'notification_events'):
        return

    connection.execute(text("""
        CREATE TABLE notification_events (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            event_id VARCHAR(64) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            category VARCHAR(32) NOT NULL DEFAULT 'task',
            actor_user_id INT NULL,
            resource_type VARCHAR(32) NOT NULL DEFAULT 'task',
            resource_id BIGINT NULL,
            project_id INT NULL,
            organization_id INT NULL,
            payload JSON NULL,
            target_user_ids JSON NULL,
            dispatch_state VARCHAR(16) NOT NULL DEFAULT 'pending',
            in_app_processed_at DATETIME NULL,
            external_queued_at DATETIME NULL,
            external_last_dispatched_at DATETIME NULL,
            CONSTRAINT uq_notification_events_event_id UNIQUE (event_id),
            CONSTRAINT fk_notification_events_actor FOREIGN KEY (actor_user_id) REFERENCES users(id),
            CONSTRAINT fk_notification_events_project FOREIGN KEY (project_id) REFERENCES projects(id),
            CONSTRAINT fk_notification_events_organization FOREIGN KEY (organization_id) REFERENCES organizations(id)
        )
    """))


def _add_delivery_columns(connection):
    columns = [
        ('delivered_at', 'ALTER TABLE notification_deliveries ADD COLUMN delivered_at DATETIME NULL'),
        ('last_error_at', 'ALTER TABLE notification_deliveries ADD COLUMN last_error_at DATETIME NULL'),
        ('request_payload', 'ALTER TABLE notification_deliveries ADD COLUMN request_payload JSON NULL'),
    ]
    for column_name, ddl in columns:
        if not _column_exists(connection, 'notification_deliveries', column_name):
            connection.execute(text(ddl))


def _create_indexes(connection):
    _create_index_if_missing(connection, 'notification_events', 'idx_notification_events_type_created', 'CREATE INDEX idx_notification_events_type_created ON notification_events (event_type, created_at)')
    _create_index_if_missing(connection, 'notification_events', 'idx_notification_events_dispatch_created', 'CREATE INDEX idx_notification_events_dispatch_created ON notification_events (dispatch_state, created_at)')
    _create_index_if_missing(connection, 'notification_deliveries', 'uq_notification_deliveries_event_channel', 'CREATE UNIQUE INDEX uq_notification_deliveries_event_channel ON notification_deliveries (event_id, channel_id)')


def upgrade(connection):
    _create_notification_events(connection)
    if _table_exists(connection, 'notification_deliveries'):
        _add_delivery_columns(connection)
        _create_indexes(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, 'notification_deliveries', 'uq_notification_deliveries_event_channel')
    _drop_index_if_exists(connection, 'notification_events', 'idx_notification_events_dispatch_created')
    _drop_index_if_exists(connection, 'notification_events', 'idx_notification_events_type_created')
    if _table_exists(connection, 'notification_events'):
        connection.execute(text('DROP TABLE notification_events'))
