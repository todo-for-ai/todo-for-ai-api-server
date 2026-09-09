"""
Migration: add_storage_hotpath_indexes
Description: 存储层热路径复合索引（对照活库 EXPLAIN 缺口）：
             - agent_task_leases(workspace_id, active, expires_at)：
               工作区「正在干活」水位门禁（每次 pull 都执行）原先只能走
               expires_at 范围扫描 + 临时表去重；
             - agent_task_leases(agent_id, active, expires_at)：
               Agent 在岗租约计数 / budget concurrent 资源用量；
             - agent_heartbeats(agent_id, created_at)：
               get_latest_by_agent 原先全表扫描 + filesort（表随心跳无限增长）。
Created: 2026-09-10
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402

# (表名, 索引名, 列)
_INDEXES = [
    ("agent_task_leases", "idx_leases_workspace_active_exp",
     ("workspace_id", "active", "expires_at", "agent_id")),
    ("agent_task_leases", "idx_leases_agent_active_exp",
     ("agent_id", "active", "expires_at")),
    ("agent_heartbeats", "idx_agent_heartbeats_agent_created",
     ("agent_id", "created_at")),
]


def _index_exists(connection, table_name, index_name):
    dialect = connection.dialect.name
    if dialect == "mysql":
        row = connection.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t "
                "AND index_name = :i"
            ),
            {"t": table_name, "i": index_name},
        ).first()
        return bool(row and row[0])
    rows = connection.execute(
        db.text(f"PRAGMA index_list({table_name})")
    ).fetchall()
    return any(row[1] == index_name for row in rows)


def upgrade(connection):
    dialect = connection.dialect.name
    for table_name, index_name, columns in _INDEXES:
        if _index_exists(connection, table_name, index_name):
            print(f"⏭️  索引 {index_name} 已存在，跳过")
            continue
        cols = ", ".join(columns)
        print(f"➕ 添加索引 {index_name} ON {table_name}({cols}) ...")
        connection.execute(
            db.text(f"CREATE INDEX {index_name} ON {table_name} ({cols})")
        )
    print("✅ 存储层热路径索引就绪")


def downgrade(connection):
    dialect = connection.dialect.name
    for table_name, index_name, _columns in _INDEXES:
        if not _index_exists(connection, table_name, index_name):
            continue
        if dialect == "mysql":
            connection.execute(db.text(f"DROP INDEX {index_name} ON {table_name}"))
        else:
            connection.execute(db.text(f"DROP INDEX {index_name}"))
    print("✅ 存储层热路径索引已回滚")
