"""Add cross_project_agents table for multi-project Agent collaboration.

Revision: add_cross_project_agents
"""

from migrations.utils import create_table, drop_table


def upgrade(engine, metadata):
    create_table(engine, metadata, "cross_project_agents", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("agent_id", "Integer", {"nullable": False}),
        ("project_id", "Integer", {"nullable": False}),
        ("authorized_by", "Integer", {"nullable": True}),
        ("role_in_project", "String(50)", {"default": "contributor", "nullable": True}),
        ("capabilities_override", "JSON", {"nullable": True}),
        ("max_concurrent_tasks", "Integer", {"default": 3, "nullable": True}),
        ("is_active", "Boolean", {"default": True, "nullable": True}),
        ("expires_at", "DateTime", {"nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("agent_id", "agents", "id"),
        ("project_id", "projects", "id"),
        ("authorized_by", "users", "id"),
    ], indexes=[
        ("ix_cross_project_agents_agent_id", ["agent_id"]),
        ("ix_cross_project_agents_project_id", ["project_id"]),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "cross_project_agents")
