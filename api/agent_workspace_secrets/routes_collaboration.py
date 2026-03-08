from datetime import datetime

from flask import request
from sqlalchemy import or_

from models import Agent, AgentSecretShare, db
from core.auth import get_current_user, unified_auth_required
from ..agent_access_control import ensure_agent_detail_access
from ..agent_common import ensure_agent_manage_access, write_agent_audit
from ..base import ApiResponse, validate_json_request
from . import agent_workspace_secrets_bp
from .constants import SHARE_ACCESS_MODES, TARGET_SELECTOR_MODES
from .shared import (
    get_agent_or_404,
    get_secret_or_404,
    is_agent_active,
    normalize_project_id_for_selector,
    normalize_target_selector,
    parse_bool,
    parse_expires_at,
    resolve_target_agent_ids_by_selector,
)


@agent_workspace_secrets_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/secrets/collaboration', methods=['GET'])
@unified_auth_required
def get_agent_secret_collaboration(workspace_id, agent_id):
    user = get_current_user()
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    include_inactive = parse_bool(request.args.get('include_inactive'), default=False)
    now = datetime.utcnow()

    project_id_filter = None
    raw_project_id = request.args.get('project_id')
    if raw_project_id not in (None, ''):
        project_id_filter, project_err = normalize_project_id_for_selector(workspace_id, raw_project_id)
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

    target_selector = normalize_target_selector(data.get('target_selector'))
    if target_selector not in TARGET_SELECTOR_MODES:
        return ApiResponse.error('Invalid target_selector', 400).to_response()

    selector_project_id = None
    if target_selector == 'project_agents':
        raw_selector_project_id = data.get('selector_project_id')
        if raw_selector_project_id in (None, ''):
            raw_selector_project_id = secret.project_id
        if raw_selector_project_id in (None, ''):
            return ApiResponse.error('selector_project_id is required for project_agents selector', 400).to_response()

        selector_project_id, project_err = normalize_project_id_for_selector(workspace_id, raw_selector_project_id)
        if project_err:
            return project_err

    selector_target_agent_ids, selector_err = resolve_target_agent_ids_by_selector(
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

    expires_at, expires_err = parse_expires_at(data.get('expires_at'))
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

        if not is_agent_active(target_agent):
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
    agent, err = get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    secret, err = get_secret_or_404(workspace_id, agent_id, secret_id)
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
