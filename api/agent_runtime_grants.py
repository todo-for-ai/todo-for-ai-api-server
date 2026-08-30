"""
Agent runtime secret grant consume/revoke APIs.
"""

from datetime import datetime

from flask import Blueprint, g

from models import AgentSecretGrant, db

from .agent_common import agent_session_required, write_agent_audit
from .base import ApiResponse, validate_json_request
from .agent_workspace_secrets.shared import mark_secret_used


agent_runtime_grants_bp = Blueprint('agent_runtime_grants', __name__)


def _get_grant_for_target_agent(grant_id, workspace_id, to_agent_id):
    return AgentSecretGrant.query.filter_by(
        grant_id=grant_id,
        workspace_id=workspace_id,
        to_agent_id=to_agent_id,
    ).first()


@agent_runtime_grants_bp.route('/agent/grants/<string:grant_id>/consume', methods=['POST'])
@agent_session_required
def consume_agent_secret_grant(grant_id):
    agent = g.current_agent
    data = validate_json_request(optional_fields=['task_id', 'attempt_id'])
    if isinstance(data, tuple):
        return data

    row = _get_grant_for_target_agent(
        grant_id=grant_id,
        workspace_id=int(agent.workspace_id),
        to_agent_id=int(agent.id),
    )
    if not row:
        return ApiResponse.not_found('Grant not found').to_response()

    if row.status != 'active':
        return ApiResponse.error('GRANT_NOT_ACTIVE', 409).to_response()

    now = datetime.utcnow()
    if row.expires_at and row.expires_at <= now:
        row.status = 'expired'
        db.session.commit()
        return ApiResponse.error('GRANT_EXPIRED', 409).to_response()

    secret = row.secret
    if not secret or not secret.is_active:
        row.status = 'revoked'
        db.session.commit()
        return ApiResponse.error('SECRET_REVOKED', 409).to_response()

    task_id = data.get('task_id')
    if row.task_id is not None and task_id not in (None, '') and int(task_id) != int(row.task_id):
        return ApiResponse.error('GRANT_TASK_MISMATCH', 409).to_response()
    if row.task_id is not None and task_id in (None, ''):
        return ApiResponse.error('task_id required by grant scope', 400).to_response()

    attempt_id = str(data.get('attempt_id') or '').strip()
    if row.attempt_id and row.attempt_id != attempt_id:
        return ApiResponse.error('GRANT_ATTEMPT_MISMATCH', 409).to_response()

    if row.max_uses is not None and int(row.used_count or 0) >= int(row.max_uses):
        row.status = 'expired'
        db.session.commit()
        return ApiResponse.error('GRANT_EXHAUSTED', 409).to_response()

    row.used_count = int(row.used_count or 0) + 1
    row.last_used_at = now
    if row.max_uses is not None and int(row.used_count) >= int(row.max_uses):
        row.status = 'expired'

    mark_secret_used(secret)

    write_agent_audit(
        event_type='agent_secret.grant_consumed',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent_secret_grant',
        target_id=row.grant_id,
        workspace_id=agent.workspace_id,
        payload={
            'grant_id': row.grant_id,
            'secret_id': int(row.secret_id),
            'from_agent_id': int(row.from_agent_id),
            'to_agent_id': int(row.to_agent_id),
            'task_id': int(row.task_id) if row.task_id is not None else None,
            'attempt_id': row.attempt_id,
            'grant_mode': row.grant_mode,
            'used_count': int(row.used_count or 0),
            'max_uses': int(row.max_uses) if row.max_uses is not None else None,
            'status': row.status,
        },
        risk_score=20,
    )

    db.session.commit()

    remaining_uses = None
    if row.max_uses is not None:
        remaining_uses = max(int(row.max_uses) - int(row.used_count or 0), 0)

    return ApiResponse.success(
        {
            'grant_id': row.grant_id,
            'status': row.status,
            'grant_mode': row.grant_mode,
            'expires_at': row.expires_at.isoformat() if row.expires_at else None,
            'remaining_uses': remaining_uses,
            'secret_ref': {
                'id': int(secret.id),
                'name': secret.name,
                'secret_type': secret.secret_type,
                'scope_type': secret.scope_type,
                'project_id': int(secret.project_id) if secret.project_id is not None else None,
                'prefix': secret.prefix,
            },
        },
        'Grant consumed successfully',
    ).to_response()


@agent_runtime_grants_bp.route('/agent/grants/<string:grant_id>/revoke', methods=['POST'])
@agent_session_required
def revoke_agent_secret_grant_runtime(grant_id):
    agent = g.current_agent

    row = AgentSecretGrant.query.filter_by(
        grant_id=grant_id,
        workspace_id=int(agent.workspace_id),
    ).first()
    if not row:
        return ApiResponse.not_found('Grant not found').to_response()

    if int(agent.id) not in {int(row.from_agent_id), int(row.to_agent_id)}:
        return ApiResponse.forbidden('Only participating agents can revoke this grant').to_response()

    if row.status == 'active':
        row.status = 'revoked'
        row.revoked_by_agent_id = int(agent.id)

    write_agent_audit(
        event_type='agent_secret.grant_revoked_by_agent',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent_secret_grant',
        target_id=row.grant_id,
        workspace_id=agent.workspace_id,
        payload={
            'grant_id': row.grant_id,
            'from_agent_id': int(row.from_agent_id),
            'to_agent_id': int(row.to_agent_id),
            'status': row.status,
        },
        risk_score=30,
    )

    db.session.commit()
    return ApiResponse.success(
        {
            'grant_id': row.grant_id,
            'status': row.status,
            'from_agent_id': int(row.from_agent_id),
            'to_agent_id': int(row.to_agent_id),
        },
        'Grant revoked successfully',
    ).to_response()

