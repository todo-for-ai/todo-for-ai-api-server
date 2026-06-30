"""Add collaboration_templates table.

Revision: add_collaboration_templates
"""

from migrations.utils import create_table, drop_table, add_column, drop_column


def upgrade(engine, metadata):
    create_table(engine, metadata, "collaboration_templates", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("owner_id", "Integer", {"nullable": False}),
        ("name", "String(200)", {"nullable": False}),
        ("description", "Text", {"nullable": True}),
        ("category", "String(100)", {"nullable": True}),
        ("agent_specs", "JSON", {"nullable": True}),
        ("workflow_id", "Integer", {"nullable": True}),
        ("is_builtin", "Boolean", {"default": False, "nullable": False}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("owner_id", "users", "id"),
        ("workflow_id", "workflows", "id"),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "collaboration_templates")
