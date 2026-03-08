"""
Agent 自动化配置 API

包含 Runner 配置、触发器管理、运行记录查询、通知 Channel 管理
"""

import hashlib
from datetime import datetime, timedelta
from flask import Blueprint, request
from models import (
    db,
    Agent,
    AgentTrigger,
    AgentTriggerType,
    AgentMisfirePolicy,
    AgentRun,
    AgentRunState,
    NotificationChannel,
    NotificationScopeType,
    NotificationChannelType,
    Project,
    Organization,
)
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request, get_request_args
from .agent_common import ensure_agent_manage_access, now_utc
from .agent_access_control import ensure_agent_detail_access
from .notification_service import (
    SUPPORTED_NOTIFICATION_CHANNEL_TYPES,
    normalize_notification_event_types,
    serialize_notification_channel,
    validate_notification_channel_config,
)


agent_automation_bp = Blueprint('agent_automation', __name__)

ALLOWED_TASK_EVENTS = {'created', 'updated', 'status_changed', 'completed', 'assigned', 'mentioned'}
ALLOWED_EXECUTION_MODES = {'external_pull', 'managed_runner'}


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


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runner-config', methods=['GET'])
@unified_auth_required
def get_runner_config(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    return ApiResponse.success(
        {
            'execution_mode': agent.execution_mode or 'external_pull',
            'runner_enabled': bool(agent.runner_enabled),
            'sandbox_profile': agent.sandbox_profile or 'standard',
            'sandbox_policy': agent.sandbox_policy or {'network_mode': 'whitelist', 'allowed_domains': []},
            'runner_config_version': agent.runner_config_version or 1,
        },
        'Runner config retrieved successfully',
    ).to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runner-config', methods=['PATCH'])
@unified_auth_required
def patch_runner_config(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(
        optional_fields=['execution_mode', 'runner_enabled', 'sandbox_profile', 'sandbox_policy']
    )
    if isinstance(data, tuple):
        return data

    if 'execution_mode' in data:
        execution_mode = str(data.get('execution_mode') or '').strip().lower()
        if execution_mode not in ALLOWED_EXECUTION_MODES:
            return ApiResponse.error('Invalid execution_mode', 400).to_response()
        agent.execution_mode = execution_mode

    if 'runner_enabled' in data:
        agent.runner_enabled = _parse_bool(data.get('runner_enabled'), False)

    if 'sandbox_profile' in data:
        agent.sandbox_profile = str(data.get('sandbox_profile') or 'standard').strip()[:64] or 'standard'

    if 'sandbox_policy' in data:
        sandbox_policy = data.get('sandbox_policy') or {}
        if not isinstance(sandbox_policy, dict):
            return ApiResponse.error('sandbox_policy must be object', 400).to_response()
        allowed_domains = sandbox_policy.get('allowed_domains') or []
        if not isinstance(allowed_domains, list):
            return ApiResponse.error('sandbox_policy.allowed_domains must be array', 400).to_response()
        sanitized_domains = []
        for domain in allowed_domains:
            domain_str = str(domain or '').strip().lower()
            if domain_str:
                sanitized_domains.append(domain_str)

        agent.sandbox_policy = {
            'network_mode': str(sandbox_policy.get('network_mode') or 'whitelist').strip().lower(),
            'allowed_domains': sanitized_domains,
        }

    agent.runner_config_version = (agent.runner_config_version or 1) + 1
    agent.config_version = (agent.config_version or 1) + 1

    db.session.commit()
    return ApiResponse.success(agent.to_dict(), 'Runner config updated successfully').to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/triggers', methods=['GET'])
@unified_auth_required
def list_agent_triggers(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    items = AgentTrigger.query.filter_by(
        workspace_id=workspace_id,
        agent_id=agent_id,
    ).order_by(AgentTrigger.priority.asc(), AgentTrigger.updated_at.desc()).all()

    return ApiResponse.success({'items': [row.to_dict() for row in items]}, 'Triggers retrieved successfully').to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/triggers', methods=['POST'])
@unified_auth_required
def create_agent_trigger(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(
        required_fields=['name', 'trigger_type'],
        optional_fields=[
            'enabled',
            'priority',
            'task_event_types',
            'task_filter',
            'cron_expr',
            'timezone',
            'misfire_policy',
            'catch_up_window_seconds',
            'dedup_window_seconds',
        ],
    )
    if isinstance(data, tuple):
        return data

    name = str(data.get('name') or '').strip()
    if not name:
        return ApiResponse.error('name cannot be empty', 400).to_response()

    trigger_type_raw = str(data.get('trigger_type') or '').strip().lower()
    if trigger_type_raw not in {'task_event', 'cron'}:
        return ApiResponse.error('trigger_type must be task_event or cron', 400).to_response()

    existing = AgentTrigger.query.filter_by(agent_id=agent.id, name=name).first()
    if existing:
        return ApiResponse.error('Trigger name already exists', 409).to_response()

    trigger_type = AgentTriggerType(trigger_type_raw)
    timezone = str(data.get('timezone') or 'UTC').strip() or 'UTC'
    if timezone.upper() != 'UTC':
        return ApiResponse.error('Only UTC timezone is supported in v1', 400).to_response()

    misfire_policy_raw = str(data.get('misfire_policy') or 'catch_up_once').strip().lower()
    if misfire_policy_raw not in {'skip', 'catch_up_once'}:
        return ApiResponse.error('Invalid misfire_policy', 400).to_response()

    task_event_types = []
    cron_expr = None
    next_fire_at = None

    if trigger_type == AgentTriggerType.TASK_EVENT:
        task_event_types = _validate_task_events(data.get('task_event_types') or [])
        if not task_event_types:
            return ApiResponse.error('task_event_types is invalid', 400).to_response()
    else:
        cron_expr = str(data.get('cron_expr') or '').strip()
        if not cron_expr:
            return ApiResponse.error('cron_expr is required for cron trigger', 400).to_response()
        next_fire_at = _compute_next_fire_at(cron_expr, now_utc())
        if not next_fire_at:
            return ApiResponse.error('Invalid cron_expr', 400).to_response()

    trigger = AgentTrigger(
        workspace_id=workspace_id,
        agent_id=agent.id,
        name=name,
        trigger_type=trigger_type.value,
        enabled=_parse_bool(data.get('enabled'), True),
        priority=_normalize_int(data.get('priority'), 100),
        task_event_types=task_event_types,
        task_filter=(data.get('task_filter') if isinstance(data.get('task_filter'), dict) else {}),
        cron_expr=cron_expr,
        timezone='UTC',
        misfire_policy=AgentMisfirePolicy(misfire_policy_raw).value,
        catch_up_window_seconds=max(10, min(_normalize_int(data.get('catch_up_window_seconds'), 300), 86400)),
        dedup_window_seconds=max(10, min(_normalize_int(data.get('dedup_window_seconds'), 60), 3600)),
        next_fire_at=next_fire_at,
        created_by=user.email,
    )

    db.session.add(trigger)
    db.session.commit()

    return ApiResponse.created(trigger.to_dict(), 'Trigger created successfully').to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/triggers/<int:trigger_id>', methods=['PATCH'])
@unified_auth_required
def patch_agent_trigger(workspace_id, agent_id, trigger_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    trigger = AgentTrigger.query.filter_by(
        id=trigger_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    ).first()
    if not trigger:
        return ApiResponse.not_found('Trigger not found').to_response()

    data = validate_json_request(
        optional_fields=[
            'name',
            'enabled',
            'priority',
            'task_event_types',
            'task_filter',
            'cron_expr',
            'misfire_policy',
            'catch_up_window_seconds',
            'dedup_window_seconds',
        ]
    )
    if isinstance(data, tuple):
        return data

    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            return ApiResponse.error('name cannot be empty', 400).to_response()
        duplicated = AgentTrigger.query.filter(
            AgentTrigger.agent_id == agent_id,
            AgentTrigger.name == name,
            AgentTrigger.id != trigger_id,
        ).first()
        if duplicated:
            return ApiResponse.error('Trigger name already exists', 409).to_response()
        trigger.name = name

    if 'enabled' in data:
        trigger.enabled = _parse_bool(data.get('enabled'), True)

    if 'priority' in data:
        trigger.priority = _normalize_int(data.get('priority'), trigger.priority or 100)

    if 'task_filter' in data:
        task_filter = data.get('task_filter') or {}
        if not isinstance(task_filter, dict):
            return ApiResponse.error('task_filter must be object', 400).to_response()
        trigger.task_filter = task_filter

    if 'misfire_policy' in data:
        misfire_policy_raw = str(data.get('misfire_policy') or '').strip().lower()
        if misfire_policy_raw not in {'skip', 'catch_up_once'}:
            return ApiResponse.error('Invalid misfire_policy', 400).to_response()
        trigger.misfire_policy = AgentMisfirePolicy(misfire_policy_raw).value

    if 'catch_up_window_seconds' in data:
        trigger.catch_up_window_seconds = max(10, min(_normalize_int(data.get('catch_up_window_seconds'), 300), 86400))

    if 'dedup_window_seconds' in data:
        trigger.dedup_window_seconds = max(10, min(_normalize_int(data.get('dedup_window_seconds'), 60), 3600))

    if str(trigger.trigger_type).lower() == AgentTriggerType.TASK_EVENT.value and 'task_event_types' in data:
        normalized = _validate_task_events(data.get('task_event_types') or [])
        if not normalized:
            return ApiResponse.error('task_event_types is invalid', 400).to_response()
        trigger.task_event_types = normalized

    if str(trigger.trigger_type).lower() == AgentTriggerType.CRON.value and 'cron_expr' in data:
        cron_expr = str(data.get('cron_expr') or '').strip()
        if not cron_expr:
            return ApiResponse.error('cron_expr cannot be empty', 400).to_response()
        next_fire_at = _compute_next_fire_at(cron_expr, now_utc())
        if not next_fire_at:
            return ApiResponse.error('Invalid cron_expr', 400).to_response()
        trigger.cron_expr = cron_expr
        trigger.next_fire_at = next_fire_at

    db.session.commit()
    return ApiResponse.success(trigger.to_dict(), 'Trigger updated successfully').to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/triggers/<int:trigger_id>', methods=['DELETE'])
@unified_auth_required
def delete_agent_trigger(workspace_id, agent_id, trigger_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    trigger = AgentTrigger.query.filter_by(
        id=trigger_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    ).first()
    if not trigger:
        return ApiResponse.not_found('Trigger not found').to_response()

    trigger.enabled = False
    db.session.commit()
    return ApiResponse.success(trigger.to_dict(), 'Trigger disabled successfully').to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runs', methods=['GET'])
@unified_auth_required
def list_agent_runs(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    query = AgentRun.query.filter_by(workspace_id=workspace_id, agent_id=agent_id)
    state_value = request.args.get('state', '').strip().lower()
    if state_value:
        if state_value not in {item.value for item in AgentRunState}:
            return ApiResponse.error('Invalid state filter', 400).to_response()
        query = query.filter(AgentRun.state == state_value)

    query = query.order_by(AgentRun.scheduled_at.desc(), AgentRun.id.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [row.to_dict() for row in items],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Runs retrieved successfully',
    ).to_response()


@agent_automation_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/runs/<string:run_id>', methods=['GET'])
@unified_auth_required
def get_agent_run(workspace_id, agent_id, run_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    run = AgentRun.query.filter_by(
        workspace_id=workspace_id,
        agent_id=agent_id,
        run_id=run_id,
    ).first()
    if not run:
        return ApiResponse.not_found('Run not found').to_response()

    return ApiResponse.success(run.to_dict(), 'Run retrieved successfully').to_response()


def _create_channel_for_scope(scope_type, scope_id):
    user = get_current_user()
    scope, err = _resolve_channel_scope(scope_type, scope_id)
    if err:
        return err

    if not _can_manage_scope(scope):
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(
        required_fields=['name', 'channel_type'],
        optional_fields=['enabled', 'is_default', 'events', 'config'],
    )
    if isinstance(data, tuple):
        return data

    channel_type_raw = str(data.get('channel_type') or '').strip().lower()
    if channel_type_raw not in SUPPORTED_NOTIFICATION_CHANNEL_TYPES:
        return ApiResponse.error('Invalid channel_type', 400).to_response()

    events = data.get('events') or []
    if not isinstance(events, list):
        return ApiResponse.error('events must be array', 400).to_response()
    normalized_events = normalize_notification_event_types(events)
    if normalized_events is None:
        return ApiResponse.error('events contains unsupported event type', 400).to_response()

    config = data.get('config') or {}
    validated_config, config_error = validate_notification_channel_config(channel_type_raw, config)
    if config_error:
        return ApiResponse.error(config_error, 400).to_response()

    row = NotificationChannel(
        scope_type=(scope_type.value if hasattr(scope_type, 'value') else str(scope_type)),
        scope_id=scope_id,
        name=str(data.get('name') or '').strip()[:128],
        channel_type=NotificationChannelType(channel_type_raw).value,
        enabled=_parse_bool(data.get('enabled'), True),
        is_default=_parse_bool(data.get('is_default'), False),
        events=normalized_events,
        config=validated_config,
        created_by_user_id=user.id,
        updated_by_user_id=user.id,
        created_by=user.email,
    )

    if not row.name:
        return ApiResponse.error('name cannot be empty', 400).to_response()

    db.session.add(row)
    db.session.commit()

    return ApiResponse.created(serialize_notification_channel(row), 'Channel created successfully').to_response()


def _list_channels_for_scope(scope_type, scope_id):
    scope, err = _resolve_channel_scope(scope_type, scope_id)
    if err:
        return err

    return ApiResponse.success({'items': _list_channels_by_scope(scope_type, scope_id)}, 'Channels retrieved successfully').to_response()


@agent_automation_bp.route('/users/<int:user_id>/channels', methods=['GET'])
@unified_auth_required
def list_user_channels(user_id):
    return _list_channels_for_scope(NotificationScopeType.USER.value, user_id)


@agent_automation_bp.route('/users/<int:user_id>/channels', methods=['POST'])
@unified_auth_required
def create_user_channel(user_id):
    return _create_channel_for_scope(NotificationScopeType.USER.value, user_id)


@agent_automation_bp.route('/organizations/<int:organization_id>/channels', methods=['GET'])
@unified_auth_required
def list_org_channels(organization_id):
    return _list_channels_for_scope(NotificationScopeType.ORGANIZATION.value, organization_id)


@agent_automation_bp.route('/organizations/<int:organization_id>/channels', methods=['POST'])
@unified_auth_required
def create_org_channel(organization_id):
    return _create_channel_for_scope(NotificationScopeType.ORGANIZATION.value, organization_id)


@agent_automation_bp.route('/projects/<int:project_id>/channels', methods=['GET'])
@unified_auth_required
def list_project_channels(project_id):
    return _list_channels_for_scope(NotificationScopeType.PROJECT.value, project_id)


@agent_automation_bp.route('/projects/<int:project_id>/channels', methods=['POST'])
@unified_auth_required
def create_project_channel(project_id):
    return _create_channel_for_scope(NotificationScopeType.PROJECT.value, project_id)


@agent_automation_bp.route('/channels/<int:channel_id>', methods=['PATCH'])
@unified_auth_required
def patch_channel(channel_id):
    user = get_current_user()
    row = NotificationChannel.query.get(channel_id)
    if not row:
        return ApiResponse.not_found('Channel not found').to_response()

    scope, err = _resolve_channel_scope(row.scope_type, row.scope_id)
    if err:
        return err
    if not _can_manage_scope(scope):
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(optional_fields=['name', 'enabled', 'is_default', 'events', 'config'])
    if isinstance(data, tuple):
        return data

    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            return ApiResponse.error('name cannot be empty', 400).to_response()
        row.name = name[:128]

    if 'enabled' in data:
        row.enabled = _parse_bool(data.get('enabled'), True)

    if 'is_default' in data:
        row.is_default = _parse_bool(data.get('is_default'), False)

    if 'events' in data:
        events = data.get('events') or []
        if not isinstance(events, list):
            return ApiResponse.error('events must be array', 400).to_response()
        normalized_events = normalize_notification_event_types(events)
        if normalized_events is None:
            return ApiResponse.error('events contains unsupported event type', 400).to_response()
        row.events = normalized_events

    if 'config' in data:
        config = data.get('config') or {}
        validated_config, config_error = validate_notification_channel_config(row.channel_type, config)
        if config_error:
            return ApiResponse.error(config_error, 400).to_response()
        existing_config = row.config or {}
        channel_type_value = str(row.channel_type or '').strip().lower()
        if channel_type_value in {NotificationChannelType.FEISHU.value, NotificationChannelType.DINGTALK.value}:
            if not validated_config.get('secret') and existing_config.get('secret'):
                validated_config['secret'] = existing_config.get('secret')
        row.config = validated_config

    row.updated_by_user_id = user.id
    db.session.commit()

    return ApiResponse.success(serialize_notification_channel(row), 'Channel updated successfully').to_response()


@agent_automation_bp.route('/channels/<int:channel_id>', methods=['DELETE'])
@unified_auth_required
def delete_channel(channel_id):
    row = NotificationChannel.query.get(channel_id)
    if not row:
        return ApiResponse.not_found('Channel not found').to_response()

    scope, err = _resolve_channel_scope(row.scope_type, row.scope_id)
    if err:
        return err
    if not _can_manage_scope(scope):
        return ApiResponse.forbidden('Access denied').to_response()

    db.session.delete(row)
    db.session.commit()
    return ApiResponse.success(None, 'Channel deleted successfully').to_response()


@agent_automation_bp.route('/projects/<int:project_id>/effective-channels', methods=['GET'])
@unified_auth_required
def get_project_effective_channels(project_id):
    user = get_current_user()
    project = Project.query.get(project_id)
    if not project:
        return ApiResponse.not_found('Project not found').to_response()
    if not user.can_access_project(project):
        return ApiResponse.forbidden('Access denied').to_response()

    event_type = str(request.args.get('event_type') or '').strip().lower()

    levels = []
    project_channels = NotificationChannel.query.filter_by(
        scope_type=NotificationScopeType.PROJECT.value,
        scope_id=project.id,
        enabled=True,
    ).all()
    levels.append({'scope_type': 'project', 'scope_id': project.id, 'items': project_channels})

    if project.organization_id:
        org_channels = NotificationChannel.query.filter_by(
            scope_type=NotificationScopeType.ORGANIZATION.value,
            scope_id=project.organization_id,
            enabled=True,
        ).all()
        levels.append({'scope_type': 'organization', 'scope_id': project.organization_id, 'items': org_channels})

    user_channels = NotificationChannel.query.filter_by(
        scope_type=NotificationScopeType.USER.value,
        scope_id=project.owner_id,
        enabled=True,
    ).all()
    levels.append({'scope_type': 'user', 'scope_id': project.owner_id, 'items': user_channels})

    selected_level = None
    selected_items = []
    for level in levels:
        current_items = [
            row for row in level['items']
            if not event_type or not row.events or event_type in [str(item).strip().lower() for item in (row.events or [])]
        ]
        if current_items:
            selected_level = {'scope_type': level['scope_type'], 'scope_id': level['scope_id']}
            selected_items = current_items
            break

    if not selected_level:
        selected_level = {'scope_type': 'none', 'scope_id': None}

    return ApiResponse.success(
        {
            'event_type': event_type or None,
            'selected_scope': selected_level,
            'items': [serialize_notification_channel(row) for row in selected_items],
        },
        'Effective channels resolved successfully',
    ).to_response()


def make_trigger_idempotency_key(trigger, reason, payload):
    digest = hashlib.sha256(f"{trigger.id}:{reason}:{payload}".encode('utf-8')).hexdigest()
    return f"trg:{trigger.id}:{digest[:32]}"
