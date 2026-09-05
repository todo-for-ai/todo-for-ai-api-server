"""GoalLoop 目标循环路由。

挂载在 projects 蓝图下：
- GET/POST /projects/<pid>/goal-loops
- GET  /goal-loops/<id>
- POST /goal-loops/<id>/pause|resume|stop|kick
（蓝图前缀为 /todo-for-ai/api/v1/projects，goal-loops/<id> 即
 /todo-for-ai/api/v1/projects/goal-loops/<id>，与项目子路由不冲突。）
"""

from flask import request

from models import db, GoalLoop, GoalLoopStatus, Project, Agent
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse, handle_api_error
from services import goal_loop_service
from . import projects_bp


def _loop_with_tasks(loop: GoalLoop) -> dict:
    data = loop.to_dict()
    data['tasks'] = [
        {
            'id': t.id,
            'title': t.title,
            'status': t.status.value if t.status else None,
        }
        for t in goal_loop_service.loop_tasks(loop.id)
    ]
    data['rounds_done'] = len(data['tasks'])
    if loop.agent:
        data['agent_name'] = loop.agent.name
        data['agent_display_name'] = loop.agent.display_name
    return data


def _get_managed_project_or_error(project_id: int):
    """返回 (project, error_response)；权限=can_manage_project。"""
    current_user = get_current_user()
    project = db.session.get(Project, project_id)
    if not project:
        return None, ApiResponse.error(
            'Project not found', 404, error_details={'code': 'PROJECT_NOT_FOUND'}
        ).to_response()
    if not current_user.can_manage_project(project):
        return None, ApiResponse.error(
            'Access denied', 403, error_details={'code': 'PERMISSION_DENIED'}
        ).to_response()
    return (project, current_user), None


@projects_bp.route('/<int:project_id>/goal-loops', methods=['GET'])
@unified_auth_required
def list_goal_loops(project_id: int):
    try:
        current_user = get_current_user()
        project = db.session.get(Project, project_id)
        if not project:
            return ApiResponse.error('Project not found', 404).to_response()
        if not current_user.can_access_project(project):
            return ApiResponse.error('Access denied', 403).to_response()

        loops = (
            GoalLoop.query.filter_by(project_id=project_id)
            .order_by(GoalLoop.id.desc())
            .all()
        )
        return ApiResponse.success(
            data={'goal_loops': [_loop_with_tasks(l) for l in loops]},
            message='Goal loops retrieved',
        ).to_response()
    except Exception as e:  # noqa: BLE001
        return handle_api_error(e)


@projects_bp.route('/<int:project_id>/goal-loops', methods=['POST'])
@unified_auth_required
def create_goal_loop(project_id: int):
    try:
        managed, error = _get_managed_project_or_error(project_id)
        if error:
            return error
        project, current_user = managed

        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        goal_text = (data.get('goal_text') or '').strip()
        done_definition = (data.get('done_definition') or '').strip()
        rounds_limit = data.get('rounds_limit') or 10
        agent_id = data.get('agent_id')

        if not title:
            return ApiResponse.error('title is required', 400).to_response()
        if not goal_text:
            return ApiResponse.error('goal_text is required', 400).to_response()

        agent = None
        if agent_id:
            agent = db.session.get(Agent, int(agent_id))
        else:
            agent = (
                Agent.query.filter_by(
                    workspace_id=project.organization_id,
                    runner_enabled=True,
                    status='ACTIVE',
                )
                .order_by(Agent.id)
                .first()
            )
        if not agent:
            return ApiResponse.error(
                'No active agent available in the project workspace', 400,
                error_details={'code': 'NO_ACTIVE_AGENT'},
            ).to_response()
        # agent 平面按组织过滤任务：绑定 agent 必须与项目同工作区，否则任务不可见
        if not project.organization_id or agent.workspace_id != project.organization_id:
            return ApiResponse.error(
                'Agent workspace does not match the project organization', 400,
                error_details={'code': 'AGENT_WORKSPACE_MISMATCH'},
            ).to_response()

        loop = goal_loop_service.create_loop(
            project=project,
            agent=agent,
            title=title,
            goal_text=goal_text,
            done_definition=done_definition,
            rounds_limit=rounds_limit,
            created_by=current_user.id,
        )
        return ApiResponse.success(
            data=_loop_with_tasks(loop), message='Goal loop created'
        ).to_response()
    except Exception as e:  # noqa: BLE001
        return handle_api_error(e)


def _get_loop(loop_id: int):
    return db.session.get(GoalLoop, loop_id)


@projects_bp.route('/goal-loops/<int:loop_id>', methods=['GET'])
@unified_auth_required
def get_goal_loop(loop_id: int):
    try:
        current_user = get_current_user()
        loop = _get_loop(loop_id)
        if not loop:
            return ApiResponse.error('Goal loop not found', 404).to_response()
        project = db.session.get(Project, loop.project_id)
        if not current_user.can_access_project(project):
            return ApiResponse.error('Access denied', 403).to_response()
        return ApiResponse.success(
            data=_loop_with_tasks(loop), message='Goal loop retrieved'
        ).to_response()
    except Exception as e:  # noqa: BLE001
        return handle_api_error(e)


def _loop_action(loop_id: int, status: GoalLoopStatus, kick_after_resume=False):
    try:
        loop = _get_loop(loop_id)
        if not loop:
            return ApiResponse.error('Goal loop not found', 404).to_response()
        managed, error = _get_managed_project_or_error(loop.project_id)
        if error:
            return error

        try:
            goal_loop_service.set_status(loop_id, status)
        except LookupError:
            return ApiResponse.error('Goal loop not found', 404).to_response()

        if kick_after_resume:
            goal_loop_service.maybe_advance(loop_id)

        return ApiResponse.success(
            data=_loop_with_tasks(_get_loop(loop_id)), message='Goal loop updated'
        ).to_response()
    except Exception as e:  # noqa: BLE001
        return handle_api_error(e)


@projects_bp.route('/goal-loops/<int:loop_id>/pause', methods=['POST'])
@unified_auth_required
def pause_goal_loop(loop_id: int):
    return _loop_action(loop_id, GoalLoopStatus.PAUSED)


@projects_bp.route('/goal-loops/<int:loop_id>/resume', methods=['POST'])
@unified_auth_required
def resume_goal_loop(loop_id: int):
    return _loop_action(loop_id, GoalLoopStatus.RUNNING, kick_after_resume=True)


@projects_bp.route('/goal-loops/<int:loop_id>/stop', methods=['POST'])
@unified_auth_required
def stop_goal_loop(loop_id: int):
    return _loop_action(loop_id, GoalLoopStatus.STOPPED)


@projects_bp.route('/goal-loops/<int:loop_id>/kick', methods=['POST'])
@unified_auth_required
def kick_goal_loop(loop_id: int):
    """手动兜底推进（agent 宕机等遗漏触发场景）。"""
    try:
        loop = _get_loop(loop_id)
        if not loop:
            return ApiResponse.error('Goal loop not found', 404).to_response()
        managed, error = _get_managed_project_or_error(loop.project_id)
        if error:
            return error
        result = goal_loop_service.maybe_advance(loop_id)
        return ApiResponse.success(
            data={'result': result, 'loop': _loop_with_tasks(_get_loop(loop_id))},
            message='Goal loop kicked',
        ).to_response()
    except Exception as e:  # noqa: BLE001
        return handle_api_error(e)
