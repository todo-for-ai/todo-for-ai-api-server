"""
Migration: add_expired_agent_states
Description: Ensure Agent assignment/run enums include EXPIRED.
Created: 2026-06-25T00:00:01
"""

from migrations.add_agent_collaboration import ensure_agent_enum_values


def upgrade(connection):
    """Ensure native database enums accept expired Agent states."""
    ensure_agent_enum_values(connection)


def downgrade(connection):
    """Do not remove enum values; existing rows may already use EXPIRED."""
    pass
