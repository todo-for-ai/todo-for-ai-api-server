"""
Migration: add_org_members_and_task_labels
Description: add organization, project member, and task label tables
Created: 2026-03-03T22:30:00
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
    if not _table_exists(connection, "organizations"):
        connection.execute(
            text(
                """
                CREATE TABLE organizations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_by VARCHAR(100),
                    owner_id INT NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    slug VARCHAR(255) NOT NULL UNIQUE,
                    description TEXT,
                    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                    CONSTRAINT fk_organizations_owner FOREIGN KEY (owner_id) REFERENCES users(id)
                )
                """
            )
        )
        print("Created table: organizations")
    else:
        print("Table already exists: organizations")

    if not _table_exists(connection, "organization_members"):
        connection.execute(
            text(
                """
                CREATE TABLE organization_members (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_by VARCHAR(100),
                    organization_id INT NOT NULL,
                    user_id INT NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'MEMBER',
                    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                    invited_by INT NULL,
                    joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_organization_member_org_user UNIQUE (organization_id, user_id),
                    CONSTRAINT fk_org_members_org FOREIGN KEY (organization_id) REFERENCES organizations(id),
                    CONSTRAINT fk_org_members_user FOREIGN KEY (user_id) REFERENCES users(id),
                    CONSTRAINT fk_org_members_inviter FOREIGN KEY (invited_by) REFERENCES users(id)
                )
                """
            )
        )
        print("Created table: organization_members")
    else:
        print("Table already exists: organization_members")

    if not _table_exists(connection, "project_members"):
        connection.execute(
            text(
                """
                CREATE TABLE project_members (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_by VARCHAR(100),
                    project_id INT NOT NULL,
                    user_id INT NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'MEMBER',
                    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                    invited_by INT NULL,
                    joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_project_member_project_user UNIQUE (project_id, user_id),
                    CONSTRAINT fk_project_members_project FOREIGN KEY (project_id) REFERENCES projects(id),
                    CONSTRAINT fk_project_members_user FOREIGN KEY (user_id) REFERENCES users(id),
                    CONSTRAINT fk_project_members_inviter FOREIGN KEY (invited_by) REFERENCES users(id)
                )
                """
            )
        )
        print("Created table: project_members")
    else:
        print("Table already exists: project_members")

    if not _table_exists(connection, "task_labels"):
        connection.execute(
            text(
                """
                CREATE TABLE task_labels (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    created_by VARCHAR(100),
                    owner_id INT NULL,
                    project_id INT NULL,
                    name VARCHAR(64) NOT NULL,
                    color VARCHAR(16) NOT NULL DEFAULT '#1677ff',
                    description VARCHAR(255) NULL,
                    is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by_user_id INT NULL,
                    CONSTRAINT fk_task_labels_owner FOREIGN KEY (owner_id) REFERENCES users(id),
                    CONSTRAINT fk_task_labels_project FOREIGN KEY (project_id) REFERENCES projects(id),
                    CONSTRAINT fk_task_labels_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        print("Created table: task_labels")
    else:
        print("Table already exists: task_labels")


def _add_project_columns(connection):
    if not _column_exists(connection, "projects", "organization_id"):
        connection.execute(
            text("ALTER TABLE projects ADD COLUMN organization_id INT NULL")
        )
        print("Added column projects.organization_id")
    else:
        print("Column already exists: projects.organization_id")

    # Foreign key 名称可能冲突，先尝试添加，失败则忽略
    try:
        connection.execute(
            text(
                """
                ALTER TABLE projects
                ADD CONSTRAINT fk_projects_organization
                FOREIGN KEY (organization_id) REFERENCES organizations(id)
                """
            )
        )
        print("Added fk_projects_organization")
    except Exception:
        print("Skip add fk_projects_organization (already exists or incompatible)")


def _create_indexes(connection):
    _create_index_if_missing(
        connection,
        "organizations",
        "idx_organizations_owner_id",
        "CREATE INDEX idx_organizations_owner_id ON organizations (owner_id)",
    )
    _create_index_if_missing(
        connection,
        "organization_members",
        "idx_org_members_org_status",
        "CREATE INDEX idx_org_members_org_status ON organization_members (organization_id, status)",
    )
    _create_index_if_missing(
        connection,
        "organization_members",
        "idx_org_members_user_status",
        "CREATE INDEX idx_org_members_user_status ON organization_members (user_id, status)",
    )
    _create_index_if_missing(
        connection,
        "project_members",
        "idx_project_members_project_status",
        "CREATE INDEX idx_project_members_project_status ON project_members (project_id, status)",
    )
    _create_index_if_missing(
        connection,
        "project_members",
        "idx_project_members_user_status",
        "CREATE INDEX idx_project_members_user_status ON project_members (user_id, status)",
    )
    _create_index_if_missing(
        connection,
        "projects",
        "idx_projects_organization_id",
        "CREATE INDEX idx_projects_organization_id ON projects (organization_id)",
    )
    _create_index_if_missing(
        connection,
        "task_labels",
        "idx_task_labels_owner_project_active",
        "CREATE INDEX idx_task_labels_owner_project_active ON task_labels (owner_id, project_id, is_active)",
    )
    _create_index_if_missing(
        connection,
        "task_labels",
        "idx_task_labels_builtin_name",
        "CREATE INDEX idx_task_labels_builtin_name ON task_labels (is_builtin, name)",
    )


def _seed_builtin_labels(connection):
    builtin_labels = [
        ("task", "#1677ff", "General task"),
        ("bug", "#ff4d4f", "Bug fix"),
        ("improvement", "#722ed1", "Improvement suggestion"),
        ("feature", "#13c2c2", "Feature request"),
        ("urgent", "#fa541c", "Urgent"),
        ("research", "#2f54eb", "Research"),
        ("refactor", "#fa8c16", "Refactor"),
        ("documentation", "#52c41a", "Documentation"),
    ]

    for name, color, desc in builtin_labels:
        connection.execute(
            text(
                """
                INSERT INTO task_labels (
                    created_at, updated_at, created_by,
                    owner_id, project_id, name, color, description,
                    is_builtin, is_active, created_by_user_id
                )
                SELECT NOW(), NOW(), 'system',
                       NULL, NULL, :name, :color, :description,
                       TRUE, TRUE, NULL
                FROM DUAL
                WHERE NOT EXISTS (
                    SELECT 1 FROM task_labels
                    WHERE is_builtin = TRUE
                      AND name = :name
                      AND project_id IS NULL
                      AND owner_id IS NULL
                )
                """
            ),
            {"name": name, "color": color, "description": desc},
        )
    print("Seeded builtin task labels")


def _backfill_project_owner_membership(connection):
    connection.execute(
        text(
            """
            INSERT INTO project_members (
                created_at, updated_at, created_by,
                project_id, user_id, role, status, invited_by, joined_at
            )
            SELECT NOW(), NOW(), 'system',
                   p.id, p.owner_id, 'OWNER', 'ACTIVE', NULL, NOW()
            FROM projects p
            LEFT JOIN project_members pm
              ON pm.project_id = p.id
             AND pm.user_id = p.owner_id
            WHERE pm.id IS NULL
            """
        )
    )
    print("Backfilled owner records into project_members")


def upgrade(connection):
    """执行迁移"""
    _create_tables(connection)
    _add_project_columns(connection)
    _create_indexes(connection)
    _seed_builtin_labels(connection)
    _backfill_project_owner_membership(connection)


def downgrade(connection):
    """回滚迁移"""
    _drop_index_if_exists(connection, "task_labels", "idx_task_labels_builtin_name")
    _drop_index_if_exists(connection, "task_labels", "idx_task_labels_owner_project_active")
    _drop_index_if_exists(connection, "projects", "idx_projects_organization_id")
    _drop_index_if_exists(connection, "project_members", "idx_project_members_user_status")
    _drop_index_if_exists(connection, "project_members", "idx_project_members_project_status")
    _drop_index_if_exists(connection, "organization_members", "idx_org_members_user_status")
    _drop_index_if_exists(connection, "organization_members", "idx_org_members_org_status")
    _drop_index_if_exists(connection, "organizations", "idx_organizations_owner_id")

    if _column_exists(connection, "projects", "organization_id"):
        try:
            connection.execute(text("ALTER TABLE projects DROP FOREIGN KEY fk_projects_organization"))
        except Exception:
            pass
        connection.execute(text("ALTER TABLE projects DROP COLUMN organization_id"))
        print("Dropped column projects.organization_id")

    if _table_exists(connection, "task_labels"):
        connection.execute(text("DROP TABLE task_labels"))
        print("Dropped table: task_labels")
    if _table_exists(connection, "project_members"):
        connection.execute(text("DROP TABLE project_members"))
        print("Dropped table: project_members")
    if _table_exists(connection, "organization_members"):
        connection.execute(text("DROP TABLE organization_members"))
        print("Dropped table: organization_members")
    if _table_exists(connection, "organizations"):
        connection.execute(text("DROP TABLE organizations"))
        print("Dropped table: organizations")
