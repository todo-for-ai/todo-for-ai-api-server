"""
Migration: optimize_project_task_list_indexes
Description: add composite indexes for project/task list performance
Created: 2026-03-01T15:45:00
"""

from sqlalchemy import text


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


def upgrade(connection):
    """执行迁移"""
    _create_index_if_missing(
        connection,
        "tasks",
        "idx_tasks_project_status",
        "CREATE INDEX idx_tasks_project_status ON tasks (project_id, status)",
    )
    _create_index_if_missing(
        connection,
        "tasks",
        "idx_tasks_project_created_at",
        "CREATE INDEX idx_tasks_project_created_at ON tasks (project_id, created_at)",
    )
    _create_index_if_missing(
        connection,
        "tasks",
        "idx_tasks_project_updated_at",
        "CREATE INDEX idx_tasks_project_updated_at ON tasks (project_id, updated_at)",
    )
    _create_index_if_missing(
        connection,
        "projects",
        "idx_projects_owner_status_activity",
        "CREATE INDEX idx_projects_owner_status_activity ON projects (owner_id, status, last_activity_at)",
    )


def downgrade(connection):
    """回滚迁移"""
    _drop_index_if_exists(connection, "tasks", "idx_tasks_project_status")
    _drop_index_if_exists(connection, "tasks", "idx_tasks_project_created_at")
    _drop_index_if_exists(connection, "tasks", "idx_tasks_project_updated_at")
    _drop_index_if_exists(connection, "projects", "idx_projects_owner_status_activity")
