"""Add agent_reputations table.

Revision: add_agent_reputations
"""

from migrations.utils import create_table, drop_table


def upgrade(engine, metadata):
    create_table(engine, metadata, "agent_reputations", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("agent_id", "Integer", {"nullable": False}),
        ("score", "Float", {"nullable": False, "default": 50.0}),
        ("total_tasks", "Integer", {"nullable": False, "default": 0}),
        ("completed_tasks", "Integer", {"nullable": False, "default": 0}),
        ("failed_tasks", "Integer", {"nullable": False, "default": 0}),
        ("avg_completion_time", "Float", {"nullable": True}),
        ("on_time_rate", "Float", {"default": 1.0, "nullable": True}),
        ("quality_score", "Float", {"default": 50.0, "nullable": True}),
        ("last_updated_at", "DateTime", {"nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("agent_id", "agents", "id"),
    ], indexes=[
        ("ix_agent_reputations_agent_id", ["agent_id"]),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "agent_reputations")
