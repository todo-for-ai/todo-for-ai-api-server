"""
Migration: add_role_template_industry
Description: agent_role_templates 增加 industry 列——岗位角色体系按行业维度
             扩展（120+ 行业 × 各行业专属工种 + 职能岗，覆盖 5000+ 工种）。
             industry 为空表示跨行业通用模板。
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
    if not _table_exists(connection, "agent_role_templates"):
        print("⏭️  表 agent_role_templates 不存在，跳过")
        return
    if _column_exists(connection, "agent_role_templates", "industry"):
        print("⏭️  列 agent_role_templates.industry 已存在，跳过")
        return

    dialect = connection.dialect.name
    print("➕ 添加列 agent_role_templates.industry ...")
    if dialect == "mysql":
        connection.execute(
            db.text(
                "ALTER TABLE agent_role_templates ADD COLUMN industry VARCHAR(64) NULL "
                "COMMENT '所属行业（空=跨行业通用）' AFTER category"
            )
        )
        connection.execute(
            db.text("CREATE INDEX ix_art_industry ON agent_role_templates (industry)")
        )
    else:
        connection.execute(
            db.text("ALTER TABLE agent_role_templates ADD COLUMN industry VARCHAR(64)")
        )
        connection.execute(
            db.text("CREATE INDEX ix_art_industry ON agent_role_templates (industry)")
        )
    print("✅ industry 列就绪")


def downgrade(connection):
    dialect = connection.dialect.name
    if _table_exists(connection, "agent_role_templates") and _column_exists(
        connection, "agent_role_templates", "industry"
    ):
        if dialect == "mysql":
            connection.execute(
                db.text("ALTER TABLE agent_role_templates DROP INDEX ix_art_industry")
            )
        else:
            connection.execute(db.text("DROP INDEX IF EXISTS ix_art_industry"))
        connection.execute(
            db.text("ALTER TABLE agent_role_templates DROP COLUMN industry")
        )
