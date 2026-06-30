"""Add knowledge_entries table.

Revision: add_knowledge_entries
"""

from migrations.utils import create_table, drop_table


def upgrade(engine, metadata):
    create_table(engine, metadata, "knowledge_entries", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("agent_id", "Integer", {"nullable": False}),
        ("title", "String(500)", {"nullable": False}),
        ("content", "Text", {"nullable": False}),
        ("domain", "String(100)", {"nullable": True}),
        ("tags", "JSON", {"nullable": True}),
        ("entry_type", "String(50)", {"default": "insight", "nullable": True}),
        ("source_task_id", "Integer", {"nullable": True}),
        ("source_type", "String(50)", {"default": "manual", "nullable": True}),
        ("confidence", "Float", {"default": 1.0, "nullable": True}),
        ("access_count", "Integer", {"default": 0, "nullable": True}),
        ("is_valid", "Boolean", {"default": True, "nullable": False}),
        ("shared_with_project", "Boolean", {"default": False, "nullable": False}),
        ("project_id", "Integer", {"nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("agent_id", "agents", "id"),
        ("source_task_id", "tasks", "id"),
        ("project_id", "projects", "id"),
    ], indexes=[
        ("ix_knowledge_entries_agent_id", ["agent_id"]),
        ("ix_knowledge_entries_domain", ["domain"]),
        ("ix_knowledge_entries_entry_type", ["entry_type"]),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "knowledge_entries")
