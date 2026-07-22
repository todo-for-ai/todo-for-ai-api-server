"""
Shared imports for the tasks blueprint package.

Centralizes models, auth, and base API helpers so CRUD and analytics
submodules import from a single hub (mirrors the agents/._shared pattern).
"""

from datetime import datetime, timedelta

from flask import Blueprint, request
from sqlalchemy import func

from models import (
    db, Task, TaskStatus, TaskPriority, Project, TaskHistory,
    ActionType, UserActivity,
)
from ..base import (
    ApiResponse, paginate_query, validate_json_request,
    get_request_args, APIException, handle_api_error,
)
from core.auth import unified_auth_required, get_current_user

# The blueprint is defined in the package __init__ and re-exported here so
# submodules can decorate routes onto it via ``from ._shared import tasks_bp``.
from . import tasks_bp  # noqa: F401,E402

__all__ = [
    "tasks_bp", "datetime", "timedelta", "request", "func",
    "db", "Task", "TaskStatus", "TaskPriority", "Project", "TaskHistory",
    "ActionType", "UserActivity",
    "ApiResponse", "paginate_query", "validate_json_request",
    "get_request_args", "APIException", "handle_api_error",
    "unified_auth_required", "get_current_user",
]
