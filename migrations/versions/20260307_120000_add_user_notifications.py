"""
Migration: add_user_notifications
Description: add in-app notification center table
Created: 2026-03-07T12:00:00
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
    connection.execute(text(f'DROP INDEX {index_name} ON {table_name}'))


def _create_user_notifications(connection):
    if _table_exists(connection, 'user_notifications'):
        return

    connection.execute(text("""
        CREATE TABLE user_notifications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            user_id INT NOT NULL,
            event_id VARCHAR(64) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            category VARCHAR(32) NOT NULL DEFAULT 'task',
            title VARCHAR(255) NOT NULL,
            body TEXT NULL,
            level VARCHAR(16) NOT NULL DEFAULT 'info',
            link_url VARCHAR(500) NULL,
            resource_type VARCHAR(32) NOT NULL DEFAULT 'task',
            resource_id BIGINT NULL,
            actor_user_id INT NULL,
            project_id INT NULL,
            organization_id INT NULL,
            extra_payload JSON NULL,
            read_at DATETIME NULL,
            archived_at DATETIME NULL,
            dedup_key VARCHAR(255) NOT NULL,
            CONSTRAINT uq_user_notifications_dedup UNIQUE (dedup_key),
            CONSTRAINT fk_user_notifications_user FOREIGN KEY (user_id) REFERENCES users(id),
            CONSTRAINT fk_user_notifications_actor FOREIGN KEY (actor_user_id) REFERENCES users(id),
            CONSTRAINT fk_user_notifications_project FOREIGN KEY (project_id) REFERENCES projects(id),
            CONSTRAINT fk_user_notifications_organization FOREIGN KEY (organization_id) REFERENCES organizations(id)
        )
    """))


def _create_indexes(connection):
    _create_index_if_missing(connection, 'user_notifications', 'idx_user_notifications_user_read_created', 'CREATE INDEX idx_user_notifications_user_read_created ON user_notifications (user_id, read_at, created_at)')
    _create_index_if_missing(connection, 'user_notifications', 'idx_user_notifications_event_created', 'CREATE INDEX idx_user_notifications_event_created ON user_notifications (event_type, created_at)')
    _create_index_if_missing(connection, 'user_notifications', 'idx_user_notifications_resource', 'CREATE INDEX idx_user_notifications_resource ON user_notifications (resource_type, resource_id)')


def upgrade(connection):
    _create_user_notifications(connection)
    _create_indexes(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, 'user_notifications', 'idx_user_notifications_resource')
    _drop_index_if_exists(connection, 'user_notifications', 'idx_user_notifications_event_created')
    _drop_index_if_exists(connection, 'user_notifications', 'idx_user_notifications_user_read_created')
    if _table_exists(connection, 'user_notifications'):
        connection.execute(text('DROP TABLE user_notifications'))
