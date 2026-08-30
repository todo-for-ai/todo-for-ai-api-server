"""
Migration: add_org_agent_members_and_task_logs
Description: add organization-agent memberships, task logs and task collaboration fields
Created: 2026-03-06T02:00:00
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


def _create_org_agent_members(connection):
    if _table_exists(connection, 'organization_agent_members'):
        return

    connection.execute(text("""
        CREATE TABLE organization_agent_members (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            organization_id INT NOT NULL,
            agent_id INT NOT NULL,
            invited_by_user_id INT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            joined_at DATETIME NULL,
            responded_at DATETIME NULL,
            CONSTRAINT uq_org_agent_member_org_agent UNIQUE (organization_id, agent_id),
            CONSTRAINT fk_org_agent_member_org FOREIGN KEY (organization_id) REFERENCES organizations(id),
            CONSTRAINT fk_org_agent_member_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_org_agent_member_inviter FOREIGN KEY (invited_by_user_id) REFERENCES users(id)
        )
    """))


def _create_task_logs(connection):
    if _table_exists(connection, 'task_logs'):
        return

    connection.execute(text("""
        CREATE TABLE task_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            task_id BIGINT NOT NULL,
            actor_type VARCHAR(20) NOT NULL,
            actor_user_id INT NULL,
            actor_agent_id INT NULL,
            content TEXT NOT NULL,
            content_type VARCHAR(32) NOT NULL DEFAULT 'text/markdown',
            CONSTRAINT fk_task_logs_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_task_logs_user FOREIGN KEY (actor_user_id) REFERENCES users(id),
            CONSTRAINT fk_task_logs_agent FOREIGN KEY (actor_agent_id) REFERENCES agents(id)
        )
    """))


def _add_task_columns(connection):
    if not _column_exists(connection, 'tasks', 'assignees'):
        connection.execute(text("ALTER TABLE tasks ADD COLUMN assignees JSON NULL"))

    if not _column_exists(connection, 'tasks', 'mentions'):
        connection.execute(text("ALTER TABLE tasks ADD COLUMN mentions JSON NULL"))

    if not _column_exists(connection, 'tasks', 'revision'):
        connection.execute(text("ALTER TABLE tasks ADD COLUMN revision INT NOT NULL DEFAULT 1"))


def _drop_task_columns(connection):
    if _column_exists(connection, 'tasks', 'revision'):
        connection.execute(text("ALTER TABLE tasks DROP COLUMN revision"))

    if _column_exists(connection, 'tasks', 'mentions'):
        connection.execute(text("ALTER TABLE tasks DROP COLUMN mentions"))

    if _column_exists(connection, 'tasks', 'assignees'):
        connection.execute(text("ALTER TABLE tasks DROP COLUMN assignees"))


def upgrade(connection):
    _create_org_agent_members(connection)
    _create_task_logs(connection)
    _add_task_columns(connection)

    _create_index_if_missing(connection, 'organization_agent_members', 'idx_org_agent_member_org_status', 'CREATE INDEX idx_org_agent_member_org_status ON organization_agent_members (organization_id, status)')
    _create_index_if_missing(connection, 'organization_agent_members', 'idx_org_agent_member_agent_status', 'CREATE INDEX idx_org_agent_member_agent_status ON organization_agent_members (agent_id, status)')
    _create_index_if_missing(connection, 'task_logs', 'idx_task_logs_task_created', 'CREATE INDEX idx_task_logs_task_created ON task_logs (task_id, created_at)')


def downgrade(connection):
    _drop_index_if_exists(connection, 'task_logs', 'idx_task_logs_task_created')
    _drop_index_if_exists(connection, 'organization_agent_members', 'idx_org_agent_member_agent_status')
    _drop_index_if_exists(connection, 'organization_agent_members', 'idx_org_agent_member_org_status')

    _drop_task_columns(connection)

    if _table_exists(connection, 'task_logs'):
        connection.execute(text("DROP TABLE task_logs"))

    if _table_exists(connection, 'organization_agent_members'):
        connection.execute(text("DROP TABLE organization_agent_members"))
