"""
Agent Runtime 鉴权 API
"""

from flask import Blueprint
from models import db, Agent, AgentKey, AgentSession
from .base import ApiResponse, validate_json_request
from .agent_common import write_agent_audit


agent_runtime_auth_bp = Blueprint('agent_runtime_auth', __name__)


@agent_runtime_auth_bp.route('/agent/auth/introspect', methods=['POST'])
def agent_auth_introspect():
    data = validate_json_request(required_fields=['agent_key'])
    if isinstance(data, tuple):
        return data

    raw_key = data['agent_key']
    key = AgentKey.verify_key(raw_key)
    if not key:
        return ApiResponse.unauthorized('Invalid agent key').to_response()

    agent = Agent.query.get(key.agent_id)
    if not agent or agent.status.value != 'active':
        return ApiResponse.unauthorized('Agent is inactive').to_response()

    session_row, raw_session_token = AgentSession.create_session(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        ttl_seconds=900,
    )
    db.session.add(session_row)

    write_agent_audit(
        event_type='agent.auth.introspect',
        actor_type='agent_key',
        actor_id=key.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        payload={'session_prefix': session_row.token_prefix},
    )

    db.session.commit()

    return ApiResponse.success(
        {
            'access_token': raw_session_token,
            'expires_in': 900,
            'agent': {
                'id': agent.id,
                'workspace_id': agent.workspace_id,
                'name': agent.name,
            },
        },
        'Agent authenticated successfully',
    ).to_response()
