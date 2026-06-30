"""
Add max_parallel_steps column to workflows table.

Compatible with MySQL, PostgreSQL, and SQLite.
"""

import sqlalchemy as sa
from alembic import op

revision = "add_max_parallel_steps"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.add_column(
        "workflows",
        sa.Column("max_parallel_steps", sa.Integer(), server_default="0", nullable=True, comment="Max steps running concurrently (0 = unlimited)"),
    )


def downgrade():
    op.drop_column("workflows", "max_parallel_steps")
