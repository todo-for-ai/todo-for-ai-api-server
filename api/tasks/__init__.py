"""Tasks API package."""

from flask import Blueprint

from .constants import tasks_list_fallback_cache

tasks_bp = Blueprint('tasks', __name__)

# Ensure route decorators register on blueprint import.
from . import routes_tasks  # noqa: E402,F401
from . import routes_attachments  # noqa: E402,F401

__all__ = ['tasks_bp', 'tasks_list_fallback_cache']
