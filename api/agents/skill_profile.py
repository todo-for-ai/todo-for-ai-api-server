"""Agent 技能画像端点（P3.1 SOUL v2）

- GET  /agents/<id>/skill-profile          查看画像（含是否过期信息）
- POST /agents/<id>/skill-profile/rebuild  从运行历史重建画像（管理权限）
"""

from ._shared import (
    ApiResponse,
    agents_bp,
    db,
    get_current_user,
    unified_auth_required,
    Agent,
)
from ..agent_access_control import ensure_agent_detail_access
from ..agent_common import ensure_agent_manage_access, write_agent_audit
from services.skill_profile import build_skill_profile, rebuild_skill_profile


def _get_agent_or_404(agent_id: int):
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


@agents_bp.route("/<int:agent_id>/skill-profile", methods=["GET"])
@unified_auth_required
def get_skill_profile(agent_id: int):
    user = get_current_user()
    agent, not_found = _get_agent_or_404(agent_id)
    if not_found:
        return not_found
    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    from datetime import datetime

    profile = agent.skill_profile
    stale = None
    if profile and agent.skill_profile_updated_at:
        stale = (datetime.utcnow() - agent.skill_profile_updated_at).total_seconds() > 86400
    return ApiResponse.success(data={
        'agent_id': agent.id,
        'profile': profile,
        'updated_at': agent.skill_profile_updated_at.isoformat() if agent.skill_profile_updated_at else None,
        'stale': stale,
    }).to_response()


@agents_bp.route("/<int:agent_id>/skill-profile/rebuild", methods=["POST"])
@unified_auth_required
def rebuild_agent_skill_profile(agent_id: int):
    user = get_current_user()
    agent, not_found = _get_agent_or_404(agent_id)
    if not_found:
        return not_found
    access_err = ensure_agent_manage_access(user, agent)
    if access_err:
        return access_err

    try:
        profile = rebuild_skill_profile(agent.id)
    except ValueError as e:
        return ApiResponse.error(str(e), 404).to_response()

    write_agent_audit(
        event_type='agent.skill_profile.rebuilt',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        payload={'skill_count': len(profile.get('skills') or [])},
        risk_score=5,
    )
    return ApiResponse.success(
        data={'agent_id': agent.id, 'profile': profile},
        message='Skill profile rebuilt',
    ).to_response()
