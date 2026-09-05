"""
Migration: add_user_settings_theme
Description: user_settings 增加 theme 列——像素皮肤系统到人级别的持久化存储。
             每个用户选定的界面皮肤（色板 id，如 sky/fc/gameboy）落库，
             登录后跨设备/跨会话跟随；默认 sky 不改变存量用户观感。
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
                "WHERE table_schema = DATABASE() AND table_name = :table_name "
                "AND column_name = :column_name"
            ),
            {"table_name": table_name, "column_name": column_name},
        ).first()
    else:
        row = connection.execute(
            db.text("PRAGMA table_info(:table_name)"),
            {"table_name": table_name},
        ).fetchall()
        return any(row[1] == column_name for row in row)
    return bool(row and row[0])


def upgrade(connection):
    if not _table_exists(connection, "user_settings"):
        print("⏭️  表 user_settings 不存在，跳过")
        return
    if _column_exists(connection, "user_settings", "theme"):
        print("⏭️  列 user_settings.theme 已存在，跳过")
        return

    print("➕ 添加列 user_settings.theme ...")
    connection.execute(
        db.text(
            "ALTER TABLE user_settings ADD COLUMN theme VARCHAR(32) "
            "NOT NULL DEFAULT 'sky' COMMENT '界面皮肤 ID'"
        )
    )
    print("✅ user_settings.theme 添加完成")


def downgrade(connection):
    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(
            db.text("ALTER TABLE user_settings DROP COLUMN theme")
        )
    # SQLite 不支持 DROP COLUMN（老版本），降级场景罕见，跳过
