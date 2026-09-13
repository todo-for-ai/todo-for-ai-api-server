"""项目任务图（DAG）读侧端点：节点/边/就绪态/环组。

派发依赖门（agent_runtime_pull）保证 Agent 按 blocked_by 图序领任务；
本端点把图结构显式暴露——前端可视化「哪些任务可并行、哪些被卡住、
哪里成环」，外部编排方也可消费同一视图。
"""

from flask import request

from models import ProjectMember, ProjectMemberStatus
from services.task_graph import build_project_task_graph

from ._shared import (
    tasks_bp, db, Project,
    ApiResponse, unified_auth_required, get_current_user,
)


def _user_can_view_project(project, user):
    # User.is_admin 是方法（非 property），owner/成员走显式校验
    if user.is_admin() or project.owner_id == user.id:
        return True
    return db.session.query(ProjectMember.id).filter(
        ProjectMember.project_id == project.id,
        ProjectMember.user_id == user.id,
        ProjectMember.status == ProjectMemberStatus.ACTIVE,
    ).first() is not None


@tasks_bp.route('/projects/<int:project_id>/task-graph', methods=['GET'])
@unified_auth_required
def get_project_task_graph(project_id):
    user = get_current_user()
    project = db.session.get(Project, project_id)
    if not project:
        return ApiResponse.error('Project not found', 404).to_response()
    if not _user_can_view_project(project, user):
        return ApiResponse.error('Permission denied', 403).to_response()

    graph = build_project_task_graph(project_id)
    return ApiResponse.success(data=graph).to_response()
