"""
Migration: add_task_parent_indexes
Description: 补齐 tasks.parent_task_id 缺失的索引（模型声明了 index=True 但历史建表未生效，
             导致子任务 COUNT 每次全表扫描），并增加 (owner_id, project_id, created_at)
             组合索引优化项目任务列表页（owner 快路径 + 项目过滤 + created_at 排序）。
Created: 2026-09-02
"""

from migrations.add_budgets import _table_exists  # noqa: F401  (reuse helper)


def _index_exists(connection, table_name, index_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :table_name "
                "AND index_name = :index_name"
            ),
            {"table_name": table_name, "index_name": index_name},
        ).first()
    else:
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                "AND name = :index_name AND tbl_name = :table_name"
            ),
            {"table_name": table_name, "index_name": index_name},
        ).first()
    return bool(row and row[0])


import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


_INDEXES = [
    # (索引名, 列) —— parent_task_id 是子任务 COUNT 的过滤列，缺失时为千万行全表扫描
    ("idx_tasks_parent_task_id", "parent_task_id"),
    # 项目任务列表页的典型查询：owner 快路径 + project_id 过滤 + created_at 排序
    ("idx_tasks_owner_project_created_at", "owner_id, project_id, created_at"),
]


def upgrade(connection):
    if not _table_exists(connection, "tasks"):
        return

    for index_name, columns in _INDEXES:
        if _index_exists(connection, "tasks", index_name):
            print(f"⏭️  索引 {index_name} 已存在，跳过")
            continue
        print(f"➕ 创建索引 tasks.{index_name} ({columns}) ...")
        connection.execute(
            db.text(f"ALTER TABLE tasks ADD INDEX {index_name} ({columns})")
        )
        print(f"✅ 索引 {index_name} 创建完成")


def downgrade(connection):
    if not _table_exists(connection, "tasks"):
        return

    for index_name, _columns in _INDEXES:
        if not _index_exists(connection, "tasks", index_name):
            continue
        print(f"➖ 删除索引 tasks.{index_name} ...")
        connection.execute(db.text(f"ALTER TABLE tasks DROP INDEX {index_name}"))
        print(f"✅ 索引 {index_name} 已删除")
