"""Add collaboration_protocols and protocol_messages tables.

Revision: add_collaboration_protocols
"""

from migrations.utils import create_table, drop_table


def upgrade(engine, metadata):
    create_table(engine, metadata, "collaboration_protocols", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("protocol_type", "String(30)", {"nullable": False}),
        ("status", "String(20)", {"nullable": False, "default": "open"}),
        ("title", "String(500)", {"nullable": False}),
        ("description", "Text", {"nullable": True}),
        ("initiator_agent_id", "Integer", {"nullable": False}),
        ("channel_id", "Integer", {"nullable": True}),
        ("project_id", "Integer", {"nullable": True}),
        ("task_id", "Integer", {"nullable": True}),
        ("config", "JSON", {"nullable": True}),
        ("result", "JSON", {"nullable": True}),
        ("deadline", "DateTime", {"nullable": True}),
        ("resolved_at", "DateTime", {"nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("initiator_agent_id", "agents", "id"),
        ("channel_id", "agent_channels", "id"),
        ("project_id", "projects", "id"),
        ("task_id", "tasks", "id"),
    ], indexes=[
        ("ix_collab_protocols_type", ["protocol_type"]),
        ("ix_collab_protocols_status", ["status"]),
    ])

    create_table(engine, metadata, "protocol_messages", [
        ("id", "Integer", {"primary_key": True, "autoincrement": True}),
        ("protocol_id", "Integer", {"nullable": False}),
        ("agent_id", "Integer", {"nullable": False}),
        ("message_type", "String(30)", {"nullable": False}),
        ("content", "Text", {"nullable": True}),
        ("payload", "JSON", {"nullable": True}),
        ("created_at", "DateTime", {"nullable": False}),
        ("updated_at", "DateTime", {"nullable": False}),
    ], foreign_keys=[
        ("protocol_id", "collaboration_protocols", "id"),
        ("agent_id", "agents", "id"),
    ], indexes=[
        ("ix_protocol_messages_protocol_id", ["protocol_id"]),
    ])


def downgrade(engine, metadata):
    drop_table(engine, metadata, "protocol_messages")
    drop_table(engine, metadata, "collaboration_protocols")
