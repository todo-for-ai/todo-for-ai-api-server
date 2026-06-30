"""Add agent_experiences table for collective intelligence.

Revision: add_agent_experiences
"""

from migrations.utils import create_table, drop_table


def upgrade(engine, metadata):
    create_table(engine, metadata, "agent_experiences", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("agent_id", "Integer", {"nullable": False}),
        ("experience_type", "String(50)", {"nullable": False, "default": "success_pattern"}),
        ("domain", "String(100)", {"nullable": True}),
        ("task_type", "String(100)", {"nullable": True}),
        ("capabilities_used", "JSON", {"default": "list", "nullable": True}),
        ("strategy", "Text", {"nullable": True}),
        ("outcome_pattern", "Text", {"nullable": True}),
        ("key_learnings", "Text", {"nullable": True}),
        ("confidence", "Float", {"default": 0.7, "nullable": True}),
        ("applicability_score", "Float", {"default": 0.5, "nullable": True}),
        ("source_task_id", "Integer", {"nullable": True}),
        ("source_step_key", "String(100)", {"nullable": True}),
        ("source_workflow_run_id", "Integer", {"nullable": True}),
        ("is_shared", "Boolean", {"default": False, "nullable": True}),
        ("project_id", "Integer", {"nullable": True}),
        ("times_reused", "Integer", {"default": 0, "nullable": True}),
        ("last_reused_at", "DateTime", {"nullable": True}),
        ("is_valid", "Boolean", {"default": True, "nullable": True}),
        ("access_count", "Integer", {"default": 0, "nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("agent_id", "agents", "id"),
        ("source_task_id", "tasks", "id"),
        ("project_id", "projects", "id"),
    ], indexes=[
        ("ix_agent_experiences_agent_id", ["agent_id"]),
        ("ix_agent_experiences_domain", ["domain"]),
        ("ix_agent_experiences_experience_type", ["experience_type"]),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "agent_experiences")
