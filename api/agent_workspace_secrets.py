"""
Workspace Agent Secrets API
"""

from datetime import datetime, timezone
from flask import Blueprint, request
from sqlalchemy import func, or_

from models import db, Agent, AgentSecret, AgentSecretShare, Project
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request
from .agent_common import ensure_agent_manage_access, write_agent_audit
from .agent_access_control import ensure_agent_detail_access


agent_workspace_secrets_bp = Blueprint('agent_workspace_secrets', __name__)

SECRET_TYPES = {'api_key', 'oauth_token', 'session_cookie', 'webhook_secret', 'custom'}
SCOPE_TYPES = {'agent_private', 'project_shared', 'workspace_shared'}
SHARE_ACCESS_MODES = {'read'}
TARGET_SELECTOR_MODES = {'manual', 'project_agents', 'workspace_active'}


def _parse_bool(value, default=False):
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on'}:
        return True
    if text in {'0', 'false', 'no', 'off'}:
        return False
    return default


def _parse_expires_at(raw_value):
    if raw_value in (None, ''):
        return None, None

    text = str(raw_value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None, ApiResponse.error('Invalid expires_at, use ISO datetime format', 400).to_response()

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    if parsed <= datetime.utcnow():
        return None, ApiResponse.error('expires_at must be in the future', 400).to_response()

    return parsed, None


def _normalize_secret_type(value):
    return (str(value or 'api_key').strip().lower() or 'api_key')


def _normalize_scope_type(value):
    return (str(value or 'agent_private').strip().lower() or 'agent_private')


def _normalize_target_selector(value):
    return (str(value or 'manual').strip().lower() or 'manual')


def _to_int_optional(raw_value):
    if raw_value in (None, ''):
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _is_agent_active(agent):
    status_text = agent.status.value if hasattr(agent.status, 'value') else str(agent.status).strip().lower()
    return status_text == 'active'


def _normalize_project_id_for_selector(workspace_id, raw_project_id):
    project_id = _to_int_optional(raw_project_id)
    if project_id is None:
        return None, ApiResponse.error('selector_project_id must be an integer', 400).to_response()

    project = Project.query.filter_by(id=project_id, organization_id=workspace_id).first()
    if not project:
        return None, ApiResponse.error('selector_project_id does not belong to this workspace', 400).to_response()
    return project_id, None


def _allowed_project_id_set(agent):
    values = agent.allowed_project_ids or []
    if not isinstance(values, list):
        values = [values]
    result = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _resolve_target_agent_ids_by_selector(workspace_id, owner_agent_id, selector_mode, selector_project_id):
    if selector_mode == 'manual':
        return [], None

    candidates = Agent.query.filter_by(workspace_id=workspace_id).all()
    resolved = []

    if selector_mode == 'workspace_active':
        for candidate in candidates:
            if candidate.id == owner_agent_id:
                continue
            if not _is_agent_active(candidate):
                continue
            resolved.append(int(candidate.id))
        return sorted(set(resolved)), None

    if selector_mode == 'project_agents':
        if selector_project_id is None:
            return [], ApiResponse.error('selector_project_id is required for project_agents selector', 400).to_response()

        for candidate in candidates:
            if candidate.id == owner_agent_id:
                continue
            if not _is_agent_active(candidate):
                continue
            if selector_project_id in _allowed_project_id_set(candidate):
                resolved.append(int(candidate.id))
        return sorted(set(resolved)), None

    return [], ApiResponse.error('Invalid target_selector', 400).to_response()


def _get_agent_or_404(workspace_id, agent_id):
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


def _get_secret_or_404(workspace_id, agent_id, secret_id):
    secret = AgentSecret.query.filter_by(id=secret_id, workspace_id=workspace_id, agent_id=agent_id).first()
    if not secret:
        return None, ApiResponse.not_found('Agent secret not found').to_response()
    return secret, None


def _mark_secret_used(secret):
    secret.last_used_at = datetime.utcnow()
    secret.usage_count = int(secret.usage_count or 0) + 1


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets', methods=['GET'])
@unified_auth_required
def list_agent_secrets(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    include_shared = _parse_bool(request.args.get('include_shared'), default=False)
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
    agent, err = _get_agent_or_404(workspace_id, agent_id)
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

    secret_type = _normalize_secret_type(data.get('secret_type'))
    if secret_type not in SECRET_TYPES:
        return ApiResponse.error('Invalid secret_type', 400).to_response()

    scope_type = _normalize_scope_type(data.get('scope_type'))
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
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = _get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    if not secret.is_active:
        return ApiResponse.error('Secret is revoked', 400).to_response()

    _mark_secret_used(secret)

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
    target_agent, err = _get_agent_or_404(workspace_id, agent_id)
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

    _mark_secret_used(secret)

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
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = _get_secret_or_404(workspace_id, agent_id, secret_id)
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
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = _get_secret_or_404(workspace_id, agent_id, secret_id)
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
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = _get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    include_inactive = _parse_bool(request.args.get('include_inactive'), default=False)
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


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/collaboration', methods=['GET'])
@unified_auth_required
def get_agent_secret_collaboration(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    include_inactive = _parse_bool(request.args.get('include_inactive'), default=False)
    now = datetime.utcnow()

    project_id_filter = None
    raw_project_id = request.args.get('project_id')
    if raw_project_id not in (None, ''):
        project_id_filter, project_err = _normalize_project_id_for_selector(workspace_id, raw_project_id)
        if project_err:
            return project_err

    outgoing_query = AgentSecretShare.query.filter_by(
        workspace_id=workspace_id,
        owner_agent_id=agent_id,
    )
    incoming_query = AgentSecretShare.query.filter_by(
        workspace_id=workspace_id,
        target_agent_id=agent_id,
    )

    if not include_inactive:
        outgoing_query = outgoing_query.filter(
            AgentSecretShare.is_active.is_(True),
            or_(AgentSecretShare.expires_at.is_(None), AgentSecretShare.expires_at > now),
        )
        incoming_query = incoming_query.filter(
            AgentSecretShare.is_active.is_(True),
            or_(AgentSecretShare.expires_at.is_(None), AgentSecretShare.expires_at > now),
        )

    outgoing_rows = outgoing_query.order_by(AgentSecretShare.updated_at.desc(), AgentSecretShare.id.desc()).all()
    incoming_rows = incoming_query.order_by(AgentSecretShare.updated_at.desc(), AgentSecretShare.id.desc()).all()

    edges = []
    outgoing_map = {}
    incoming_map = {}
    related_agent_ids = set()

    def _upsert_collaborator(target_map, collaborator_id, direction, share, secret):
        row = target_map.get(collaborator_id)
        if row is None:
            row = {
                'agent_id': collaborator_id,
                'agent_name': None,
                'direction': direction,
                'share_count': 0,
                'active_share_count': 0,
                'expired_share_count': 0,
                'secret_count': 0,
                'secret_ids': set(),
                'last_granted_at': None,
                'last_updated_at': None,
            }
            target_map[collaborator_id] = row

        row['share_count'] += 1
        if share.is_active:
            row['active_share_count'] += 1
        if share.expires_at and share.expires_at <= now:
            row['expired_share_count'] += 1

        row['secret_ids'].add(int(secret.id))
        row['secret_count'] = len(row['secret_ids'])

        if share.created_at and (row['last_granted_at'] is None or share.created_at > row['last_granted_at']):
            row['last_granted_at'] = share.created_at
        if share.updated_at and (row['last_updated_at'] is None or share.updated_at > row['last_updated_at']):
            row['last_updated_at'] = share.updated_at

    def _append_edge(direction, share, secret):
        is_expired = bool(share.expires_at and share.expires_at <= now)
        edges.append(
            {
                'id': share.id,
                'direction': direction,
                'secret_id': secret.id,
                'secret_name': secret.name,
                'secret_type': secret.secret_type,
                'scope_type': secret.scope_type,
                'project_id': secret.project_id,
                'secret_is_active': bool(secret.is_active),
                'owner_agent_id': share.owner_agent_id,
                'target_agent_id': share.target_agent_id,
                'access_mode': share.access_mode,
                'is_active': bool(share.is_active),
                'is_expired': is_expired,
                'expires_at': share.expires_at.isoformat() if share.expires_at else None,
                'created_at': share.created_at.isoformat() if share.created_at else None,
                'updated_at': share.updated_at.isoformat() if share.updated_at else None,
            }
        )

    for share in outgoing_rows:
        secret = share.secret
        if not secret:
            continue
        if project_id_filter is not None and int(secret.project_id or 0) != int(project_id_filter):
            continue
        if not include_inactive and not secret.is_active:
            continue

        related_agent_ids.add(int(share.target_agent_id))
        _append_edge('outgoing', share, secret)
        _upsert_collaborator(outgoing_map, int(share.target_agent_id), 'outgoing', share, secret)

    for share in incoming_rows:
        secret = share.secret
        if not secret:
            continue
        if project_id_filter is not None and int(secret.project_id or 0) != int(project_id_filter):
            continue
        if not include_inactive and not secret.is_active:
            continue

        related_agent_ids.add(int(share.owner_agent_id))
        _append_edge('incoming', share, secret)
        _upsert_collaborator(incoming_map, int(share.owner_agent_id), 'incoming', share, secret)

    if related_agent_ids:
        agents = Agent.query.filter(
            Agent.workspace_id == workspace_id,
            Agent.id.in_(list(related_agent_ids)),
        ).all()
        name_map = {
            int(item.id): (item.display_name or item.name)
            for item in agents
        }
    else:
        name_map = {}

    def _finalize_rows(source_map):
        rows = []
        for collaborator_id, row in source_map.items():
            row['agent_name'] = name_map.get(collaborator_id)
            row['secret_ids'] = sorted(list(row['secret_ids']))
            row['last_granted_at'] = row['last_granted_at'].isoformat() if row['last_granted_at'] else None
            row['last_updated_at'] = row['last_updated_at'].isoformat() if row['last_updated_at'] else None
            rows.append(row)

        rows.sort(
            key=lambda item: (
                -int(item['active_share_count']),
                -int(item['share_count']),
                -int(item['agent_id']),
            )
        )
        return rows

    outgoing_collaborators = _finalize_rows(outgoing_map)
    incoming_collaborators = _finalize_rows(incoming_map)

    stats = {
        'agent_id': agent_id,
        'project_id': project_id_filter,
        'include_inactive': include_inactive,
        'outgoing_share_count': sum(int(item['share_count']) for item in outgoing_collaborators),
        'incoming_share_count': sum(int(item['share_count']) for item in incoming_collaborators),
        'outgoing_agent_count': len(outgoing_collaborators),
        'incoming_agent_count': len(incoming_collaborators),
        'edge_count': len(edges),
        'active_edge_count': sum(1 for item in edges if item['is_active'] and not item['is_expired'] and item['secret_is_active']),
    }

    return ApiResponse.success(
        {
            'stats': stats,
            'outgoing_collaborators': outgoing_collaborators,
            'incoming_collaborators': incoming_collaborators,
            'edges': edges,
        },
        'Secret collaboration topology retrieved successfully',
    ).to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/shares', methods=['POST'])
@unified_auth_required
def create_agent_secret_shares(workspace_id, agent_id, secret_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = _get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    if not secret.is_active:
        return ApiResponse.error('Secret is revoked', 400).to_response()

    data = validate_json_request(
        optional_fields=[
            'target_agent_id',
            'target_agent_ids',
            'target_selector',
            'selector_project_id',
            'access_mode',
            'expires_at',
            'granted_reason',
        ],
    )
    if isinstance(data, tuple):
        return data

    target_agent_ids = []
    if data.get('target_agent_id') is not None:
        try:
            target_agent_ids.append(int(data.get('target_agent_id')))
        except (TypeError, ValueError):
            return ApiResponse.error('target_agent_id must be an integer', 400).to_response()

    raw_target_ids = data.get('target_agent_ids') or []
    if raw_target_ids:
        if not isinstance(raw_target_ids, list):
            return ApiResponse.error('target_agent_ids must be an array', 400).to_response()
        for raw in raw_target_ids:
            try:
                target_agent_ids.append(int(raw))
            except (TypeError, ValueError):
                return ApiResponse.error('target_agent_ids must contain integers', 400).to_response()

    target_selector = _normalize_target_selector(data.get('target_selector'))
    if target_selector not in TARGET_SELECTOR_MODES:
        return ApiResponse.error('Invalid target_selector', 400).to_response()

    selector_project_id = None
    if target_selector == 'project_agents':
        raw_selector_project_id = data.get('selector_project_id')
        if raw_selector_project_id in (None, ''):
            raw_selector_project_id = secret.project_id
        if raw_selector_project_id in (None, ''):
            return ApiResponse.error('selector_project_id is required for project_agents selector', 400).to_response()

        selector_project_id, project_err = _normalize_project_id_for_selector(workspace_id, raw_selector_project_id)
        if project_err:
            return project_err

    selector_target_agent_ids, selector_err = _resolve_target_agent_ids_by_selector(
        workspace_id=workspace_id,
        owner_agent_id=agent_id,
        selector_mode=target_selector,
        selector_project_id=selector_project_id,
    )
    if selector_err:
        return selector_err

    target_agent_ids = sorted(set(target_agent_ids + selector_target_agent_ids))
    if not target_agent_ids:
        return ApiResponse.error(
            'No target agents resolved, provide target_agent_id(s) or adjust target_selector',
            400,
        ).to_response()

    access_mode = str(data.get('access_mode') or 'read').strip().lower()
    if access_mode not in SHARE_ACCESS_MODES:
        return ApiResponse.error('Invalid access_mode', 400).to_response()

    expires_at, expires_err = _parse_expires_at(data.get('expires_at'))
    if expires_err:
        return expires_err

    granted_reason = str(data.get('granted_reason') or '').strip() or None

    touched = []
    created_count = 0
    updated_count = 0

    for target_agent_id in target_agent_ids:
        if target_agent_id == agent_id:
            return ApiResponse.error('Cannot share secret to the same agent', 400).to_response()

        target_agent = Agent.query.filter_by(id=target_agent_id, workspace_id=workspace_id).first()
        if not target_agent:
            return ApiResponse.error(f'target agent {target_agent_id} not found in workspace', 404).to_response()

        target_status = target_agent.status.value if hasattr(target_agent.status, 'value') else str(target_agent.status).lower()
        if target_status != 'active':
            return ApiResponse.error(f'target agent {target_agent_id} is not active', 400).to_response()

        share = AgentSecretShare.query.filter_by(
            workspace_id=workspace_id,
            secret_id=secret_id,
            owner_agent_id=agent_id,
            target_agent_id=target_agent_id,
            is_active=True,
        ).first()

        if share:
            share.access_mode = access_mode
            share.expires_at = expires_at
            share.granted_reason = granted_reason
            share.granted_by_user_id = user.id
            share.created_by = share.created_by or user.email
            updated_count += 1
        else:
            share = AgentSecretShare(
                workspace_id=workspace_id,
                secret_id=secret_id,
                owner_agent_id=agent_id,
                target_agent_id=target_agent_id,
                access_mode=access_mode,
                expires_at=expires_at,
                granted_reason=granted_reason,
                granted_by_user_id=user.id,
                created_by=user.email,
                is_active=True,
            )
            db.session.add(share)
            created_count += 1

        touched.append(share)

    write_agent_audit(
        event_type='agent_secret.shared',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret',
        target_id=secret.id,
        workspace_id=workspace_id,
        payload={
            'agent_id': agent_id,
            'secret_name': secret.name,
            'target_agent_ids': target_agent_ids,
            'target_selector': target_selector,
            'selector_project_id': selector_project_id,
            'access_mode': access_mode,
            'expires_at': expires_at.isoformat() if expires_at else None,
            'created_count': created_count,
            'updated_count': updated_count,
        },
        risk_score=65,
    )

    db.session.commit()
    return ApiResponse.success(
        {
            'items': [item.to_dict(include_agents=True) for item in touched],
            'summary': {
                'created': created_count,
                'updated': updated_count,
                'total': len(touched),
                'target_selector': target_selector,
                'selector_project_id': selector_project_id,
                'resolved_target_count': len(target_agent_ids),
            },
        },
        'Secret shared successfully',
    ).to_response()


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/<int:secret_id>/shares/<int:share_id>/revoke', methods=['POST'])
@unified_auth_required
def revoke_agent_secret_share(workspace_id, agent_id, secret_id, share_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = _get_secret_or_404(workspace_id, agent_id, secret_id)
    if err:
        return err

    share = AgentSecretShare.query.filter_by(
        id=share_id,
        workspace_id=workspace_id,
        secret_id=secret.id,
        owner_agent_id=agent_id,
    ).first()
    if not share:
        return ApiResponse.not_found('Secret share not found').to_response()

    if not share.is_active:
        return ApiResponse.error('Secret share is already revoked', 409).to_response()

    share.is_active = False
    share.revoked_by_user_id = user.id

    write_agent_audit(
        event_type='agent_secret.share_revoked',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_secret_share',
        target_id=share.id,
        workspace_id=workspace_id,
        payload={
            'agent_id': agent_id,
            'secret_id': secret.id,
            'target_agent_id': share.target_agent_id,
        },
        risk_score=60,
    )

    db.session.commit()
    return ApiResponse.success(share.to_dict(include_agents=True), 'Secret share revoked successfully').to_response()
