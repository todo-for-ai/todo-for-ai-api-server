"""
Migration: add_trigger_actions
Description: agent_triggers 增加动作字段——cron 触发器到点后除了派 Agent
             （run_agent）还能定时自动创建任务（create_task，action_payload
             携带任务模板）；last_fired_key 为 create_task 动作的幂等键。
Created: 2026-09-10
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402
from migrations.add_goal_loop_time_budget import _column_exists  # noqa: E402  (reuse helper)
from migrations.add_workspace_runtime_settings import _table_exists  # noqa: E402  (reuse helper)


def upgrade(connection):
    if not _table_exists(connection, "agent_triggers"):
        print("⏭️  表 agent_triggers 不存在，跳过")
        return

    dialect = connection.dialect.name

    if not _column_exists(connection, "agent_triggers", "action"):
        print("➕ 添加列 agent_triggers.action ...")
        if dialect == "mysql":
            connection.execute(
                db.text(
                    "ALTER TABLE agent_triggers ADD COLUMN action VARCHAR(16) NOT NULL "
                    "DEFAULT 'run_agent' COMMENT '触发动作: run_agent/create_task' "
                    "AFTER dedup_window_seconds"
                )
            )
        else:
            connection.execute(
                db.text(
                    "ALTER TABLE agent_triggers ADD COLUMN action VARCHAR(16) "
                    "NOT NULL DEFAULT 'run_agent'"
                )
            )

    if not _column_exists(connection, "agent_triggers", "action_payload"):
        print("➕ 添加列 agent_triggers.action_payload ...")
        if dialect == "mysql":
            connection.execute(
                db.text(
                    "ALTER TABLE agent_triggers ADD COLUMN action_payload JSON NULL "
                    "COMMENT '动作参数（create_task: project_id/title/description/priority/tags）' "
                    "AFTER action"
                )
            )
        else:
            connection.execute(
                db.text("ALTER TABLE agent_triggers ADD COLUMN action_payload JSON")
            )

    if not _column_exists(connection, "agent_triggers", "last_fired_key"):
        print("➕ 添加列 agent_triggers.last_fired_key ...")
        if dialect == "mysql":
            connection.execute(
                db.text(
                    "ALTER TABLE agent_triggers ADD COLUMN last_fired_key VARCHAR(80) NULL "
                    "COMMENT 'create_task 动作的幂等键（最近一次触发）' "
                    "AFTER action_payload"
                )
            )
        else:
            connection.execute(
                db.text("ALTER TABLE agent_triggers ADD COLUMN last_fired_key VARCHAR(80)")
            )
    print("✅ agent_triggers 动作字段就绪")


def downgrade(connection):
    if not _table_exists(connection, "agent_triggers"):
        return
    for column in ("last_fired_key", "action_payload", "action"):
        if _column_exists(connection, "agent_triggers", column):
            connection.execute(
                db.text(f"ALTER TABLE agent_triggers DROP COLUMN {column}")
            )
