from datetime import datetime

from flask import request
from sqlalchemy import func, or_

from models import AgentSecret, AgentSecretShare, Project, db
from core.auth import get_current_user, unified_auth_required
from ..agent_access_control import ensure_agent_detail_access
from ..agent_common import ensure_agent_manage_access, write_agent_audit
from ..base import ApiResponse, validate_json_request
from . import agent_workspace_secrets_bp
from .constants import SCOPE_TYPES, SECRET_TYPES, SHARE_ACCESS_MODES
from .shared import (
    get_agent_or_404,
    get_secret_or_404,
    mark_secret_used,
    normalize_scope_type,
    normalize_secret_type,
    parse_bool,
)


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets', methods=['GET'])
@unified_auth_required
def list_agent_secrets(workspace_id, agent_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    include_shared = parse_bool(request.args.get('include_shared'), default=False)
    now = datetime.utcnow()

    owned_items = AgentSecret.query.filter_by(
        workspace_id=workspace_id,
        agent_id=agent_id,
    ).order_by(
        AgentSecret.is_active.desc(),
        AgentSecret.updated_at.desc(),
    ).all()

    owned_secret_ids = [item.id for item in owned_items]
    active_share_counts = {}
    if owned_secret_ids:
        rows = db.session.query(
            AgentSecretShare.secret_id,
            func.count(AgentSecretShare.id),
        ).filter(
            AgentSecretShare.secret_id.in_(owned_secret_ids),
            AgentSecretShare.is_active.is_(True),
            or_(AgentSecretShare.expires_at.is_(None), AgentSecretShare.expires_at > now),
        ).group_by(
            AgentSecretShare.secret_id
        ).all()
        active_share_counts = {int(secret_id): int(count) for secret_id, count in rows}

    items = []
    owner_name = agent.display_name or agent.name
    for row in owned_items:
        data = row.to_dict(include_secret=False)
        data['source'] = 'owned'
        data['owner_agent_id'] = agent.id
        data['owner_agent_name'] = owner_name
        data['shared_to_agent_count'] = active_share_counts.get(row.id, 0)
        items.append(data)

    if include_shared:
        shares = AgentSecretShare.query.filter(
            AgentSecretShare.workspace_id == workspace_id,
            AgentSecretShare.target_agent_id == agent_id,
            AgentSecretShare.is_active.is_(True),
            or_(AgentSecretShare.expires_at.is_(None), AgentSecretShare.expires_at > now),
        ).order_by(
            AgentSecretShare.updated_at.desc()
        ).all()

        for share in shares:
            secret = share.secret
            if not secret or not secret.is_active:
                continue
            if secret.agent_id == agent_id:
                continue

            data = secret.to_dict(include_secret=False)
            data['source'] = 'shared'
            data['owner_agent_id'] = share.owner_agent_id
            if share.owner_agent:
                data['owner_agent_name'] = share.owner_agent.display_name or share.owner_agent.name
            else:
                data['owner_agent_name'] = None
            data['shared_to_agent_count'] = None
            data['share_id'] = share.id
            data['access_mode'] = share.access_mode
            data['share_expires_at'] = share.expires_at.isoformat() if share.expires_at else None
            items.append(data)

    return ApiResponse.success({'items': items}, 'Agent secrets retrieved successfully').to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets', methods=['POST'])
@unified_auth_required
def create_agent_secret(workspace_id, agent_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(
        required_fields=['name', 'secret_value'],
        optional_fields=['secret_type', 'scope_type', 'project_id', 'description'],
    )
    if isinstance(data, tuple):
        return data

    name = str(data['name']).strip()
    secret_value = str(data['secret_value'])
    if not name:
        return ApiResponse.error('name cannot be empty', 400).to_response()
    if not secret_value:
        return ApiResponse.error('secret_value cannot be empty', 400).to_response()

    secret_type = normalize_secret_type(data.get('secret_type'))
    if secret_type not in SECRET_TYPES:
        return ApiResponse.error('Invalid secret_type', 400).to_response()

    scope_type = normalize_scope_type(data.get('scope_type'))
    if scope_type not in SCOPE_TYPES:
        return ApiResponse.error('Invalid scope_type', 400).to_response()

    description = str(data.get('description') or '').strip() or None

    project_id = data.get('project_id')
    if scope_type == 'project_shared':
        if project_id is None:
            return ApiResponse.error('project_id is required for project_shared scope', 400).to_response()
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return ApiResponse.error('project_id must be an integer', 400).to_response()

        project = Project.query.filter_by(id=project_id, organization_id=workspace_id).first()
        if not project:
            return ApiResponse.error('project_id does not belong to this workspace', 400).to_response()
    else:
        project_id = None

    existing = AgentSecret.query.filter_by(
        workspace_id=workspace_id,
        agent_id=agent_id,
        name=name,
        is_active=True,
    ).first()
    if existing:
        return ApiResponse.error('Active secret with same name already exists', 409).to_response()

    secret = AgentSecret.from_plaintext(
        agent_id=agent_id,
        workspace_id=workspace_id,
        name=name,
        secret_type=secret_type,
        scope_type=scope_type,
        project_id=project_id,
        description=description,
        secret_value=secret_value,
        user_id=user.id,
        created_by=user.email,
    )
    db.session.add(secret)

    write_agent_audit(
        event_type='agent_secret.created',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret',
        target_id='pending',
        workspace_id=workspace_id,
        payload={
            'agent_id': agent_id,
            'name': name,
            'secret_type': secret_type,
            'scope_type': scope_type,
            'project_id': project_id,
        },
        risk_score=70,
    )

    db.session.commit()
    return ApiResponse.created(secret.to_dict(include_secret=False), 'Agent secret created successfully').to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/reveal', methods=['POST'])
@unified_auth_required
def reveal_agent_secret(workspace_id, agent_id, secret_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    if not secret.is_active:
        return ApiResponse.error('Secret is revoked', 400).to_response()

    mark_secret_used(secret)

    write_agent_audit(
        event_type='agent_secret.revealed',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret',
        target_id=secret.id,
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'name': secret.name},
        risk_score=95,
    )

    db.session.commit()
    return ApiResponse.success(
        {
            'id': secret.id,
            'name': secret.name,
            'secret_value': secret.reveal(),
        },
        'Agent secret revealed successfully',
    ).to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/shared-secrets/<int:secret_id>/reveal', methods=['POST'])
@unified_auth_required
def reveal_shared_agent_secret(workspace_id, agent_id, secret_id):
    user = get_current_user()
    target_agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, target_agent)
    if manage_err:
        return manage_err

    now = datetime.utcnow()
    share = AgentSecretShare.query.filter(
        AgentSecretShare.workspace_id == workspace_id,
        AgentSecretShare.target_agent_id == agent_id,
        AgentSecretShare.secret_id == secret_id,
        AgentSecretShare.is_active.is_(True),
        or_(AgentSecretShare.expires_at.is_(None), AgentSecretShare.expires_at > now),
    ).first()
    if not share:
        return ApiResponse.not_found('Shared secret not found').to_response()

    if share.access_mode not in SHARE_ACCESS_MODES:
        return ApiResponse.forbidden('Shared secret is not readable').to_response()

    secret = share.secret
    if not secret or not secret.is_active:
        return ApiResponse.error('Secret is revoked', 400).to_response()

    mark_secret_used(secret)

    write_agent_audit(
        event_type='agent_secret.shared_revealed',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret',
        target_id=secret.id,
        workspace_id=workspace_id,
        payload={
            'owner_agent_id': share.owner_agent_id,
            'target_agent_id': share.target_agent_id,
            'secret_name': secret.name,
            'share_id': share.id,
        },
        risk_score=90,
    )

    db.session.commit()
    return ApiResponse.success(
        {
            'id': secret.id,
            'name': secret.name,
            'secret_value': secret.reveal(),
            'owner_agent_id': share.owner_agent_id,
            'owner_agent_name': (share.owner_agent.display_name or share.owner_agent.name) if share.owner_agent else None,
            'share_id': share.id,
        },
        'Shared secret revealed successfully',
    ).to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/rotate', methods=['POST'])
@unified_auth_required
def rotate_agent_secret(workspace_id, agent_id, secret_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    data = validate_json_request(required_fields=['secret_value'])
    if isinstance(data, tuple):
        return data

    if not secret.is_active:
        return ApiResponse.error('Secret is revoked', 400).to_response()

    secret_value = str(data['secret_value'])
    if not secret_value:
        return ApiResponse.error('secret_value cannot be empty', 400).to_response()

    rotated = AgentSecret.from_plaintext(
        agent_id=secret.agent_id,
        workspace_id=secret.workspace_id,
        name=secret.name,
        secret_type=secret.secret_type,
        scope_type=secret.scope_type,
        project_id=secret.project_id,
        description=secret.description,
        secret_value=secret_value,
        user_id=user.id,
        created_by=secret.created_by or user.email,
    )
    secret.secret_hash = rotated.secret_hash
    secret.secret_encrypted = rotated.secret_encrypted
    secret.prefix = rotated.prefix
    secret.updated_by_user_id = user.id
    secret.is_active = True

    write_agent_audit(
        event_type='agent_secret.rotated',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret',
        target_id=secret.id,
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'name': secret.name},
        risk_score=80,
    )

    db.session.commit()
    return ApiResponse.success(secret.to_dict(include_secret=False), 'Agent secret rotated successfully').to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/revoke', methods=['POST'])
@unified_auth_required
def revoke_agent_secret(workspace_id, agent_id, secret_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    secret.is_active = False
    secret.updated_by_user_id = user.id

    active_shares = AgentSecretShare.query.filter_by(
        workspace_id=workspace_id,
        secret_id=secret.id,
        is_active=True,
    ).all()
    for share in active_shares:
        share.is_active = False
        share.revoked_by_user_id = user.id

    write_agent_audit(
        event_type='agent_secret.revoked',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret',
        target_id=secret.id,
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'name': secret.name},
        risk_score=75,
    )

    db.session.commit()
    return ApiResponse.success(secret.to_dict(include_secret=False), 'Agent secret revoked successfully').to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/shares', methods=['GET'])
@unified_auth_required
def list_agent_secret_shares(workspace_id, agent_id, secret_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    include_inactive = parse_bool(request.args.get('include_inactive'), default=False)
    now = datetime.utcnow()

    query = AgentSecretShare.query.filter_by(
        workspace_id=workspace_id,
        owner_agent_id=agent_id,
        secret_id=secret.id,
    )

    if not include_inactive:
        query = query.filter(AgentSecretShare.is_active.is_(True))

    shares = query.order_by(AgentSecretShare.updated_at.desc()).all()
    items = []
    for share in shares:
        data = share.to_dict(include_agents=True)
        data['is_expired'] = bool(share.expires_at and share.expires_at <= now)
        items.append(data)

    return ApiResponse.success({'items': items}, 'Secret shares retrieved successfully').to_response()
