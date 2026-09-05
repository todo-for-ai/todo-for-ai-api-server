"""
Migration: add_goal_loops
Description: GoalLoop 目标循环表——某 agent 在某项目上朝一个目标逐轮做任务，
             由平台驱动器在任务终态后自动推进下一轮（规划器决定 continue/
             complete/blocked），带轮数上限、连续受阻、人工暂停三重护栏。
Created: 2026-09-06
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402
from migrations.add_budgets import _table_exists  # noqa: E402  (reuse helper)


def upgrade(connection):
    if _table_exists(connection, "goal_loops"):
        print("⏭️  表 goal_loops 已存在，跳过")
        return

    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(db.text("""
            CREATE TABLE goal_loops (
                id INT AUTO_INCREMENT PRIMARY KEY,
                workspace_id INT NOT NULL,
                project_id INT NOT NULL,
                agent_id INT NOT NULL,
                title VARCHAR(500) NOT NULL,
                goal_text TEXT NOT NULL,
                done_definition TEXT NULL,
                status ENUM('RUNNING','PAUSED','DONE','LIMIT_REACHED','STALLED','STOPPED')
                    NOT NULL DEFAULT 'RUNNING',
                advancing INT NOT NULL DEFAULT 0,
                rounds_limit INT NOT NULL DEFAULT 10,
                stall_limit INT NOT NULL DEFAULT 2,
                stall_count INT NOT NULL DEFAULT 0,
                last_error TEXT NULL,
                completion_summary TEXT NULL,
                last_task_id INT NULL,
                started_at DATETIME NULL,
                finished_at DATETIME NULL,
                created_by INT NULL,
                created_at DATETIME NULL,
                updated_at DATETIME NULL,
                CONSTRAINT fk_goal_loops_project FOREIGN KEY (project_id) REFERENCES projects (id),
                CONSTRAINT fk_goal_loops_agent FOREIGN KEY (agent_id) REFERENCES agents (id),
                CONSTRAINT fk_goal_loops_workspace FOREIGN KEY (workspace_id) REFERENCES organizations (id)
            )
        """))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_workspace_id ON goal_loops (workspace_id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_project_id ON goal_loops (project_id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_agent_id ON goal_loops (agent_id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_status ON goal_loops (status)"
        ))
    else:
        connection.execute(db.text("""
            CREATE TABLE goal_loops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                title VARCHAR(500) NOT NULL,
                goal_text TEXT NOT NULL,
                done_definition TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'running',
                advancing INTEGER NOT NULL DEFAULT 0,
                rounds_limit INTEGER NOT NULL DEFAULT 10,
                stall_limit INTEGER NOT NULL DEFAULT 2,
                stall_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                completion_summary TEXT,
                last_task_id INTEGER,
                started_at DATETIME,
                finished_at DATETIME,
                created_by INTEGER,
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_workspace_id ON goal_loops (workspace_id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_project_id ON goal_loops (project_id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_agent_id ON goal_loops (agent_id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_status ON goal_loops (status)"
        ))
    print("✅ 表 goal_loops 创建完成")


def downgrade(connection):
    if _table_exists(connection, "goal_loops"):
        connection.execute(db.text("DROP TABLE goal_loops"))
