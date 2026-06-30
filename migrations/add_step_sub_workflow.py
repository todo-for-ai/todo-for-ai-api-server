"""Add sub_workflow_id column to workflow_steps.

Revision: add_step_sub_workflow
"""

from migrations.utils import add_column, drop_column


def upgrade(engine, metadata):
    add_column(engine, metadata, "workflow_steps", "sub_workflow_id", "Integer", nullable=True,
               foreign_key=("sub_workflow_id", "workflows", "id"))


def downgrade(engine, metadata):
    drop_column(engine, metadata, "workflow_steps", "sub_workflow_id")
