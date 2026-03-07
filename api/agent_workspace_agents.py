"""
Workspace Agent 基础管理 API
"""

from flask import Blueprint
from models import db, Agent, AgentStatus, AgentSoulVersion
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request, get_request_args
from .agent_common import (
    get_workspace_or_404,
    ensure_workspace_access,
    ensure_agent_manage_access,
    write_agent_audit,
)


agent_workspace_agents_bp = Blueprint('agent_workspace_agents', __name__)


def _agent_query_for_workspace(workspace_id):
    return Agent.query.filter_by(workspace_id=workspace_id)


AGENT_EDITABLE_FIELDS = [
    'name', 'description', 'display_name', 'avatar_url', 'homepage_url', 'contact_email',
    'capability_tags', 'allowed_project_ids',
    'llm_provider', 'llm_model', 'temperature', 'top_p', 'max_output_tokens', 'context_window_tokens', 'reasoning_mode',
    'system_prompt', 'soul_markdown',
    'response_style', 'tool_policy', 'memory_policy', 'handoff_policy',
    'execution_mode', 'runner_enabled', 'sandbox_profile', 'sandbox_policy',
    'max_concurrency', 'max_retry', 'timeout_seconds', 'heartbeat_interval_seconds',
]
FLOAT_FIELDS = {'temperature', 'top_p'}
INT_FIELDS = {'max_output_tokens', 'context_window_tokens', 'max_concurrency', 'max_retry', 'timeout_seconds', 'heartbeat_interval_seconds'}
BOOL_FIELDS = {'runner_enabled'}
JSON_OBJECT_FIELDS = {'response_style', 'tool_policy', 'memory_policy', 'handoff_policy', 'sandbox_policy'}
JSON_LIST_FIELDS = {'capability_tags', 'allowed_project_ids'}


def _normalize_int(value, default_value):
    try:
        return int(value)
    except Exception:
        return default_value


def _normalize_float(value, default_value):
    try:
        return float(value)
    except Exception:
        return default_value


def _create_soul_snapshot(agent, user, change_summary):
    soul_content = agent.soul_markdown or ''
    snapshot = AgentSoulVersion(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        version=agent.soul_version,
        soul_markdown=soul_content,
        change_summary=(change_summary or '')[:255],
        edited_by_user_id=user.id,
        created_by=user.email,
    )
    db.session.add(snapshot)


def _normalized_value(field, raw_value):
    if field in FLOAT_FIELDS:
        defaults = {'temperature': 0.7, 'top_p': 1.0}
        return _normalize_float(raw_value, defaults[field])
    if field in INT_FIELDS:
        defaults = {
            'max_output_tokens': None,
            'context_window_tokens': None,
            'max_concurrency': 1,
            'max_retry': 2,
            'timeout_seconds': 1800,
            'heartbeat_interval_seconds': 20,
        }
        return _normalize_int(raw_value, defaults[field])
    if field in BOOL_FIELDS:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        lowered = str(raw_value or '').strip().lower()
        return lowered in {'1', 'true', 'yes', 'on'}
    if field in JSON_OBJECT_FIELDS:
        return raw_value or {}
    if field in JSON_LIST_FIELDS:
        return raw_value or []
    if isinstance(raw_value, str):
        return raw_value.strip()
    return raw_value


@agent_workspace_agents_bp.route('/workspaces/<int:workspace_id>/agents', methods=['GET'])
@unified_auth_required
def list_agents(workspace_id):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    args = get_request_args()
    query = _agent_query_for_workspace(workspace_id)

    if args['search']:
        search = f"%{args['search']}%"
        query = query.filter((Agent.name.like(search)) | (Agent.display_name.like(search)))

    status_filter = str(args.get('status') or '').strip().lower()
    if status_filter:
        allowed_status = {item.value for item in AgentStatus}
        if status_filter not in allowed_status:
            return ApiResponse.error('Invalid status filter', 400).to_response()
        query = query.filter(Agent.status == AgentStatus(status_filter))

    query = query.order_by(Agent.updated_at.desc())
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [item.to_dict() for item in items],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Agents retrieved successfully',
    ).to_response()


@agent_workspace_agents_bp.route('/workspaces/<int:workspace_id>/agents', methods=['POST'])
@unified_auth_required
def create_agent(workspace_id):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    data = validate_json_request(
        required_fields=['name'],
        optional_fields=AGENT_EDITABLE_FIELDS + ['change_summary'],
    )
    if isinstance(data, tuple):
        return data

    name = str(data.get('name', '')).strip()
    if not name:
        return ApiResponse.error('name cannot be empty', 400).to_response()

    agent = Agent(
        workspace_id=workspace_id,
        creator_user_id=user.id,
        name=name,
        description=data.get('description', ''),
        display_name=data.get('display_name', ''),
        avatar_url=data.get('avatar_url', ''),
        homepage_url=data.get('homepage_url', ''),
        contact_email=data.get('contact_email', ''),
        capability_tags=data.get('capability_tags') or [],
        allowed_project_ids=data.get('allowed_project_ids') or [],
        llm_provider=data.get('llm_provider', ''),
        llm_model=data.get('llm_model', ''),
        temperature=_normalize_float(data.get('temperature'), 0.7),
        top_p=_normalize_float(data.get('top_p'), 1.0),
        max_output_tokens=_normalize_int(data.get('max_output_tokens'), None),
        context_window_tokens=_normalize_int(data.get('context_window_tokens'), None),
        reasoning_mode=data.get('reasoning_mode') or 'balanced',
        system_prompt=data.get('system_prompt', ''),
        soul_markdown=data.get('soul_markdown', ''),
        response_style=data.get('response_style') or {},
        tool_policy=data.get('tool_policy') or {},
        memory_policy=data.get('memory_policy') or {},
        handoff_policy=data.get('handoff_policy') or {},
        execution_mode=(str(data.get('execution_mode') or 'external_pull').strip().lower() or 'external_pull'),
        runner_enabled=bool(data.get('runner_enabled', False)),
        sandbox_profile=(str(data.get('sandbox_profile') or 'standard').strip()[:64] or 'standard'),
        sandbox_policy=data.get('sandbox_policy') or {'network_mode': 'whitelist', 'allowed_domains': []},
        max_concurrency=_normalize_int(data.get('max_concurrency'), 1),
        max_retry=_normalize_int(data.get('max_retry'), 2),
        timeout_seconds=_normalize_int(data.get('timeout_seconds'), 1800),
        heartbeat_interval_seconds=_normalize_int(data.get('heartbeat_interval_seconds'), 20),
        soul_version=1,
        config_version=1,
        runner_config_version=1,
        status=AgentStatus.ACTIVE,
        created_by=user.email,
    )
    db.session.add(agent)
    db.session.flush()

    _create_soul_snapshot(agent, user, data.get('change_summary') or 'initial version')

    write_agent_audit(
        event_type='agent.created',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=workspace_id,
        payload={'name': agent.name, 'config_version': agent.config_version, 'soul_version': agent.soul_version},
    )

    db.session.commit()
    return ApiResponse.created(agent.to_dict(), 'Agent created successfully').to_response()


@agent_workspace_agents_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>', methods=['GET'])
@unified_auth_required
def get_agent(workspace_id, agent_id):
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    return ApiResponse.success(agent.to_dict(), 'Agent retrieved successfully').to_response()


@agent_workspace_agents_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>', methods=['PATCH'])
@unified_auth_required
def update_agent(workspace_id, agent_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(optional_fields=AGENT_EDITABLE_FIELDS + ['status', 'change_summary'])
    if isinstance(data, tuple):
        return data

    updated_fields = []
    soul_changed = False

    if 'status' in data:
        try:
            agent.status = AgentStatus(data['status'])
            updated_fields.append('status')
        except ValueError:
            return ApiResponse.error('Invalid status', 400).to_response()

    for field in AGENT_EDITABLE_FIELDS:
        if field in data:
            old_value = getattr(agent, field)
            normalized_value = _normalized_value(field, data[field])
            setattr(agent, field, normalized_value)
            if old_value != normalized_value:
                updated_fields.append(field)
                if field == 'soul_markdown':
                    soul_changed = True

    if updated_fields:
        agent.config_version = (agent.config_version or 1) + 1

    if soul_changed:
        agent.soul_version = (agent.soul_version or 1) + 1
        _create_soul_snapshot(agent, user, data.get('change_summary') or 'updated soul')

    write_agent_audit(
        event_type='agent.updated',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=workspace_id,
        payload={'updated_fields': updated_fields, 'config_version': agent.config_version, 'soul_version': agent.soul_version},
    )

    db.session.commit()
    return ApiResponse.success(agent.to_dict(), 'Agent updated successfully').to_response()


@agent_workspace_agents_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>', methods=['DELETE'])
@unified_auth_required
def delete_agent(workspace_id, agent_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    agent.status = AgentStatus.REVOKED
    write_agent_audit(
        event_type='agent.deleted',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=workspace_id,
        payload={},
        risk_score=20,
    )

    db.session.commit()
    return ApiResponse.success(None, 'Agent revoked successfully').to_response()
