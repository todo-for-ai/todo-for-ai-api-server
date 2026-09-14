"""团队项目关联：列表/添加/移除（原样搬移）。"""



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

    data = validate_json_request(
        required_fields=['project_id'],
        optional_fields=['role', 'config'],
    )
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
