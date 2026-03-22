"""
添加 agent_secrets.key_version 字段

用于支持密钥轮换和多版本密钥管理

Revision ID: 20260322_163000
Revises: 20260321_235100
Create Date: 2026-03-22 16:30:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic
revision = '20260322_163000'
down_revision = '20260321_235100'
branch_labels = None
depends_on = None


def table_has_column(table_name, column_name):
    """检查表是否已有指定列"""
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade():
    # 添加 key_version 字段（如果不存在）
    if not table_has_column('agent_secrets', 'key_version'):
        op.add_column(
            'agent_secrets',
            sa.Column(
                'key_version',
                sa.String(32),
                nullable=False,
                server_default='primary',
                comment='加密密钥版本'
            )
        )

    # 为现有记录设置默认 key_version
    op.execute("UPDATE agent_secrets SET key_version = 'primary' WHERE key_version IS NULL")


def downgrade():
    # 删除 key_version 字段
    if table_has_column('agent_secrets', 'key_version'):
        op.drop_column('agent_secrets', 'key_version')
