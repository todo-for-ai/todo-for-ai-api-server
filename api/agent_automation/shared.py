"""Shared helpers for agent automation APIs."""

from datetime import datetime, timedelta

from models import (
    Agent,
    NotificationChannel,
    NotificationScopeType,
    Project,
    Organization,
)
from core.auth import get_current_user
from ..base import ApiResponse
from ..agent_common import now_utc
from ..notification_service import serialize_notification_channel

from .constants import ALLOWED_TASK_EVENTS

def _normalize_int(value, default_value):
    try:
        return int(value)
    except Exception:
        return default_value


def _parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {'1', 'true', 'yes', 'on'}:
            return True
        if lowered in {'0', 'false', 'no', 'off'}:
            return False
    return default


def _get_agent_or_404(workspace_id, agent_id):
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


def _parse_cron_field(field, min_value, max_value):
    values = set()
    token = (field or '').strip()
    if not token:
        return None

    if token == '*':
        return set(range(min_value, max_value + 1))

    for part in token.split(','):
        part = part.strip()
        if not part:
            return None
        if part.startswith('*/'):
            try:
                step = int(part[2:])
            except Exception:
                return None
            if step <= 0:
                return None
            values.update(range(min_value, max_value + 1, step))
            continue

        if '-' in part:
            bounds = part.split('-', 1)
            if len(bounds) != 2:
                return None
            try:
                start = int(bounds[0])
                end = int(bounds[1])
            except Exception:
                return None
            if start > end or start < min_value or end > max_value:
                return None
            values.update(range(start, end + 1))
            continue

        try:
            value = int(part)
        except Exception:
            return None
        if value < min_value or value > max_value:
            return None
        values.add(value)

    return values


def _parse_cron_expr(expr):
    parts = [p.strip() for p in (expr or '').split() if p.strip()]
    if len(parts) != 5:
        return None

    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    dom = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    dow = _parse_cron_field(parts[4], 0, 6)

    if not all([minute, hour, dom, month, dow]):
        return None

    return {
        'minute': minute,
        'hour': hour,
        'dom': dom,
        'month': month,
        'dow': dow,
    }


def _compute_next_fire_at(cron_expr, base_time=None):
    parsed = _parse_cron_expr(cron_expr)
    if not parsed:
        return None

    now = base_time or now_utc()
    # 向上取整到下一分钟
    cursor = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    max_cursor = cursor + timedelta(days=366)

    while cursor <= max_cursor:
        py_weekday = cursor.weekday()  # Monday=0
        cron_weekday = (py_weekday + 1) % 7  # Sunday=0
        if (
            cursor.minute in parsed['minute']
            and cursor.hour in parsed['hour']
            and cursor.day in parsed['dom']
            and cursor.month in parsed['month']
            and cron_weekday in parsed['dow']
        ):
            return cursor
        cursor += timedelta(minutes=1)

    return None


def _validate_task_events(task_event_types):
    if not isinstance(task_event_types, list) or not task_event_types:
        return None
    normalized = []
    for item in task_event_types:
        event_name = str(item or '').strip().lower()
        if event_name not in ALLOWED_TASK_EVENTS:
            return None
        if event_name not in normalized:
            normalized.append(event_name)
    return normalized


def _resolve_channel_scope(scope_type, scope_id):
    user = get_current_user()
    scope_type_value = scope_type.value if hasattr(scope_type, 'value') else str(scope_type or '').strip().lower()

    if scope_type_value == NotificationScopeType.USER.value:
        if user.id != scope_id:
            return None, ApiResponse.forbidden('Access denied').to_response()
        return {'scope_type': scope_type_value, 'scope_id': scope_id}, None

    if scope_type_value == NotificationScopeType.ORGANIZATION.value:
        org = Organization.query.get(scope_id)
        if not org:
            return None, ApiResponse.not_found('Organization not found').to_response()
        if not user.can_access_organization(org):
            return None, ApiResponse.forbidden('Access denied').to_response()
        return {'scope_type': scope_type_value, 'scope_id': scope_id, 'organization': org}, None

    if scope_type_value == NotificationScopeType.PROJECT.value:
        project = Project.query.get(scope_id)
        if not project:
            return None, ApiResponse.not_found('Project not found').to_response()
        if not user.can_access_project(project):
            return None, ApiResponse.forbidden('Access denied').to_response()
        return {'scope_type': scope_type_value, 'scope_id': scope_id, 'project': project}, None

    return None, ApiResponse.error('Unsupported scope type', 400).to_response()


def _can_manage_scope(scope):
    user = get_current_user()
    if scope['scope_type'] == NotificationScopeType.USER.value:
        return user.id == scope['scope_id']
    if scope['scope_type'] == NotificationScopeType.ORGANIZATION.value:
        return user.can_manage_organization(scope.get('organization'))
    if scope['scope_type'] == NotificationScopeType.PROJECT.value:
        return user.can_manage_project(scope.get('project'))
    return False


def _list_channels_by_scope(scope_type, scope_id):
    channels = NotificationChannel.query.filter_by(
        scope_type=scope_type,
        scope_id=scope_id,
    ).order_by(NotificationChannel.updated_at.desc()).all()
    return [serialize_notification_channel(row) for row in channels]
