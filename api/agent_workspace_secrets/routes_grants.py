from datetime import datetime, timedelta

from flask import request
from sqlalchemy import or_

from core.auth import get_current_user, unified_auth_required
from models import Agent, AgentSecretGrant, Project, Task, db

from ..agent_common import ensure_agent_manage_access, generate_id, write_agent_audit
from ..base import ApiResponse, validate_json_request
from . import agent_workspace_secrets_bp
from .constants import GRANT_MODES, GRANT_STATUSES
from .shared import (
    get_agent_or_404,
    get_secret_or_404,
    is_agent_active,
    parse_bool,
    parse_expires_at,
)


def _default_expires_at_for_mode(grant_mode: str):
    now = datetime.utcnow()
    if grant_mode == 'ephemeral':
        return now + timedelta(hours=1)
    if grant_mode == 'leased':
        return now + timedelta(days=1)
    return now + timedelta(days=30)


def _default_max_uses_for_mode(grant_mode: str):
    if grant_mode == 'ephemeral':
        return 1
    if grant_mode == 'leased':
        return 100
    return None


def _parse_positive_int(value, field_name):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None, ApiResponse.error(f'{field_name} must be an integer', 400).to_response()
    if parsed <= 0:
        return None, ApiResponse.error(f'{field_name} must be > 0', 400).to_response()
    return parsed, None


def _grant_to_dict_with_agents(row):
    data = row.to_dict()
    data['from_agent_name'] = (row.from_agent.display_name or row.from_agent.name) if row.from_agent else None
    data['to_agent_name'] = (row.to_agent.display_name or row.to_agent.name) if row.to_agent else None
    data['remaining_uses'] = None
    if row.max_uses is not None:
        data['remaining_uses'] = max(int(row.max_uses) - int(row.used_count or 0), 0)
    return data


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/grants', methods=['GET'])
@unified_auth_required
def list_agent_secret_grants(workspace_id, agent_id, secret_id):
    user = get_current_user()
    owner_agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, owner_agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    include_inactive = parse_bool(request.args.get('include_inactive'), default=False)
    include_expired = parse_bool(request.args.get('include_expired'), default=False)
    status_filter = str(request.args.get('status') or '').strip().lower()
    now = datetime.utcnow()

    query = AgentSecretGrant.query.filter_by(
        workspace_id=workspace_id,
        secret_id=secret.id,
        from_agent_id=agent_id,
    )

    if status_filter:
        if status_filter not in GRANT_STATUSES:
            return ApiResponse.error('Invalid status filter', 400).to_response()
        query = query.filter(AgentSecretGrant.status == status_filter)
    elif not include_inactive:
        query = query.filter(AgentSecretGrant.status == 'active')

    if not include_expired:
        query = query.filter(
            or_(
                AgentSecretGrant.expires_at.is_(None),
                AgentSecretGrant.expires_at > now,
            )
        )

    rows = query.order_by(AgentSecretGrant.updated_at.desc(), AgentSecretGrant.id.desc()).limit(300).all()
    items = [_grant_to_dict_with_agents(row) for row in rows]

    return ApiResponse.success({'items': items}, 'Secret grants retrieved successfully').to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/grants', methods=['POST'])
@unified_auth_required
def create_agent_secret_grant(workspace_id, agent_id, secret_id):
    user = get_current_user()
    owner_agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, owner_agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err
    if not secret.is_active:
        return ApiResponse.error('Secret is revoked', 400).to_response()

    data = validate_json_request(
        required_fields=['target_agent_id'],
        optional_fields=[
            'grant_mode',
            'expires_at',
            'max_uses',
            'task_id',
            'attempt_id',
            'chain_id',
            'granted_reason',
        ],
    )
    if isinstance(data, tuple):
        return data

    target_agent_id, err = _parse_positive_int(data.get('target_agent_id'), 'target_agent_id')
    if err:
        return err
    if int(target_agent_id) == int(agent_id):
        return ApiResponse.error('Cannot grant secret to same agent', 400).to_response()

    target_agent = Agent.query.filter_by(id=target_agent_id, workspace_id=workspace_id).first()
    if not target_agent:
        return ApiResponse.not_found('Target agent not found').to_response()
    if not is_agent_active(target_agent):
        return ApiResponse.error('Target agent is not active', 400).to_response()

    grant_mode = str(data.get('grant_mode') or 'ephemeral').strip().lower()
    if grant_mode not in GRANT_MODES:
        return ApiResponse.error('Invalid grant_mode', 400).to_response()

    max_uses_raw = data.get('max_uses')
    if max_uses_raw in (None, ''):
        max_uses = _default_max_uses_for_mode(grant_mode)
    else:
        max_uses, err = _parse_positive_int(max_uses_raw, 'max_uses')
        if err:
            return err

    expires_at, expires_err = parse_expires_at(data.get('expires_at'))
    if expires_err:
        return expires_err
    if expires_at is None:
        expires_at = _default_expires_at_for_mode(grant_mode)

    task_id = None
    if data.get('task_id') not in (None, ''):
        task_id, err = _parse_positive_int(data.get('task_id'), 'task_id')
        if err:
            return err
        task = (
            Task.query
            .join(Project, Project.id == Task.project_id)
            .filter(Task.id == task_id, Project.organization_id == workspace_id)
            .first()
        )
        if not task:
            return ApiResponse.error('task_id does not belong to this workspace', 400).to_response()

    chain_id = None
    if data.get('chain_id') not in (None, ''):
        chain_id, err = _parse_positive_int(data.get('chain_id'), 'chain_id')
        if err:
            return err

    attempt_id = str(data.get('attempt_id') or '').strip() or None
    if attempt_id and len(attempt_id) > 64:
        return ApiResponse.error('attempt_id exceeds max length 64', 400).to_response()

    granted_reason = str(data.get('granted_reason') or '').strip() or None

    row = AgentSecretGrant(
        grant_id=generate_id('grt'),
        secret_id=secret.id,
        workspace_id=workspace_id,
        from_agent_id=agent_id,
        to_agent_id=target_agent_id,
        chain_id=chain_id,
        task_id=task_id,
        attempt_id=attempt_id,
        grant_mode=grant_mode,
        max_uses=max_uses,
        used_count=0,
        expires_at=expires_at,
        status='active',
        granted_reason=granted_reason,
        granted_by_user_id=user.id,
        created_by=user.email,
    )
    db.session.add(row)

    write_agent_audit(
        event_type='agent_secret.grant_created',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret_grant',
        target_id='pending',
        workspace_id=workspace_id,
        payload={
            'secret_id': int(secret.id),
            'secret_name': secret.name,
            'from_agent_id': int(agent_id),
            'to_agent_id': int(target_agent_id),
            'grant_mode': grant_mode,
            'max_uses': max_uses,
            'expires_at': expires_at.isoformat() if expires_at else None,
            'task_id': task_id,
            'attempt_id': attempt_id,
            'chain_id': chain_id,
        },
        risk_score=70,
    )

    db.session.commit()
    return ApiResponse.created(
        _grant_to_dict_with_agents(row),
        'Secret grant created successfully',
    ).to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/grants/<string:grant_id>/revoke', methods=['POST'])
@unified_auth_required
def revoke_agent_secret_grant(workspace_id, agent_id, secret_id, grant_id):
    user = get_current_user()
    owner_agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, owner_agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    row = AgentSecretGrant.query.filter_by(
        grant_id=grant_id,
        workspace_id=workspace_id,
        secret_id=secret.id,
        from_agent_id=agent_id,
    ).first()
    if not row:
        return ApiResponse.not_found('Secret grant not found').to_response()

    if row.status != 'active':
        return ApiResponse.error('Secret grant is not active', 409).to_response()

    row.status = 'revoked'
    row.revoked_by_user_id = user.id

    write_agent_audit(
        event_type='agent_secret.grant_revoked',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret_grant',
        target_id=row.grant_id,
        workspace_id=workspace_id,
        payload={
            'grant_id': row.grant_id,
            'secret_id': int(secret.id),
            'from_agent_id': int(row.from_agent_id),
            'to_agent_id': int(row.to_agent_id),
        },
        risk_score=60,
    )

    db.session.commit()
    return ApiResponse.success(
        _grant_to_dict_with_agents(row),
        'Secret grant revoked successfully',
    ).to_response()

