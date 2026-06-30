"""Add condition column to workflow_steps.

Revision: add_step_condition
"""

from migrations.utils import create_table, drop_table, add_column, drop_column


def upgrade(engine, metadata):
    add_column(engine, metadata, "workflow_steps", "condition", "JSON", nullable=True)


def downgrade(engine, metadata):
    drop_column(engine, metadata, "workflow_steps", "condition")
