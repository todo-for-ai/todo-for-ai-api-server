"""
Migration: add_repo_autonomy_level
Description: Add project_repo_bindings.autonomy_level for progressive PR approval (L0-L2).
Created: 2026-08-31
"""

from migrations.add_repo_autonomy_level import upgrade, downgrade  # noqa: F401
