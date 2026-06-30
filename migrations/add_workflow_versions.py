"""Add workflow_versions table.

Revision: add_workflow_versions
"""

from migrations.utils import create_table, drop_table


def upgrade(engine, metadata):
    create_table(engine, metadata, "workflow_versions", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("workflow_id", "Integer", {"nullable": False}),
        ("version_number", "Integer", {"nullable": False}),
        ("definition", "JSON", {"nullable": False}),
        ("steps_snapshot", "JSON", {"nullable": False}),
        ("change_summary", "Text", {"nullable": True}),
        ("created_by", "String(255)", {"nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("workflow_id", "workflows", "id"),
    ], indexes=[
        ("ix_workflow_versions_workflow_id", ["workflow_id"]),
        ("ix_workflow_versions_version_number", ["workflow_id", "version_number"]),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "workflow_versions")
