"""Trigger routes for agent automation."""

from models import db, AgentTrigger, AgentTriggerType, AgentMisfirePolicy
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse, validate_json_request
from ..agent_common import ensure_agent_manage_access, now_utc
from ..agent_access_control import ensure_agent_detail_access

from . import agent_automation_bp
from .shared import (
    _get_agent_or_404,
    _parse_bool,
    _normalize_int,
    _validate_task_events,
    _compute_next_fire_at,
)

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
