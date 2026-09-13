"""
Migration: add_goal_loop_context_digest
Description: goal_loops 增加上下文压缩列——
             context_digest（滚动压缩的历史轮次摘要，注入后续轮次的
             执行者上下文走廊）与 context_digest_upto（摘要已覆盖到的
             最后一个轮次任务 ID，用于增量压缩与幂等）。
Created: 2026-09-13
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402

_COLUMNS = (
    ("context_digest", "TEXT"),
    ("context_digest_upto", "INT"),
)


def _column_exists(connection, table_name, column_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
            ),
            {"t": table_name, "c": column_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text(f"PRAGMA table_info({table_name})")
    ).fetchall()
    return any(row[1] == column_name for row in rows)


def upgrade(connection):
    dialect = connection.dialect.name
    for name, mysql_type in _COLUMNS:
        if _column_exists(connection, "goal_loops", name):
            print(f"⏭️  列 goal_loops.{name} 已存在，跳过")
            continue
        print(f"➕ 添加列 goal_loops.{name} ...")
        if dialect == "mysql":
            comment = {
                "context_digest": "滚动压缩的历史轮次摘要（上下文走廊的压缩层）",
                "context_digest_upto": "摘要已覆盖到的最后一个轮次任务 ID",
            }[name]
            connection.execute(
                db.text(
                    f"ALTER TABLE goal_loops ADD COLUMN {name} {mysql_type} NULL COMMENT '{comment}'"
                )
            )
        else:
            connection.execute(
                db.text(f"ALTER TABLE goal_loops ADD COLUMN {name} {mysql_type}")
            )
    print("✅ goal_loops 上下文压缩列就绪")


def downgrade(connection):
    for name, _ in _COLUMNS:
        if _column_exists(connection, "goal_loops", name):
            connection.execute(
                db.text(f"ALTER TABLE goal_loops DROP COLUMN {name}")
            )
