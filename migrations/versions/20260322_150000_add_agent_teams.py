"""
Migration: add_agent_teams
Description: Add agent teams, role templates, and task orchestration tables
Created: 2026-03-22T15:00:00
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


def _create_agent_role_templates(connection):
    if _table_exists(connection, 'agent_role_templates'):
        print('Table already exists: agent_role_templates')
        return

    connection.execute(text("""
        CREATE TABLE agent_role_templates (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            workspace_id INT NULL,
            created_by_user_id INT NOT NULL,
            name VARCHAR(64) NOT NULL,
            display_name VARCHAR(128) NOT NULL,
            description TEXT,
            avatar_url VARCHAR(512),
            category VARCHAR(32) DEFAULT 'general',
            capability_tags JSON,
            system_prompt TEXT,
            soul_markdown TEXT,
            response_style JSON,
            tool_policy JSON,
            memory_policy JSON,
            handoff_policy JSON,
            llm_provider VARCHAR(64),
            llm_model VARCHAR(128),
            temperature VARCHAR(10),
            reasoning_mode VARCHAR(32) DEFAULT 'balanced',
            is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            usage_count INT DEFAULT 0,
            parent_template_id INT NULL,
            CONSTRAINT fk_agent_role_templates_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_role_templates_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id),
            CONSTRAINT fk_agent_role_templates_parent FOREIGN KEY (parent_template_id) REFERENCES agent_role_templates(id)
        )
    """))
    print('Created table: agent_role_templates')


def _create_agent_teams(connection):
    if _table_exists(connection, 'agent_teams'):
        print('Table already exists: agent_teams')
        return

    connection.execute(text("""
        CREATE TABLE agent_teams (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            workspace_id INT NOT NULL,
            created_by_user_id INT NOT NULL,
            name VARCHAR(128) NOT NULL,
            description TEXT,
            avatar_url VARCHAR(512),
            config JSON,
            default_strategy VARCHAR(32) DEFAULT 'sequential',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            member_count INT DEFAULT 0,
            task_count INT DEFAULT 0,
            CONSTRAINT fk_agent_teams_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_teams_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id)
        )
    """))
    print('Created table: agent_teams')


def _create_agent_team_members(connection):
    if _table_exists(connection, 'agent_team_members'):
        print('Table already exists: agent_team_members')
        return

    connection.execute(text("""
        CREATE TABLE agent_team_members (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            team_id INT NOT NULL,
            agent_id INT NOT NULL,
            added_by_user_id INT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            order_index INT DEFAULT 0,
            responsibility VARCHAR(255),
            config JSON,
            notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT fk_agent_team_members_team FOREIGN KEY (team_id) REFERENCES agent_teams(id) ON DELETE CASCADE,
            CONSTRAINT fk_agent_team_members_agent FOREIGN KEY (agent_id) REFERENCES agents(id),
            CONSTRAINT fk_agent_team_members_added_by FOREIGN KEY (added_by_user_id) REFERENCES users(id),
            UNIQUE KEY uq_team_agent (team_id, agent_id)
        )
    """))
    print('Created table: agent_team_members')


def _create_agent_team_projects(connection):
    if _table_exists(connection, 'agent_team_projects'):
        print('Table already exists: agent_team_projects')
        return

    connection.execute(text("""
        CREATE TABLE agent_team_projects (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            team_id INT NOT NULL,
            project_id INT NOT NULL,
            workspace_id INT NOT NULL,
            added_by_user_id INT NOT NULL,
            config JSON,
            role VARCHAR(32) DEFAULT 'collaborator',
            CONSTRAINT fk_agent_team_projects_team FOREIGN KEY (team_id) REFERENCES agent_teams(id) ON DELETE CASCADE,
            CONSTRAINT fk_agent_team_projects_project FOREIGN KEY (project_id) REFERENCES projects(id),
            CONSTRAINT fk_agent_team_projects_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_agent_team_projects_added_by FOREIGN KEY (added_by_user_id) REFERENCES users(id),
            UNIQUE KEY uq_team_project (team_id, project_id)
        )
    """))
    print('Created table: agent_team_projects')


def _create_team_task_orchestrations(connection):
    if _table_exists(connection, 'team_task_orchestrations'):
        print('Table already exists: team_task_orchestrations')
        return

    connection.execute(text("""
        CREATE TABLE team_task_orchestrations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            team_id INT NOT NULL,
            task_id INT NOT NULL,
            workspace_id INT NOT NULL,
            created_by_user_id INT NOT NULL,
            strategy VARCHAR(20) NOT NULL,
            participating_agent_ids JSON,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            current_stage INT DEFAULT 0,
            total_stages INT DEFAULT 0,
            output_aggregator_agent_id INT,
            result_payload JSON,
            config JSON,
            started_at DATETIME,
            completed_at DATETIME,
            CONSTRAINT fk_orchestrations_team FOREIGN KEY (team_id) REFERENCES agent_teams(id),
            CONSTRAINT fk_orchestrations_task FOREIGN KEY (task_id) REFERENCES tasks(id),
            CONSTRAINT fk_orchestrations_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id),
            CONSTRAINT fk_orchestrations_creator FOREIGN KEY (created_by_user_id) REFERENCES users(id),
            CONSTRAINT fk_orchestrations_aggregator FOREIGN KEY (output_aggregator_agent_id) REFERENCES agents(id)
        )
    """))
    print('Created table: team_task_orchestrations')


def _create_team_subtasks(connection):
    if _table_exists(connection, 'team_subtasks'):
        print('Table already exists: team_subtasks')
        return

    connection.execute(text("""
        CREATE TABLE team_subtasks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(100),
            orchestration_id INT NOT NULL,
            assigned_agent_id INT NOT NULL,
            workspace_id INT NOT NULL,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            stage_index INT DEFAULT 0,
            order_index INT DEFAULT 0,
            depends_on_subtask_ids JSON,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            input_payload JSON,
            output_payload JSON,
            started_at DATETIME,
            completed_at DATETIME,
            attempt_count INT DEFAULT 0,
            last_error TEXT,
            CONSTRAINT fk_subtasks_orchestration FOREIGN KEY (orchestration_id) REFERENCES team_task_orchestrations(id) ON DELETE CASCADE,
            CONSTRAINT fk_subtasks_agent FOREIGN KEY (assigned_agent_id) REFERENCES agents(id),
            CONSTRAINT fk_subtasks_workspace FOREIGN KEY (workspace_id) REFERENCES organizations(id)
        )
    """))
    print('Created table: team_subtasks')


def _create_indexes(connection):
    _create_index_if_missing(connection, 'agent_role_templates', 'idx_agent_role_templates_workspace',
                             'CREATE INDEX idx_agent_role_templates_workspace ON agent_role_templates (workspace_id)')
    _create_index_if_missing(connection, 'agent_role_templates', 'idx_agent_role_templates_builtin_status',
                             'CREATE INDEX idx_agent_role_templates_builtin_status ON agent_role_templates (is_builtin, status)')
    _create_index_if_missing(connection, 'agent_role_templates', 'idx_agent_role_templates_category',
                             'CREATE INDEX idx_agent_role_templates_category ON agent_role_templates (category)')

    _create_index_if_missing(connection, 'agent_teams', 'idx_agent_teams_workspace_status',
                             'CREATE INDEX idx_agent_teams_workspace_status ON agent_teams (workspace_id, status)')

    _create_index_if_missing(connection, 'agent_team_members', 'idx_agent_team_members_team',
                             'CREATE INDEX idx_agent_team_members_team ON agent_team_members (team_id)')
    _create_index_if_missing(connection, 'agent_team_members', 'idx_agent_team_members_agent',
                             'CREATE INDEX idx_agent_team_members_agent ON agent_team_members (agent_id)')

    _create_index_if_missing(connection, 'agent_team_projects', 'idx_agent_team_projects_team',
                             'CREATE INDEX idx_agent_team_projects_team ON agent_team_projects (team_id)')
    _create_index_if_missing(connection, 'agent_team_projects', 'idx_agent_team_projects_project',
                             'CREATE INDEX idx_agent_team_projects_project ON agent_team_projects (project_id)')

    _create_index_if_missing(connection, 'team_task_orchestrations', 'idx_orchestrations_team_status',
                             'CREATE INDEX idx_orchestrations_team_status ON team_task_orchestrations (team_id, status)')
    _create_index_if_missing(connection, 'team_task_orchestrations', 'idx_orchestrations_task',
                             'CREATE INDEX idx_orchestrations_task ON team_task_orchestrations (task_id)')

    _create_index_if_missing(connection, 'team_subtasks', 'idx_subtasks_orchestration',
                             'CREATE INDEX idx_subtasks_orchestration ON team_subtasks (orchestration_id)')
    _create_index_if_missing(connection, 'team_subtasks', 'idx_subtasks_agent',
                             'CREATE INDEX idx_subtasks_agent ON team_subtasks (assigned_agent_id)')
    _create_index_if_missing(connection, 'team_subtasks', 'idx_subtasks_status',
                             'CREATE INDEX idx_subtasks_status ON team_subtasks (status)')


def upgrade(connection):
    _create_agent_role_templates(connection)
    _create_agent_teams(connection)
    _create_agent_team_members(connection)
    _create_agent_team_projects(connection)
    _create_team_task_orchestrations(connection)
    _create_team_subtasks(connection)
    _create_indexes(connection)


def downgrade(connection):
    _drop_index_if_exists(connection, 'team_subtasks', 'idx_subtasks_status')
    _drop_index_if_exists(connection, 'team_subtasks', 'idx_subtasks_agent')
    _drop_index_if_exists(connection, 'team_subtasks', 'idx_subtasks_orchestration')
    _drop_index_if_exists(connection, 'team_task_orchestrations', 'idx_orchestrations_task')
    _drop_index_if_exists(connection, 'team_task_orchestrations', 'idx_orchestrations_team_status')
    _drop_index_if_exists(connection, 'agent_team_projects', 'idx_agent_team_projects_project')
    _drop_index_if_exists(connection, 'agent_team_projects', 'idx_agent_team_projects_team')
    _drop_index_if_exists(connection, 'agent_team_members', 'idx_agent_team_members_agent')
    _drop_index_if_exists(connection, 'agent_team_members', 'idx_agent_team_members_team')
    _drop_index_if_exists(connection, 'agent_teams', 'idx_agent_teams_workspace_status')
    _drop_index_if_exists(connection, 'agent_role_templates', 'idx_agent_role_templates_category')
    _drop_index_if_exists(connection, 'agent_role_templates', 'idx_agent_role_templates_builtin_status')
    _drop_index_if_exists(connection, 'agent_role_templates', 'idx_agent_role_templates_workspace')

    for table in ['team_subtasks', 'team_task_orchestrations', 'agent_team_projects',
                  'agent_team_members', 'agent_teams', 'agent_role_templates']:
        if _table_exists(connection, table):
            connection.execute(text(f"DROP TABLE {table}"))
            print(f"Dropped table: {table}")
