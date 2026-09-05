"""
Migration: backfill_audit_project_ids
Description: 回填 agent_audit_events 的 task_id / project_id。写入方此前从不填这两列
             （全库为 NULL），项目详情页「最近动态」按 project_id/task_id 召回时无数据
             可用。target_type='task' 的事件其 target_id 即任务 ID，按 tasks 主键 join
             回填两列；引用已删除任务的行保持 NULL，天然幂等。
Created: 2026-09-05
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402
from migrations.add_budgets import _table_exists  # noqa: E402  (reuse helper)


def upgrade(connection):
    if not _table_exists(connection, "agent_audit_events") or not _table_exists(
        connection, "tasks"
    ):
        print("⏭️  表不存在，跳过")
        return

    pending = connection.execute(
        db.text(
            "SELECT COUNT(*) FROM agent_audit_events "
            "WHERE target_type = 'task' AND task_id IS NULL"
        )
    ).scalar()
    if not pending:
        print("⏭️  无待回填的审计事件，跳过")
        return
    print(f"🔄 回填 {pending} 条审计事件的 task_id / project_id ...")

    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(
            db.text(
                "UPDATE agent_audit_events ae "
                "JOIN tasks t ON t.id = CAST(ae.target_id AS UNSIGNED) "
                "SET ae.task_id = t.id, ae.project_id = t.project_id "
                "WHERE ae.target_type = 'task' AND ae.task_id IS NULL"
            )
        )
    else:
        connection.execute(
            db.text(
                "UPDATE agent_audit_events "
                "SET task_id = CAST(target_id AS INTEGER), "
                "project_id = ("
                "  SELECT t.project_id FROM tasks t "
                "  WHERE t.id = CAST(agent_audit_events.target_id AS INTEGER)"
                ") "
                "WHERE target_type = 'task' AND task_id IS NULL"
            )
        )

    filled = connection.execute(
        db.text(
            "SELECT COUNT(*) FROM agent_audit_events "
            "WHERE target_type = 'task' AND task_id IS NOT NULL"
        )
    ).scalar()
    print(f"✅ 回填完成，task_id 非空事件数: {filled}")


def downgrade(connection):
    # 数据回填不可逆（无法区分回填值与原始值），且回退无业务意义
    pass
