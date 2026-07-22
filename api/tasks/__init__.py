"""
Tasks API blueprint package.

CRUD routes live in ``crud`` and analytics routes in ``task_analytics``;
each imports ``tasks_bp`` from this package and registers its own routes
as an import side-effect.
"""

from flask import Blueprint

tasks_bp = Blueprint("tasks", __name__)

# ── Submodule imports (side-effect: registers routes on tasks_bp) ──
from . import crud  # noqa: E402,F401
from . import task_analytics  # noqa: E402,F401
