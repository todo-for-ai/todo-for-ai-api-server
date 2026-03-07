"""
Migration: add_org_custom_roles_and_member_role_bindings
Description: add organization role definitions and member-role mapping tables
Created: 2026-03-06T08:00:00
"""

from sqlalchemy import text


SYSTEM_ROLES = (
    ('owner', 'Owner', 'Organization owner'),
    ('admin', 'Admin', 'Organization admin'),
    ('member', 'Member', 'Organization member'),
    ('viewer', 'Viewer', 'Read-only member'),
)


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


def _create_tables(connection):
    if not _table_exists(connection, "organization_role_definitions"):
        connection.execute(
            text(
                """
                CREATE TABLE organization_role_definitions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_by VARCHAR(100),
                    organization_id INT NOT NULL,
                    `key` VARCHAR(64) NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    description VARCHAR(255) NULL,
                    is_system BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    CONSTRAINT uq_org_role_definition_org_key UNIQUE (organization_id, `key`),
                    CONSTRAINT fk_org_role_definition_org FOREIGN KEY (organization_id) REFERENCES organizations(id)
                )
                """
            )
        )
        print("Created table: organization_role_definitions")
    else:
        print("Table already exists: organization_role_definitions")

    if not _table_exists(connection, "organization_member_roles"):
        connection.execute(
            text(
                """
                CREATE TABLE organization_member_roles (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_by VARCHAR(100),
                    organization_id INT NOT NULL,
                    member_id INT NOT NULL,
                    role_id INT NOT NULL,
                    CONSTRAINT uq_org_member_role_member_role UNIQUE (member_id, role_id),
                    CONSTRAINT fk_org_member_roles_org FOREIGN KEY (organization_id) REFERENCES organizations(id),
                    CONSTRAINT fk_org_member_roles_member FOREIGN KEY (member_id) REFERENCES organization_members(id),
                    CONSTRAINT fk_org_member_roles_role FOREIGN KEY (role_id) REFERENCES organization_role_definitions(id)
                )
                """
            )
        )
        print("Created table: organization_member_roles")
    else:
        print("Table already exists: organization_member_roles")


def _create_indexes(connection):
    _create_index_if_missing(
        connection,
        "organization_role_definitions",
        "idx_org_role_definition_org_active",
        "CREATE INDEX idx_org_role_definition_org_active ON organization_role_definitions (organization_id, is_active)",
    )
    _create_index_if_missing(
        connection,
        "organization_member_roles",
        "idx_org_member_roles_org_member",
        "CREATE INDEX idx_org_member_roles_org_member ON organization_member_roles (organization_id, member_id)",
    )
    _create_index_if_missing(
        connection,
        "organization_member_roles",
        "idx_org_member_roles_role",
        "CREATE INDEX idx_org_member_roles_role ON organization_member_roles (role_id)",
    )


def _seed_system_roles(connection):
    org_rows = connection.execute(text("SELECT id FROM organizations")).fetchall()
    for org_row in org_rows:
        org_id = int(org_row.id)
        for key, name, description in SYSTEM_ROLES:
            exists = connection.execute(
                text(
                    """
                    SELECT COUNT(1) AS cnt
                    FROM organization_role_definitions
                    WHERE organization_id = :organization_id AND `key` = :role_key
                    """
                ),
                {"organization_id": org_id, "role_key": key},
            ).scalar()
            if exists:
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO organization_role_definitions (
                        organization_id, `key`, name, description, is_system, is_active, created_by, created_at, updated_at
                    ) VALUES (
                        :organization_id, :role_key, :name, :description, TRUE, TRUE, 'migration', NOW(), NOW()
                    )
                    """
                ),
                {
                    "organization_id": org_id,
                    "role_key": key,
                    "name": name,
                    "description": description,
                },
            )


def _migrate_member_roles(connection):
    member_rows = connection.execute(
        text(
            """
            SELECT id, organization_id, role
            FROM organization_members
            WHERE status != 'REMOVED'
            """
        )
    ).fetchall()

    for row in member_rows:
        member_id = int(row.id)
        organization_id = int(row.organization_id)
        role_key = (row.role or '').strip().lower() or 'member'
        if role_key not in {item[0] for item in SYSTEM_ROLES}:
            role_key = 'member'

        role_id = connection.execute(
            text(
                """
                SELECT id
                FROM organization_role_definitions
                WHERE organization_id = :organization_id
                  AND `key` = :role_key
                LIMIT 1
                """
            ),
            {"organization_id": organization_id, "role_key": role_key},
        ).scalar()

        if not role_id:
            continue

        connection.execute(
            text(
                """
                INSERT IGNORE INTO organization_member_roles (
                    organization_id, member_id, role_id, created_by, created_at, updated_at
                ) VALUES (
                    :organization_id, :member_id, :role_id, 'migration', NOW(), NOW()
                )
                """
            ),
            {
                "organization_id": organization_id,
                "member_id": member_id,
                "role_id": int(role_id),
            },
        )


def upgrade(connection):
    _create_tables(connection)
    _create_indexes(connection)
    _seed_system_roles(connection)
    _migrate_member_roles(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, "organization_member_roles", "idx_org_member_roles_role")
    _drop_index_if_exists(connection, "organization_member_roles", "idx_org_member_roles_org_member")
    _drop_index_if_exists(connection, "organization_role_definitions", "idx_org_role_definition_org_active")

    if _table_exists(connection, "organization_member_roles"):
        connection.execute(text("DROP TABLE organization_member_roles"))
        print("Dropped table: organization_member_roles")
    if _table_exists(connection, "organization_role_definitions"):
        connection.execute(text("DROP TABLE organization_role_definitions"))
        print("Dropped table: organization_role_definitions")
