"""
Migration: add_organization_events
Description: add organization event stream table
Created: 2026-03-13T12:00:00
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


def upgrade(connection):
    if not _table_exists(connection, 'organization_events'):
        connection.execute(text("""
            CREATE TABLE organization_events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                created_by VARCHAR(100),
                organization_id INT NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                source VARCHAR(32) NOT NULL DEFAULT 'api',
                level VARCHAR(16) NOT NULL DEFAULT 'info',
                actor_type VARCHAR(32) NULL,
                actor_id VARCHAR(64) NULL,
                actor_name VARCHAR(128) NULL,
                target_type VARCHAR(32) NULL,
                target_id VARCHAR(64) NULL,
                project_id INT NULL,
                task_id BIGINT NULL,
                message VARCHAR(512) NULL,
                payload JSON NULL,
                occurred_at DATETIME NOT NULL,
                CONSTRAINT fk_org_events_org FOREIGN KEY (organization_id) REFERENCES organizations(id)
            )
        """))

    _create_index_if_missing(
        connection,
        'organization_events',
        'idx_org_events_org_time',
        'CREATE INDEX idx_org_events_org_time ON organization_events (organization_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'organization_events',
        'idx_org_events_org_event_type',
        'CREATE INDEX idx_org_events_org_event_type ON organization_events (organization_id, event_type, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'organization_events',
        'idx_org_events_org_project_time',
        'CREATE INDEX idx_org_events_org_project_time ON organization_events (organization_id, project_id, occurred_at)'
    )
    _create_index_if_missing(
        connection,
        'organization_events',
        'idx_org_events_org_task_time',
        'CREATE INDEX idx_org_events_org_task_time ON organization_events (organization_id, task_id, occurred_at)'
    )


def downgrade(connection):
    _drop_index_if_exists(connection, 'organization_events', 'idx_org_events_org_task_time')
    _drop_index_if_exists(connection, 'organization_events', 'idx_org_events_org_project_time')
    _drop_index_if_exists(connection, 'organization_events', 'idx_org_events_org_event_type')
    _drop_index_if_exists(connection, 'organization_events', 'idx_org_events_org_time')

    if _table_exists(connection, 'organization_events'):
        connection.execute(text("DROP TABLE organization_events"))
