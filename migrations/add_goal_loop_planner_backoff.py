"""
Migration: add_goal_loop_planner_backoff
Description: goal_loops 增加 transient_streak / retry_after 两列——规划器
             瞬时故障（LLM 调用层超时/5xx/限流）按指数退避自动重试，
             不烧语义受阻预算；退避截止时间之前推进请求直接跳过。
Created: 2026-09-13
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402

_COLUMNS = [
    ("transient_streak",
     "ALTER TABLE goal_loops ADD COLUMN {col} INT NOT NULL DEFAULT 0 "
     "COMMENT '连续瞬时规划器故障次数'",
     "ALTER TABLE goal_loops ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"),
    ("retry_after",
     "ALTER TABLE goal_loops ADD COLUMN {col} DATETIME NULL "
     "COMMENT '瞬时故障退避截止时间（UTC），NULL=不在退避中'",
     "ALTER TABLE goal_loops ADD COLUMN {col} DATETIME NULL"),
]


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
    for col, mysql_sql, sqlite_sql in _COLUMNS:
        if _column_exists(connection, "goal_loops", col):
            print(f"⏭️  列 goal_loops.{col} 已存在，跳过")
            continue
        print(f"➕ 添加列 goal_loops.{col} ...")
        connection.execute(db.text(
            (mysql_sql if dialect == "mysql" else sqlite_sql).format(col=col)
        ))
    print("✅ goal_loops 规划器退避列就绪")


def downgrade(connection):
    for col, _, _ in _COLUMNS:
        if _column_exists(connection, "goal_loops", col):
            connection.execute(db.text(f"ALTER TABLE goal_loops DROP COLUMN {col}"))
