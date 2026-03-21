"""
Agent Runtime Interaction Protocol API.
"""

from flask import Blueprint, g, request

from core.interaction_contract import (
    normalize_interaction_request_payload,
    normalize_interaction_resolve_payload,
)
from models import Agent, AgentTaskEvent, Project, Task, db

from .agent_common import agent_session_required, generate_id, now_utc, write_agent_audit
from .base import ApiResponse, validate_json_request


agent_runtime_interactions_bp = Blueprint('agent_runtime_interactions', __name__)

INTERACTION_REQUEST_EVENT_TYPE = 'interaction_request'
INTERACTION_RESOLVE_EVENT_TYPE = 'interaction_resolve'
INTERACTION_APPROVAL_EVENT_TYPE = 'interaction_approval'


def _parse_positive_int(raw_value):
    try:
        value = int(str(raw_value or '').strip())
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def _resolve_task_in_workspace(task_id: int, workspace_id: int):
    return (
        Task.query
        .join(Project, Project.id == Task.project_id)
        .filter(
            Task.id == task_id,
            Project.organization_id == workspace_id,
        )
        .first()
    )


def _resolve_target_agent(target_agent_id: int, workspace_id: int):
    return Agent.query.filter_by(id=target_agent_id, workspace_id=workspace_id).first()


def _find_interaction_request_event(
    *,
    workspace_id: int,
    task_id: int,
    interaction_id: str,
    scan_limit: int = 500,
):
    rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.task_id == task_id,
            AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
        )
        .order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc())
        .limit(scan_limit)
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        if str(payload.get('interaction_id') or '').strip() == interaction_id:
            return row
    return None


def _risk_score_for_sensitivity(level: str) -> int:
    normalized = str(level or '').strip().lower()
    if normalized == 'critical':
        return 60
    if normalized == 'high':
        return 30
    if normalized == 'medium':
        return 15
    return 5


def _evaluate_interaction_governance(interaction_type, security_context):
    sensitivity = str((security_context or {}).get('sensitivity_level') or 'medium').strip().lower()
    required_capabilities = (security_context or {}).get('required_capabilities') or []
    if not isinstance(required_capabilities, list):
        required_capabilities = []

    if sensitivity in {'critical', 'high'}:
        return {
            'risk_tier': 'high',
            'requires_approval': True,
            'reason': f'sensitivity={sensitivity}',
        }

    if interaction_type == 'request_capability' and len(required_capabilities) > 0:
        return {
            'risk_tier': 'high',
            'requires_approval': True,
            'reason': 'capability_request_requires_human_approval',
        }

    if interaction_type in {'proxy_execute', 'critique_feedback'}:
        return {
            'risk_tier': 'medium',
            'requires_approval': False,
            'reason': 'interactive_collaboration',
        }

    return {
        'risk_tier': 'low',
        'requires_approval': False,
        'reason': 'default_policy',
    }


def _latest_interaction_approval(
    *,
    workspace_id: int,
    task_id: int,
    interaction_id: str,
    scan_limit: int = 300,
):
    rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.task_id == task_id,
            AgentTaskEvent.event_type == INTERACTION_APPROVAL_EVENT_TYPE,
        )
        .order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc())
        .limit(scan_limit)
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        if str(payload.get('interaction_id') or '').strip() == interaction_id:
            return row
    return None


@agent_runtime_interactions_bp.route('/agent/interactions/request', methods=['POST'])
@agent_session_required
def request_interaction():
    agent = g.current_agent
    data = validate_json_request(required_fields=['task_id', 'interaction_type', 'target_agent_id', 'contract'])
    if isinstance(data, tuple):
        return data

    normalized, err = normalize_interaction_request_payload(
        data,
        current_agent_id=int(agent.id),
    )
    if err:
        return ApiResponse.error(err, 400).to_response()

    task_id = int(normalized['task_id'])
    task = _resolve_task_in_workspace(task_id=task_id, workspace_id=int(agent.workspace_id))
    if not task:
        return ApiResponse.not_found('Task not found in current workspace').to_response()

    target_agent = _resolve_target_agent(
        target_agent_id=int(normalized['target_agent_id']),
        workspace_id=int(agent.workspace_id),
    )
    if not target_agent:
        return ApiResponse.not_found('Target agent not found').to_response()

    target_status = target_agent.status.value if hasattr(target_agent.status, 'value') else str(target_agent.status or '').lower()
    if target_status != 'active':
        return ApiResponse.error('Target agent is not active', 409).to_response()

    interaction_id = generate_id('intx')
    event_time = now_utc()
    attempt_id = normalized.get('attempt_id') or generate_id('ia')
    security_context = normalized.get('security_context') or {}
    sensitivity_level = str(security_context.get('sensitivity_level') or 'medium')
    governance = _evaluate_interaction_governance(
        interaction_type=normalized['interaction_type'],
        security_context=security_context,
    )
    request_status = 'pending_approval' if governance.get('requires_approval') else 'requested'

    payload = {
        'interaction_id': interaction_id,
        'interaction_type': normalized['interaction_type'],
        'source_agent_id': int(agent.id),
        'source_agent_name': agent.name,
        'target_agent_id': int(target_agent.id),
        'target_agent_name': target_agent.name,
        'task_id': task_id,
        'attempt_id': attempt_id,
        'chain_context': normalized.get('chain_context') or {},
        'contract': normalized.get('contract') or {},
        'security_context': security_context,
        'metadata': normalized.get('metadata') or {},
        'status': request_status,
        'governance': governance,
        'requested_at': event_time.isoformat(),
    }

    row = AgentTaskEvent(
        task_id=task_id,
        attempt_id=attempt_id,
        agent_id=int(agent.id),
        workspace_id=int(agent.workspace_id),
        event_type=INTERACTION_REQUEST_EVENT_TYPE,
        seq=1,
        event_timestamp=event_time,
        payload=payload,
        message=f"interaction request {interaction_id} -> agent:{target_agent.id}",
        created_by=f'agent:{agent.id}',
    )
    db.session.add(row)

    write_agent_audit(
        event_type='interaction.requested',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent',
        target_id=target_agent.id,
        workspace_id=agent.workspace_id,
        payload={
            'interaction_id': interaction_id,
            'task_id': task_id,
            'attempt_id': attempt_id,
            'interaction_type': normalized['interaction_type'],
            'audit_source': 'interaction_contract',
            'source_agent_id': int(agent.id),
            'target_agent_id': int(target_agent.id),
            'sensitivity_level': sensitivity_level,
            'risk_tier': governance.get('risk_tier'),
            'requires_approval': bool(governance.get('requires_approval')),
            'request_status': request_status,
        },
        risk_score=_risk_score_for_sensitivity(sensitivity_level),
    )
    db.session.commit()

    response_payload = {
        'interaction_id': interaction_id,
        'task_id': task_id,
        'attempt_id': attempt_id,
        'status': request_status,
        'source_agent_id': int(agent.id),
        'target_agent_id': int(target_agent.id),
        'requested_at': event_time.isoformat(),
        'governance': governance,
    }
    if governance.get('requires_approval'):
        return ApiResponse.success(
            data=response_payload,
            message='Interaction request accepted and pending human approval',
            code=202,
        ).to_response()
    return ApiResponse.created(
        data=response_payload,
        message='Interaction request accepted',
    ).to_response()


@agent_runtime_interactions_bp.route('/agent/interactions/<string:interaction_id>/resolve', methods=['POST'])
@agent_session_required
def resolve_interaction(interaction_id: str):
    agent = g.current_agent
    data = validate_json_request(required_fields=['task_id', 'status'])
    if isinstance(data, tuple):
        return data

    normalized, err = normalize_interaction_resolve_payload(
        data,
        current_agent_id=int(agent.id),
        interaction_id=interaction_id,
    )
    if err:
        return ApiResponse.error(err, 400).to_response()

    task_id = int(normalized['task_id'])
    task = _resolve_task_in_workspace(task_id=task_id, workspace_id=int(agent.workspace_id))
    if not task:
        return ApiResponse.not_found('Task not found in current workspace').to_response()

    request_row = _find_interaction_request_event(
        workspace_id=int(agent.workspace_id),
        task_id=task_id,
        interaction_id=normalized['interaction_id'],
    )
    if not request_row:
        return ApiResponse.not_found('Interaction request not found').to_response()

    request_payload = request_row.payload or {}
    source_agent_id = _parse_positive_int(request_payload.get('source_agent_id')) or 0
    target_agent_id = _parse_positive_int(request_payload.get('target_agent_id')) or 0
    if target_agent_id != int(agent.id):
        return ApiResponse.forbidden('Only target agent can resolve interaction').to_response()

    if source_agent_id <= 0:
        return ApiResponse.error('Invalid interaction source in request payload', 409).to_response()

    governance = request_payload.get('governance') or {}
    if bool(governance.get('requires_approval')):
        approval_row = _latest_interaction_approval(
            workspace_id=int(agent.workspace_id),
            task_id=task_id,
            interaction_id=normalized['interaction_id'],
        )
        if not approval_row:
            return ApiResponse.error('APPROVAL_REQUIRED', 409).to_response()

        approval_payload = approval_row.payload or {}
        decision = str(approval_payload.get('decision') or '').strip().lower()
        if decision == 'rejected':
            return ApiResponse.error('INTERACTION_REJECTED', 409).to_response()
        if decision != 'approved':
            return ApiResponse.error('APPROVAL_REQUIRED', 409).to_response()

    event_time = now_utc()
    attempt_id = normalized.get('attempt_id') or request_payload.get('attempt_id') or generate_id('ia')
    result_payload = normalized.get('result') or {}
    status = normalized['status']

    payload = {
        'interaction_id': normalized['interaction_id'],
        'interaction_type': request_payload.get('interaction_type'),
        'task_id': task_id,
        'attempt_id': attempt_id,
        'source_agent_id': source_agent_id,
        'target_agent_id': target_agent_id,
        'resolver_agent_id': int(agent.id),
        'status': status,
        'result': result_payload,
        'resolved_at': event_time.isoformat(),
        'request_event_id': int(request_row.id),
    }

    row = AgentTaskEvent(
        task_id=task_id,
        attempt_id=str(attempt_id),
        agent_id=int(agent.id),
        workspace_id=int(agent.workspace_id),
        event_type=INTERACTION_RESOLVE_EVENT_TYPE,
        seq=1,
        event_timestamp=event_time,
        payload=payload,
        message=f"interaction resolve {normalized['interaction_id']} status={status}",
        created_by=f'agent:{agent.id}',
    )
    db.session.add(row)

    write_agent_audit(
        event_type='interaction.resolved',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent',
        target_id=source_agent_id,
        workspace_id=agent.workspace_id,
        payload={
            'interaction_id': normalized['interaction_id'],
            'task_id': task_id,
            'attempt_id': str(attempt_id),
            'status': status,
            'error_code': result_payload.get('error_code'),
            'audit_source': 'interaction_contract',
            'source_agent_id': source_agent_id,
            'target_agent_id': target_agent_id,
            'requires_approval': bool(governance.get('requires_approval')),
        },
        risk_score=25 if status in {'failed', 'blocked', 'timeout'} else 10,
    )

    db.session.commit()
    return ApiResponse.success(
        data={
            'interaction_id': normalized['interaction_id'],
            'task_id': task_id,
            'status': status,
            'resolved_at': event_time.isoformat(),
            'resolver_agent_id': int(agent.id),
        },
        message='Interaction resolved',
    ).to_response()


@agent_runtime_interactions_bp.route('/agent/tasks/<int:task_id>/interactions', methods=['GET'])
@agent_session_required
def list_task_interactions(task_id: int):
    agent = g.current_agent
    task = _resolve_task_in_workspace(task_id=task_id, workspace_id=int(agent.workspace_id))
    if not task:
        return ApiResponse.not_found('Task not found in current workspace').to_response()

    try:
        limit = int(str(request.args.get('limit') or '100').strip())
    except Exception:
        limit = 100
    limit = min(max(limit, 1), 200)

    rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == int(agent.workspace_id),
            AgentTaskEvent.task_id == int(task_id),
            AgentTaskEvent.event_type.in_(
                [
                    INTERACTION_REQUEST_EVENT_TYPE,
                    INTERACTION_RESOLVE_EVENT_TYPE,
                    INTERACTION_APPROVAL_EVENT_TYPE,
                ]
            ),
        )
        .order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc())
        .limit(limit)
        .all()
    )

    items = []
    for row in rows:
        payload = row.payload or {}
        interaction_id_value = str(payload.get('interaction_id') or '').strip()
        if not interaction_id_value:
            continue
        items.append(
            {
                'event_id': int(row.id),
                'event_type': row.event_type,
                'interaction_id': interaction_id_value,
                'task_id': int(row.task_id),
                'attempt_id': row.attempt_id,
                'agent_id': int(row.agent_id),
                'event_timestamp': row.event_timestamp.isoformat() if row.event_timestamp else None,
                'message': row.message,
                'payload': payload,
            }
        )

    return ApiResponse.success(
        data={
            'task_id': int(task_id),
            'items': items,
        },
        message='Task interactions retrieved successfully',
    ).to_response()
