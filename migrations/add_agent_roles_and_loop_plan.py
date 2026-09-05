"""
Migration: add_agent_roles_and_loop_plan
Description: ① agents.role_template_id——Agent 实例绑定岗位角色（复用既有
             agent_role_templates 内置岗位：pm/developer/qa 等），让"角色"
             从模板层落到具体 Agent，角色信息随 overview/planner 传播；
             ② goal_loops 计划式拆解——plan（拆解出的有序步骤）、plan_index
             （当前执行到第几步）、plan_revision（计划重排次数），GoalLoop
             从"走一步看一步"升级为"先拆解目标成计划再逐轮执行"。
Created: 2026-09-06
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402
from migrations.add_budgets import _table_exists  # noqa: E402  (reuse helper)


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t "
                "AND column_name = :c"
            ),
            {"t": table_name, "c": column_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text("PRAGMA table_info(:t)"), {"t": table_name}
    ).fetchall()
    return any(r[1] == column_name for r in rows)


def upgrade(connection):
    dialect = connection.dialect.name

    # ── ① agents.role_template_id ──
    if _table_exists(connection, "agents") and not _column_exists(
        connection, "agents", "role_template_id"
    ):
        print("➕ 添加列 agents.role_template_id ...")
        connection.execute(
            db.text(
                "ALTER TABLE agents ADD COLUMN role_template_id INT NULL "
                "COMMENT '岗位角色模板ID（agent_role_templates）'"
            )
        )
        connection.execute(
            db.text(
                "ALTER TABLE agents ADD CONSTRAINT fk_agents_role_template "
                "FOREIGN KEY (role_template_id) REFERENCES agent_role_templates (id)"
            )
            if dialect == "mysql"
            else db.text("SELECT 1")
        )
        connection.execute(
            db.text("CREATE INDEX ix_agents_role_template_id ON agents (role_template_id)")
        )
    else:
        print("⏭️  agents.role_template_id 已存在或表缺失，跳过")

    # ── ② goal_loops 计划列 ──
    if not _table_exists(connection, "goal_loops"):
        print("⏭️  表 goal_loops 不存在，跳过循环列")
        return
    for column, ddl in [
        ("plan", "JSON NULL COMMENT '拆解出的有序计划步骤 [{title,content}]'"),
        ("plan_index", "INT NOT NULL DEFAULT 0 COMMENT '下一个待执行步骤下标'"),
        ("plan_revision", "INT NOT NULL DEFAULT 0 COMMENT '计划重排次数'"),
    ]:
        if _column_exists(connection, "goal_loops", column):
            print(f"⏭️  列 goal_loops.{column} 已存在，跳过")
            continue
        print(f"➕ 添加列 goal_loops.{column} ...")
        if dialect == "mysql":
            mysql_ddl = ddl.replace("JSON NULL", "JSON NULL", 1)
            connection.execute(
                db.text(f"ALTER TABLE goal_loops ADD COLUMN {column} {mysql_ddl}")
            )
        else:
            sqlite_type = "TEXT" if "JSON" in ddl else "INTEGER NOT NULL DEFAULT 0"
            connection.execute(
                db.text(f"ALTER TABLE goal_loops ADD COLUMN {column} {sqlite_type}")
            )
    print("✅ goal_loops 计划列就绪")


def downgrade(connection):
    if _table_exists(connection, "goal_loops"):
        for column in ("plan", "plan_index", "plan_revision"):
            if _column_exists(connection, "goal_loops", column):
                connection.execute(
                    db.text(f"ALTER TABLE goal_loops DROP COLUMN {column}")
                )
    # agents.role_template_id 保留（绑定关系无害）
