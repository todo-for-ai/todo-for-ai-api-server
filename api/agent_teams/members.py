"""团队成员：列表/添加/更新/移除/排序（原样搬移）。"""



from flask import Blueprint, request
from sqlalchemy import func

from models import (
    db, AgentTeam, AgentTeamStatus, AgentTeamMember, AgentTeamMemberRole,
    AgentTeamProject, Agent, AgentStatus, Organization
)
from core.auth import unified_auth_required, get_current_user
from api.agent_common import (
    get_workspace_or_404, ensure_workspace_access,
    ensure_agent_manage_access, write_agent_audit
)
from api.base import ApiResponse, validate_json_request, get_request_args


from api.agent_teams._core import agent_teams_bp, TEAM_EDITABLE_FIELDS, _filter_team_editable_fields


# ==================== 团队成员管理 ====================

@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/members', methods=['GET'])
@unified_auth_required
def list_team_members(workspace_id, team_id):
    """列出团队成员"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    team = AgentTeam.query.filter_by(
        id=team_id,
        workspace_id=workspace_id
    ).first()

    if not team:
        return ApiResponse.not_found('Team not found').to_response()

    members = team.members.order_by(AgentTeamMember.order_index).all()

    return ApiResponse.success({
        'items': [m.to_dict() for m in members],
        'total': len(members)
    }).to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/members', methods=['POST'])
@unified_auth_required
def add_team_member(workspace_id, team_id):
    """添加成员到团队"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    team = AgentTeam.query.filter_by(
        id=team_id,
        workspace_id=workspace_id
    ).first()

    if not team:
        return ApiResponse.not_found('Team not found').to_response()

    # optional_fields 必须列全，否则白名单过滤会丢弃 role/responsibility/config
    data = validate_json_request(
        required_fields=['agent_id'],
        optional_fields=['role', 'responsibility', 'config'],
    )
    if isinstance(data, tuple):
        return data

    agent_id = data['agent_id']

    # 检查 Agent 是否存在且属于当前 workspace
    agent = Agent.query.filter_by(
        id=agent_id,
        workspace_id=workspace_id
    ).first()

    if not agent:
        return ApiResponse.error('Agent not found', 404).to_response()

    # 检查是否已是成员
    existing = AgentTeamMember.query.filter_by(
        team_id=team_id,
        agent_id=agent_id
    ).first()

    if existing:
        return ApiResponse.error('Agent is already a member of this team', 409).to_response()

    # 计算新的 order_index
    max_order = db.session.query(func.max(AgentTeamMember.order_index)).filter_by(team_id=team_id).scalar() or 0

    role_str = data.get('role', 'member')
    try:
        role = AgentTeamMemberRole(role_str)
    except ValueError:
        role = AgentTeamMemberRole.MEMBER

    member = AgentTeamMember(
        team_id=team_id,
        agent_id=agent_id,
        added_by_user_id=user.id,
        role=role,
        order_index=max_order + 1,
        responsibility=data.get('responsibility'),
        config=data.get('config'),
    )

    db.session.add(member)

    # 更新成员计数
    team.member_count = team.members.count()

    db.session.commit()

    write_agent_audit(
        event_type='team.member_added',
        actor_type='user',
        actor_id=user.id,
        target_type='team',
        target_id=team.id,
        workspace_id=workspace_id,
        payload={
            'agent_id': agent_id,
            'agent_name': agent.name,
            'role': role.value,
        }
    )

    return ApiResponse.created(member.to_dict(), 'Member added successfully').to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/members/<int:member_id>', methods=['PUT'])
@unified_auth_required
def update_team_member(workspace_id, team_id, member_id):
    """更新团队成员角色/配置"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    member = AgentTeamMember.query.join(AgentTeam).filter(
        AgentTeamMember.id == member_id,
        AgentTeam.id == team_id,
        AgentTeam.workspace_id == workspace_id
    ).first()

    if not member:
        return ApiResponse.not_found('Member not found').to_response()

    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    if 'role' in data:
        try:
            member.role = AgentTeamMemberRole(data['role'])
        except ValueError:
            pass

    if 'responsibility' in data:
        member.responsibility = data['responsibility']

    if 'config' in data:
        member.config = data['config']

    if 'notifications_enabled' in data:
        member.notifications_enabled = bool(data['notifications_enabled'])

    db.session.commit()

    return ApiResponse.success(member.to_dict(), 'Member updated successfully').to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/members/<int:member_id>', methods=['DELETE'])
@unified_auth_required
def remove_team_member(workspace_id, team_id, member_id):
    """从团队移除成员"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    team = AgentTeam.query.filter_by(
        id=team_id,
        workspace_id=workspace_id
    ).first()

    if not team:
        return ApiResponse.not_found('Team not found').to_response()

    member = AgentTeamMember.query.filter_by(
        id=member_id,
        team_id=team_id
    ).first()

    if not member:
        return ApiResponse.not_found('Member not found').to_response()

    agent_name = member.agent.name if member.agent else 'Unknown'

    db.session.delete(member)

    # 更新成员计数
    team.member_count = team.members.count()

    db.session.commit()

    write_agent_audit(
        event_type='team.member_removed',
        actor_type='user',
        actor_id=user.id,
        target_type='team',
        target_id=team.id,
        workspace_id=workspace_id,
        payload={
            'agent_id': member.agent_id,
            'agent_name': agent_name,
        }
    )

    return ApiResponse.success(None, 'Member removed successfully').to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/members/reorder', methods=['POST'])
@unified_auth_required
def reorder_team_members(workspace_id, team_id):
    """批量调整成员顺序"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    team = AgentTeam.query.filter_by(
        id=team_id,
        workspace_id=workspace_id
    ).first()

    if not team:
        return ApiResponse.not_found('Team not found').to_response()

    data = validate_json_request(required_fields=['orders'])
    if isinstance(data, tuple):
        return data

    orders = data['orders']  # [{"member_id": 1, "order_index": 0}, ...]

    for item in orders:
        member_id = item.get('member_id')
        order_index = item.get('order_index')

        if member_id is not None and order_index is not None:
            AgentTeamMember.query.filter_by(
                id=member_id,
                team_id=team_id
            ).update({'order_index': order_index})

    db.session.commit()

    return ApiResponse.success(None, 'Members reordered successfully').to_response()
