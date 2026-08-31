"""Agent 记忆治理端点（P3.4）

记忆可查看、可审计、可遗忘：
- GET    /agents/<id>/memory/versions?kind=soul|skill_profile  统一版本历史（复用 AgentSoulVersion）
- GET    /agents/<id>/memory/audit?event_type=                 该 Agent 的记忆相关审计事件
- DELETE /agents/<id>/skill-profile                            遗忘技能画像（写墓碑版本 + 审计）
"""

from flask import request

from models import Agent, AgentAuditEvent, AgentSoulVersion, db
from ._shared import ApiResponse, agents_bp, get_current_user, unified_auth_required
from ..agent_access_control import ensure_agent_detail_access
from ..agent_common import ensure_agent_manage_access, write_agent_audit
from models.agent_soul_version import MEMORY_KINDS
from services.skill_profile import forget_skill_profile


def _get_agent_or_404(agent_id: int):
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()
    return agent, None


def _paged(query, default_per_page=20, max_per_page=100):
    try:
        page = max(int(request.args.get('page', 1)), 1)
        per_page = min(max(int(request.args.get('per_page', default_per_page)), 1), max_per_page)
    except (TypeError, ValueError):
        page, per_page = 1, default_per_page
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, {
        'page': page,
        'per_page': per_page,
        'total': total,
        'has_prev': page > 1,
        'has_next': page * per_page < total,
    }


@agents_bp.route("/<int:agent_id>/memory/versions", methods=["GET"])
@unified_auth_required
def list_memory_versions(agent_id: int):
    """统一记忆版本历史：SOUL 与技能画像快照同表共存，kind 可选过滤。"""
    user = get_current_user()
    agent, not_found = _get_agent_or_404(agent_id)
    if not_found:
        return not_found
    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    query = AgentSoulVersion.query.filter_by(agent_id=agent.id)
    kind = request.args.get('kind')
    if kind:
        if kind not in MEMORY_KINDS:
            return ApiResponse.error(f'invalid kind, one of {list(MEMORY_KINDS)}', 400).to_response()
        query = query.filter_by(memory_kind=kind)
    query = query.order_by(AgentSoulVersion.id.desc())

    items, pagination = _paged(query)
    return ApiResponse.success(data={
        'items': [row.to_dict() for row in items],
        'pagination': pagination,
    }).to_response()


@agents_bp.route("/<int:agent_id>/memory/audit", methods=["GET"])
@unified_auth_required
def list_memory_audit(agent_id: int):
    """该 Agent 的记忆相关审计事件（soul/画像 变更、回滚、遗忘、重建）。"""
    user = get_current_user()
    agent, not_found = _get_agent_or_404(agent_id)
    if not_found:
        return not_found
    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    query = AgentAuditEvent.query.filter_by(target_agent_id=agent.id)
    event_type = request.args.get('event_type')
    if event_type:
        query = query.filter(AgentAuditEvent.event_type == event_type)
    else:
        # 只返回记忆治理相关事件
        query = query.filter(AgentAuditEvent.event_type.in_([
            'agent.soul_updated',
            'agent.soul_rolled_back',
            'agent.skill_profile.rebuilt',
            'agent.skill_profile.forgotten',
        ]))
    query = query.order_by(AgentAuditEvent.id.desc())

    items, pagination = _paged(query)
    return ApiResponse.success(data={
        'items': [row.to_dict() for row in items],
        'pagination': pagination,
    }).to_response()


@agents_bp.route("/<int:agent_id>/skill-profile", methods=["DELETE"])
@unified_auth_required
def forget_agent_skill_profile(agent_id: int):
    """遗忘技能画像（合规）：清空画像、写墓碑版本、记审计。"""
    user = get_current_user()
    agent, not_found = _get_agent_or_404(agent_id)
    if not_found:
        return not_found
    access_err = ensure_agent_manage_access(user, agent)
    if access_err:
        return access_err

    try:
        forgotten = forget_skill_profile(agent.id, edited_by_user_id=user.id)
    except ValueError as e:
        return ApiResponse.error(str(e), 404).to_response()

    write_agent_audit(
        event_type='agent.skill_profile.forgotten',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        payload={'forgotten': True},
        risk_score=25,
    )
    return ApiResponse.success(
        data={'agent_id': agent.id, 'profile': forgotten},
        message='Skill profile forgotten',
    ).to_response()
