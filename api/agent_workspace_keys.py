"""
Workspace Agent Key 与 Connect Link API
"""

from datetime import timedelta
from urllib.parse import urlencode
from flask import Blueprint, current_app
from models import db, Agent, AgentKey, AgentConnectLink
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request
from .agent_common import (
    ensure_agent_manage_access,
    write_agent_audit,
    sign_link_payload,
    now_utc,
)
from .agent_access_control import ensure_agent_detail_access


agent_workspace_keys_bp = Blueprint('agent_workspace_keys', __name__)


@agent_workspace_keys_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/keys', methods=['GET'])
@unified_auth_required
def list_agent_keys(workspace_id, agent_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    items = AgentKey.query.filter_by(agent_id=agent_id, workspace_id=workspace_id).order_by(AgentKey.created_at.desc()).all()
    return ApiResponse.success({'items': [row.to_dict() for row in items]}, 'Agent keys retrieved successfully').to_response()


@agent_workspace_keys_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/keys', methods=['POST'])
@unified_auth_required
def create_agent_key(workspace_id, agent_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(required_fields=['name'])
    if isinstance(data, tuple):
        return data

    key, raw_token = AgentKey.generate_key(
        name=data['name'].strip(),
        workspace_id=workspace_id,
        agent_id=agent_id,
        created_by_user_id=user.id,
    )
    key.created_by = user.email

    db.session.add(key)
    write_agent_audit(
        event_type='agent_key.created',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_key',
        target_id='pending',
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'name': key.name},
        risk_score=50,
    )

    db.session.commit()
    result = key.to_dict()
    result['token'] = raw_token
    return ApiResponse.created(result, 'Agent key created successfully').to_response()


@agent_workspace_keys_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/keys/<int:key_id>/reveal', methods=['POST'])
@unified_auth_required
def reveal_agent_key(workspace_id, agent_id, key_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    key = AgentKey.query.filter_by(id=key_id, agent_id=agent_id, workspace_id=workspace_id).first()
    if not key:
        return ApiResponse.not_found('Agent key not found').to_response()

    token = key.reveal()
    if not token:
        return ApiResponse.error('Unable to decrypt key', 400).to_response()

    write_agent_audit(
        event_type='agent_key.revealed',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_key',
        target_id=key.id,
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'prefix': key.prefix},
        risk_score=90,
    )

    db.session.commit()
    return ApiResponse.success({'key_id': key.id, 'token': token}, 'Agent key revealed successfully').to_response()


@agent_workspace_keys_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/keys/<int:key_id>/revoke', methods=['POST'])
@unified_auth_required
def revoke_agent_key(workspace_id, agent_id, key_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    key = AgentKey.query.filter_by(id=key_id, agent_id=agent_id, workspace_id=workspace_id).first()
    if not key:
        return ApiResponse.not_found('Agent key not found').to_response()

    key.revoke()
    write_agent_audit(
        event_type='agent_key.revoked',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_key',
        target_id=key.id,
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'prefix': key.prefix},
        risk_score=70,
    )

    db.session.commit()
    return ApiResponse.success(key.to_dict(), 'Agent key revoked successfully').to_response()


@agent_workspace_keys_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/connect-link', methods=['POST'])
@unified_auth_required
def create_connect_link(workspace_id, agent_id):
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(optional_fields=['ttl_seconds'])
    if isinstance(data, tuple):
        return data

    ttl_seconds = int(data.get('ttl_seconds', 600)) if data else 600
    ttl_seconds = max(60, min(ttl_seconds, 3600))

    key = AgentKey.query.filter_by(agent_id=agent_id, workspace_id=workspace_id, is_active=True).order_by(AgentKey.id.desc()).first()
    if not key:
        return ApiResponse.error('No active key found for this agent', 400).to_response()

    raw_key = key.reveal()
    if not raw_key:
        return ApiResponse.error('Unable to decrypt key', 400).to_response()

    now = now_utc()
    expires_at = now + timedelta(seconds=ttl_seconds)
    ts = int(now.timestamp())
    payload = urlencode({'workspace': workspace_id, 'agent': agent_id, 'key': raw_key, 'ts': ts})
    sig = sign_link_payload(payload)

    host = current_app.config.get('FRONTEND_URL') or 'https://todo4ai.org'
    url = f"{host}/agent/connect?{payload}&sig={sig}"

    link = AgentConnectLink(
        workspace_id=workspace_id,
        agent_id=agent_id,
        key_id=key.id,
        created_by_user_id=user.id,
        url=url,
        signature=sig,
        expires_at=expires_at,
        created_by=user.email,
    )
    db.session.add(link)

    write_agent_audit(
        event_type='agent_link.generated',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_connect_link',
        target_id='pending',
        workspace_id=workspace_id,
        payload={'agent_id': agent_id, 'key_id': key.id, 'expires_at': expires_at.isoformat()},
        risk_score=60,
    )

    db.session.commit()
    return ApiResponse.created({'url': url, 'expires_at': expires_at.isoformat()}, 'Connect link generated successfully').to_response()
