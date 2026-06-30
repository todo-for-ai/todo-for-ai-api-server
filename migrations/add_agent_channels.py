"""
Add Agent Collaboration Channels tables.

Creates:
- agent_channels: collaboration channels for multi-agent discussion
- agent_channel_members: agent membership in channels
- agent_channel_messages: messages in channels

Compatible with MySQL, PostgreSQL, and SQLite.
"""

import sqlalchemy as sa
from alembic import op

revision = "add_agent_channels"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # --- agent_channels ---
    op.create_table(
        "agent_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False, comment="Channel display name"),
        sa.Column("description", sa.Text(), nullable=True, comment="Channel description/purpose"),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True, index=True, comment="Project scope"),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("tasks.id"), nullable=True, index=True, comment="Task scope"),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True, comment="Channel creator"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1", comment="Whether the channel is active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # --- agent_channel_members ---
    op.create_table(
        "agent_channel_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("agent_channels.id"), nullable=False, index=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column("role", sa.String(20), server_default="member", comment="Channel role: owner, member"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # --- agent_channel_messages ---
    op.create_table(
        "agent_channel_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("agent_channels.id"), nullable=False, index=True),
        sa.Column("sender_agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=True, index=True),
        sa.Column("sender_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("content", sa.Text(), nullable=False, comment="Message content"),
        sa.Column("message_type", sa.String(50), server_default="text", comment="Message type"),
        sa.Column("metadata", sa.JSON(), nullable=True, comment="Extra structured metadata"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade():
    op.drop_table("agent_channel_messages")
    op.drop_table("agent_channel_members")
    op.drop_table("agent_channels")
