"""
Workspace Agent SOUL 版本 API
"""

from flask import Blueprint
from models import db, Agent, AgentSoulVersion
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request, get_request_args
from .agent_common import ensure_workspace_access, ensure_agent_manage_access, write_agent_audit


agent_workspace_soul_bp = Blueprint('agent_workspace_soul', __name__)


def _get_agent_or_404(workspace_id, agent_id):
    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


@agent_workspace_soul_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/soul/versions', methods=['GET'])
@unified_auth_required
def list_soul_versions(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, agent.workspace)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    query = AgentSoulVersion.query.filter_by(agent_id=agent_id, workspace_id=workspace_id).order_by(
        AgentSoulVersion.version.desc()
    )
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [row.to_dict() for row in items],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'SOUL versions retrieved successfully',
    ).to_response()


@agent_workspace_soul_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/soul/versions/<int:version>', methods=['GET'])
@unified_auth_required
def get_soul_version(workspace_id, agent_id, version):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, agent.workspace)
    if access_err:
        return access_err

    row = AgentSoulVersion.query.filter_by(agent_id=agent_id, workspace_id=workspace_id, version=version).first()
    if not row:
        return ApiResponse.not_found('SOUL version not found').to_response()

    return ApiResponse.success(row.to_dict(), 'SOUL version retrieved successfully').to_response()


@agent_workspace_soul_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/soul/rollback', methods=['POST'])
@unified_auth_required
def rollback_soul_version(workspace_id, agent_id):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    data = validate_json_request(required_fields=['version'], optional_fields=['change_summary'])
    if isinstance(data, tuple):
        return data

    try:
        target_version = int(data['version'])
    except Exception:
        return ApiResponse.error('version must be integer', 400).to_response()

    if target_version <= 0:
        return ApiResponse.error('version must be positive', 400).to_response()

    target = AgentSoulVersion.query.filter_by(
        agent_id=agent_id,
        workspace_id=workspace_id,
        version=target_version,
    ).first()
    if not target:
        return ApiResponse.not_found('SOUL version not found').to_response()

    old_version = agent.soul_version or 1
    agent.soul_markdown = target.soul_markdown
    agent.soul_version = old_version + 1
    agent.config_version = (agent.config_version or 1) + 1

    snapshot = AgentSoulVersion(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        version=agent.soul_version,
        soul_markdown=agent.soul_markdown or '',
        change_summary=(data.get('change_summary') or f'rollback to v{target_version}')[:255],
        edited_by_user_id=user.id,
        created_by=user.email,
    )
    db.session.add(snapshot)

    write_agent_audit(
        event_type='agent.soul_rolled_back',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=workspace_id,
        payload={
            'rollback_from_version': old_version,
            'rollback_target_version': target_version,
            'new_soul_version': agent.soul_version,
            'config_version': agent.config_version,
        },
        risk_score=20,
    )

    db.session.commit()
    return ApiResponse.success(agent.to_dict(), 'SOUL rolled back successfully').to_response()
