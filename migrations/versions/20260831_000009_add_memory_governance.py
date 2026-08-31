"""
Migration: add_memory_governance
Description: P3.4 记忆治理 - agent_soul_versions 增加 memory_kind/snapshot_json，唯一约束放宽到 (agent_id, memory_kind, version)。
Created: 2026-08-31
"""

from migrations.add_memory_governance import upgrade, downgrade  # noqa: F401
