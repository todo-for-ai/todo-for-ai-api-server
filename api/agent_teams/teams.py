"""团队 CRUD：列表/创建/详情/更新/删除（原样搬移）。"""



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
