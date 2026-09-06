"""
Migration: add_goal_loop_director
Description: goal_loops 增加 director_agent_id——多 Agent 编排：指挥者负责
             目标拆解与评审，执行者按计划步骤声明的岗位要求从工作区 Agent
             池路由。director 为空时退回绑定 Agent（单 Agent 模式，向后兼容）。
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
    if not _table_exists(connection, "goal_loops"):
        print("⏭️  表 goal_loops 不存在，跳过")
        return
    if _column_exists(connection, "goal_loops", "director_agent_id"):
        print("⏭️  列 goal_loops.director_agent_id 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 添加列 goal_loops.director_agent_id ...")
    if dialect == "mysql":
        connection.execute(
            db.text(
                "ALTER TABLE goal_loops ADD COLUMN director_agent_id INT NULL "
                "COMMENT '指挥者Agent ID（规划+评审），NULL=退回绑定Agent' AFTER agent_id"
            )
        )
        connection.execute(
            db.text("CREATE INDEX ix_goal_loops_director_agent_id ON goal_loops (director_agent_id)")
        )
        connection.execute(
            db.text(
                "ALTER TABLE goal_loops "
                "ADD CONSTRAINT fk_goal_loops_director FOREIGN KEY (director_agent_id) "
                "REFERENCES agents (id)"
            )
        )
    else:
        connection.execute(
            db.text(
                "ALTER TABLE goal_loops ADD COLUMN director_agent_id INTEGER "
                "REFERENCES agents (id)"
            )
        )
        connection.execute(
            db.text("CREATE INDEX ix_goal_loops_director_agent_id ON goal_loops (director_agent_id)")
        )
    print("✅ director_agent_id 列就绪")


def downgrade(connection):
    dialect = connection.dialect.name
    if _table_exists(connection, "goal_loops") and _column_exists(
        connection, "goal_loops", "director_agent_id"
    ):
        if dialect == "mysql":
            connection.execute(
                db.text("ALTER TABLE goal_loops DROP FOREIGN KEY fk_goal_loops_director")
            )
            connection.execute(
                db.text("ALTER TABLE goal_loops DROP INDEX ix_goal_loops_director_agent_id")
            )
        else:
            connection.execute(db.text("DROP INDEX IF EXISTS ix_goal_loops_director_agent_id"))
        connection.execute(
            db.text("ALTER TABLE goal_loops DROP COLUMN director_agent_id")
        )
