"""
Agent 团队管理 API

提供团队 CRUD 和成员管理功能
"""

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


agent_teams_bp = Blueprint('agent_teams', __name__)

TEAM_EDITABLE_FIELDS = ['name', 'description', 'avatar_url', 'config', 'default_strategy']


def _filter_team_editable_fields(data):
    return {k: v for k, v in data.items() if k in TEAM_EDITABLE_FIELDS}


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams', methods=['GET'])
@unified_auth_required
def list_teams(workspace_id):
    """列出团队"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    args = get_request_args()
    query = AgentTeam.query.filter_by(workspace_id=workspace_id)

    status_filter = request.args.get('status')
    if status_filter:
        # Enum 按 name 落库；API 传入的是小写 value，需先转枚举再比较
        try:
            status_enum = AgentTeamStatus(status_filter)
        except ValueError:
            status_enum = status_filter
        query = query.filter(AgentTeam.status == status_enum)
    else:
        query = query.filter(AgentTeam.status != AgentTeamStatus.ARCHIVED)

    if args['search']:
        search = f"%{args['search']}%"
        query = query.filter(
            db.or_(
                AgentTeam.name.ilike(search),
                AgentTeam.description.ilike(search)
            )
        )

    query = query.order_by(AgentTeam.updated_at.desc())
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [item.to_dict() for item in items],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Teams retrieved successfully',
    ).to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams', methods=['POST'])
@unified_auth_required
def create_team(workspace_id):
    """创建团队"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    data = validate_json_request(
        required_fields=['name'],
        optional_fields=TEAM_EDITABLE_FIELDS + [],
    )
    if isinstance(data, tuple):
        return data

    # 检查名称是否已存在
    existing = AgentTeam.query.filter_by(
        workspace_id=workspace_id,
        name=data['name']
    ).first()
    if existing:
        return ApiResponse.error('Team with this name already exists', 409).to_response()

    team_data = _filter_team_editable_fields(data)
    # name 已显式传入，从可编辑字段中剔除避免重复 kwarg
    team_data.pop('name', None)

    team = AgentTeam(
        workspace_id=workspace_id,
        created_by_user_id=user.id,
        name=data['name'],
        **team_data
    )

    db.session.add(team)
    db.session.commit()

    write_agent_audit(
        event_type='team.created',
        actor_type='user',
        actor_id=user.id,
        target_type='team',
        target_id=team.id,
        workspace_id=workspace_id,
        payload={'team_name': team.name}
    )

    return ApiResponse.created(team.to_dict(), 'Team created successfully').to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>', methods=['GET'])
@unified_auth_required
def get_team(workspace_id, team_id):
    """获取团队详情（包含成员）"""
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

    include_members = request.args.get('include_members', 'true').lower() == 'true'

    return ApiResponse.success(team.to_dict(include_members=include_members)).to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>', methods=['PUT'])
@unified_auth_required
def update_team(workspace_id, team_id):
    """更新团队"""
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

    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    team_data = _filter_team_editable_fields(data)

    # 如果改名，检查新名称是否已存在
    if 'name' in data and data['name'] != team.name:
        existing = AgentTeam.query.filter_by(
            workspace_id=workspace_id,
            name=data['name']
        ).first()
        if existing:
            return ApiResponse.error('Team with this name already exists', 409).to_response()
        team.name = data['name']

    for key, value in team_data.items():
        setattr(team, key, value)

    db.session.commit()

    write_agent_audit(
        event_type='team.updated',
        actor_type='user',
        actor_id=user.id,
        target_type='team',
        target_id=team.id,
        workspace_id=workspace_id,
        payload={'updated_fields': list(team_data.keys())}
    )

    return ApiResponse.success(team.to_dict(), 'Team updated successfully').to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>', methods=['DELETE'])
@unified_auth_required
def delete_team(workspace_id, team_id):
    """删除团队（软删除）"""
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

    team.status = AgentTeamStatus.ARCHIVED
    db.session.commit()

    write_agent_audit(
        event_type='team.archived',
        actor_type='user',
        actor_id=user.id,
        target_type='team',
        target_id=team.id,
        workspace_id=workspace_id,
    )

    return ApiResponse.success(None, 'Team archived successfully').to_response()


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


# ==================== 团队项目关联 ====================

@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/projects', methods=['GET'])
@unified_auth_required
def list_team_projects(workspace_id, team_id):
    """列出团队关联的项目"""
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

    projects = AgentTeamProject.query.filter_by(team_id=team_id).all()

    return ApiResponse.success({
        'items': [p.to_dict() for p in projects],
        'total': len(projects)
    }).to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/projects', methods=['POST'])
@unified_auth_required
def add_team_project(workspace_id, team_id):
    """将团队关联到项目"""
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

    data = validate_json_request(required_fields=['project_id'])
    if isinstance(data, tuple):
        return data

    project_id = data['project_id']

    # 检查项目是否存在
    from models import Project
    project = Project.query.filter_by(
        id=project_id,
        organization_id=workspace_id
    ).first()

    if not project:
        return ApiResponse.error('Project not found', 404).to_response()

    # 检查是否已关联
    existing = AgentTeamProject.query.filter_by(
        team_id=team_id,
        project_id=project_id
    ).first()

    if existing:
        return ApiResponse.error('Team is already associated with this project', 409).to_response()

    association = AgentTeamProject(
        team_id=team_id,
        project_id=project_id,
        workspace_id=workspace_id,
        added_by_user_id=user.id,
        config=data.get('config'),
        role=data.get('role', 'collaborator'),
    )

    db.session.add(association)

    # 更新任务计数
    team.task_count = AgentTeamProject.query.filter_by(team_id=team_id).count()

    db.session.commit()

    return ApiResponse.created(association.to_dict(), 'Project associated successfully').to_response()


@agent_teams_bp.route('/workspaces/<int:workspace_id>/agent-teams/<int:team_id>/projects/<int:project_id>', methods=['DELETE'])
@unified_auth_required
def remove_team_project(workspace_id, team_id, project_id):
    """解除团队与项目的关联"""
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

    association = AgentTeamProject.query.filter_by(
        team_id=team_id,
        project_id=project_id
    ).first()

    if not association:
        return ApiResponse.not_found('Association not found').to_response()

    db.session.delete(association)

    # 更新任务计数
    team.task_count = AgentTeamProject.query.filter_by(team_id=team_id).count()

    db.session.commit()

    return ApiResponse.success(None, 'Project association removed successfully').to_response()
