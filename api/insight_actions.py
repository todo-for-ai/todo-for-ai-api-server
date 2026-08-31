"""洞察行动端点（P3.3 洞察落地为动作）

- GET  /agents/<id>/load-forecast                     Agent 负载预测明细
- GET  /projects/<id>/insights/dod-recommendations    返工分析 → DoD 模板推荐
- POST /projects/<id>/insights/dod-recommendations/apply  一键合并推荐模板进任务 DoD
"""

from flask import Blueprint, request

from models import Task, db
from core.auth import get_current_user, unified_auth_required
from .base import ApiResponse, validate_json_request
from .agent_access_control import ensure_agent_detail_access
from .agent_common import write_agent_audit
from services.insight_actions import (
    apply_dod_template,
    apply_mentorship_pair,
    predict_agent_load,
    recommend_dod_templates,
    recommend_mentorship_pairs,
)

insight_actions_bp = Blueprint('insight_actions', __name__)


@insight_actions_bp.route('/agents/<int:agent_id>/load-forecast', methods=['GET'])
@unified_auth_required
def agent_load_forecast(agent_id: int):
    """Agent 负载预测（吞吐/积压天数/是否超载节流）。"""
    from models import Agent

    user = get_current_user()
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()
    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    try:
        window_days = max(1, min(90, int(request.args.get('window_days', 7))))
    except (TypeError, ValueError):
        window_days = 7

    load = predict_agent_load(agent, window_days=window_days)
    load['throttled_in_dispatch'] = load['overloaded']
    return ApiResponse.success(data={'agent_id': agent.id, **load}).to_response()


def _get_project_or_404(project_id: int):
    from models import Project

    project = db.session.get(Project, project_id)
    if not project:
        return None, ApiResponse.not_found('Project not found').to_response()
    return project, None


@insight_actions_bp.route('/projects/<int:project_id>/insights/dod-recommendations', methods=['GET'])
@unified_auth_required
def project_dod_recommendations(project_id: int):
    """返工分析 → DoD 模板推荐（按自愈修复类别聚合）。"""
    user = get_current_user()
    project, not_found = _get_project_or_404(project_id)
    if not_found:
        return not_found
    if not user.can_access_project(project):
        return ApiResponse.forbidden('Access denied').to_response()

    try:
        days = max(1, min(365, int(request.args.get('days', 30))))
    except (TypeError, ValueError):
        days = 30

    return ApiResponse.success(data=recommend_dod_templates(project_id, days=days)).to_response()


@insight_actions_bp.route('/projects/<int:project_id>/insights/dod-recommendations/apply', methods=['POST'])
@unified_auth_required
def apply_project_dod_recommendations(project_id: int):
    """把推荐模板合并进指定任务的 DoD（项目管理权限；按 type+value 去重）。"""
    user = get_current_user()
    data = validate_json_request(
        required_fields=['task_id', 'categories'],
    )
    if isinstance(data, tuple):
        return data
    project, not_found = _get_project_or_404(project_id)
    if not_found:
        return not_found
    if not user.can_manage_project(project):
        return ApiResponse.forbidden('Access denied').to_response()

    task_id = data.get('task_id')
    categories = data.get('categories') or []
    if not task_id or not categories:
        return ApiResponse.error('task_id and categories are required', 400).to_response()

    task = db.session.get(Task, int(task_id))
    if not task or task.project_id != project_id:
        return ApiResponse.not_found('Task not found in this project').to_response()

    result = apply_dod_template(task, [str(c) for c in categories])
    if result['added']:
        write_agent_audit(
            event_type='insight.dod_template_applied',
            actor_type='user',
            actor_id=user.id,
            target_type='task',
            target_id=task.id,
            workspace_id=project.organization_id if project else None,
            payload={'added': result['added'], 'categories': categories},
            risk_score=5,
        )
    return ApiResponse.success(
        data={'task_id': task.id, **result},
        message='DoD template applied' if result['added'] else 'No new DoD items to add',
    ).to_response()


# ── 知识传播网络 → 导师制编排 ────────────────────────────────────────────

def _get_workspace_or_404(workspace_id: int):
    from models import Organization

    workspace = db.session.get(Organization, workspace_id)
    if not workspace:
        return None, ApiResponse.not_found('Workspace not found').to_response()
    return workspace, None


@insight_actions_bp.route('/workspaces/<int:workspace_id>/insights/mentorship-recommendations', methods=['GET'])
@unified_auth_required
def mentorship_recommendations(workspace_id: int):
    """知识传播网络 → 导师制配对建议（高产出知识 Agent × 低覆盖 Agent）。"""
    from .agent_common import ensure_workspace_manage_access

    user = get_current_user()
    workspace, not_found = _get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    try:
        limit = max(1, min(20, int(request.args.get('limit', 5))))
        window_days = max(1, min(365, int(request.args.get('window_days', 90))))
    except (TypeError, ValueError):
        limit, window_days = 5, 90

    return ApiResponse.success(
        data=recommend_mentorship_pairs(workspace_id, limit=limit, window_days=window_days),
    ).to_response()


@insight_actions_bp.route('/workspaces/<int:workspace_id>/insights/mentorship-recommendations/apply', methods=['POST'])
@unified_auth_required
def apply_mentorship(workspace_id: int):
    """确认导师制建议 → 落地为协作编排（双成员团队 + 导师经验共享）。"""
    from .agent_common import ensure_workspace_manage_access, write_agent_audit as _audit
    from models import Agent

    user = get_current_user()
    data = validate_json_request(required_fields=['mentor_id', 'mentee_id', 'domain'])
    if isinstance(data, tuple):
        return data
    workspace, not_found = _get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    mentor = db.session.get(Agent, int(data['mentor_id']))
    mentee = db.session.get(Agent, int(data['mentee_id']))
    if not mentor or mentor.workspace_id != workspace_id:
        return ApiResponse.not_found('Mentor agent not found in this workspace').to_response()
    if not mentee or mentee.workspace_id != workspace_id:
        return ApiResponse.not_found('Mentee agent not found in this workspace').to_response()
    if mentor.id == mentee.id:
        return ApiResponse.error('mentor and mentee must differ', 400).to_response()

    result = apply_mentorship_pair(
        mentor, mentee, str(data['domain']), created_by_user_id=user.id,
    )
    _audit(
        event_type='insight.mentorship_applied',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_team',
        target_id=result['team_id'],
        workspace_id=workspace_id,
        payload={
            'mentor_id': mentor.id, 'mentee_id': mentee.id,
            'domain': data['domain'], 'created': result['created'],
            'shared_experiences': result['shared_experiences'],
        },
        risk_score=10,
    )
    return ApiResponse.success(
        data={'workspace_id': workspace_id, **result},
        message='Mentorship orchestration applied',
    ).to_response()
