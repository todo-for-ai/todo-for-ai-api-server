"""
Migration: expand_org_role_definition_content
Description: expand organization role definition fields for title/description/content style settings
Created: 2026-03-06T10:00:00
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


def upgrade(connection):
    if not _table_exists(connection, "organization_role_definitions"):
        print("Table not found, skip: organization_role_definitions")
        return

    if _column_exists(connection, "organization_role_definitions", "description"):
        connection.execute(
            text(
                """
                ALTER TABLE organization_role_definitions
                MODIFY COLUMN description TEXT NULL COMMENT '角色描述'
                """
            )
        )
        print("Modified column description to TEXT")

    if not _column_exists(connection, "organization_role_definitions", "content"):
        connection.execute(
            text(
                """
                ALTER TABLE organization_role_definitions
                ADD COLUMN content TEXT NULL COMMENT '角色设定内容（Markdown）' AFTER description
                """
            )
        )
        print("Added column: content")
    else:
        print("Column already exists, skip: content")


def downgrade(connection):
    if not _table_exists(connection, "organization_role_definitions"):
        print("Table not found, skip: organization_role_definitions")
        return

    if _column_exists(connection, "organization_role_definitions", "content"):
        connection.execute(
            text(
                """
                ALTER TABLE organization_role_definitions
                DROP COLUMN content
                """
            )
        )
        print("Dropped column: content")
    else:
        print("Column not found, skip drop: content")

    if _column_exists(connection, "organization_role_definitions", "description"):
        connection.execute(
            text(
                """
                ALTER TABLE organization_role_definitions
                MODIFY COLUMN description VARCHAR(255) NULL COMMENT '角色描述'
                """
            )
        )
        print("Reverted column description to VARCHAR(255)")
