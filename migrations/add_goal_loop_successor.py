"""
Migration: add_goal_loop_successor
Description: goal_loops 增加 successor_loop_id 列——目标链式接续：本循环
             到终态（done/limit_reached/stalled/stopped）后自动唤醒后继
             循环（PAUSED→RUNNING），Agent 不因单目标完成而闲置。
Created: 2026-09-13
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


def _table_exists(connection, table_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table_name},
        ).first()
        return bool(row and row[0])
    row = connection.execute(
        db.text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name},
    ).first()
    return bool(row and row[0])


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
    if not _table_exists(connection, "goal_loops"):
        print("⏭️  表 goal_loops 不存在，跳过")
        return
    if _column_exists(connection, "goal_loops", "successor_loop_id"):
        print("⏭️  列 goal_loops.successor_loop_id 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 添加列 goal_loops.successor_loop_id ...")
    if dialect == "mysql":
        connection.execute(db.text(
            "ALTER TABLE goal_loops ADD COLUMN successor_loop_id INT NULL "
            "COMMENT '后继循环ID（本循环终态后自动接续）' AFTER plan_revision"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_successor_loop_id ON goal_loops (successor_loop_id)"
        ))
        connection.execute(db.text(
            "ALTER TABLE goal_loops "
            "ADD CONSTRAINT fk_goal_loops_successor FOREIGN KEY (successor_loop_id) "
            "REFERENCES goal_loops (id)"
        ))
    else:
        connection.execute(db.text(
            "ALTER TABLE goal_loops ADD COLUMN successor_loop_id INTEGER "
            "REFERENCES goal_loops (id)"
        ))
        connection.execute(db.text(
            "CREATE INDEX ix_goal_loops_successor_loop_id ON goal_loops (successor_loop_id)"
        ))
    print("✅ goal_loops.successor_loop_id 就绪")


def downgrade(connection):
    if not _column_exists(connection, "goal_loops", "successor_loop_id"):
        return
    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(db.text(
            "ALTER TABLE goal_loops DROP FOREIGN KEY fk_goal_loops_successor"
        ))
        connection.execute(db.text(
            "ALTER TABLE goal_loops DROP COLUMN successor_loop_id"
        ))
    else:
        # SQLite 的 DROP COLUMN 不允许列参与 FK 定义（表级 schema 残留引用），
        # 完整重建表代价高且回滚极少使用——best-effort，失败仅告警
        try:
            connection.execute(db.text(
                "ALTER TABLE goal_loops DROP COLUMN successor_loop_id"
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  SQLite 回滚 goal_loops.successor_loop_id 失败（可忽略）: {exc}")
