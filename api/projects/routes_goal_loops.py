"""GoalLoop 目标循环路由。

挂载在 projects 蓝图下：
- GET/POST /projects/<pid>/goal-loops
- GET  /goal-loops/<id>
- POST /goal-loops/<id>/pause|resume|stop|kick
（蓝图前缀为 /todo-for-ai/api/v1/projects，goal-loops/<id> 即
 /todo-for-ai/api/v1/projects/goal-loops/<id>，与项目子路由不冲突。）
"""

from flask import request

from models import db, GoalLoop, GoalLoopStatus, Project, Agent, AgentTaskAttempt
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse, handle_api_error
from services import goal_loop_service
from . import projects_bp


def _loop_with_tasks(loop: GoalLoop) -> dict:
    data = loop.to_dict()
    tasks = goal_loop_service.loop_tasks(loop.id)
    # 每轮任务的实际执行者（首个 attempt 的 agent）
    agent_ids = {}
    if tasks:
        attempts = (
            AgentTaskAttempt.query.filter(AgentTaskAttempt.task_id.in_([t.id for t in tasks]))
            .order_by(AgentTaskAttempt.id)
            .all()
        )
        for att in attempts:
            agent_ids.setdefault(att.task_id, att.agent_id)
    agents = {
        a.id: a for a in
        Agent.query.filter(Agent.id.in_(set(agent_ids.values()))).all()
    } if agent_ids else {}
    def _agent_name(task_id):
        aid = agent_ids.get(task_id)
        agent = agents.get(aid) if aid else None
        return (agent.display_name or agent.name) if agent else None

    data['tasks'] = [
        {
            'id': t.id,
            'title': t.title,
            'status': t.status.value if t.status else None,
            'agent_id': agent_ids.get(t.id),
            'agent_name': _agent_name(t.id),
        }
        for t in tasks
    ]
    data['rounds_done'] = len(data['tasks'])
    if loop.agent:
        data['agent_name'] = loop.agent.name
        data['agent_display_name'] = loop.agent.display_name
    if loop.director_agent_id:
        director = db.session.get(Agent, loop.director_agent_id)
        if director:
            data['director_name'] = director.name
            data['director_display_name'] = director.display_name
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
        from services.goal_loop.constants import DEFAULT_ROUNDS_LIMIT, DEFAULT_STALL_LIMIT
        rounds_limit = data.get('rounds_limit') or DEFAULT_ROUNDS_LIMIT
        time_budget_hours = data.get('time_budget_hours')
        stall_limit = data.get('stall_limit') or DEFAULT_STALL_LIMIT
        agent_id = data.get('agent_id')

        if not title:
            return ApiResponse.error('title is required', 400).to_response()
        if not goal_text:
            return ApiResponse.error('goal_text is required', 400).to_response()
        # 长跑护栏上限校验（服务层再做钳制）
        try:
            if rounds_limit is not None and not (1 <= int(rounds_limit) <= 2000):
                return ApiResponse.error('rounds_limit must be within 1..2000', 400).to_response()
            if time_budget_hours not in (None, '', 0) and not (1 <= int(time_budget_hours) <= 720):
                return ApiResponse.error('time_budget_hours must be within 1..720 (30 days)', 400).to_response()
            if stall_limit not in (None, '', 0) and not (1 <= int(stall_limit) <= 50):
                return ApiResponse.error('stall_limit must be within 1..50', 400).to_response()
        except (TypeError, ValueError):
            return ApiResponse.error('rounds_limit/time_budget_hours/stall_limit must be integers', 400).to_response()

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

        # 指挥者（可选）：负责拆解与评审；缺省 = 绑定 Agent（单 Agent 模式）
        director = None
        director_agent_id = data.get('director_agent_id')
        if director_agent_id:
            director = db.session.get(Agent, int(director_agent_id))
            if not director:
                return ApiResponse.error(
                    'Director agent not found', 400,
                    error_details={'code': 'DIRECTOR_NOT_FOUND'},
                ).to_response()
            if director.workspace_id != project.organization_id:
                return ApiResponse.error(
                    'Director agent workspace does not match the project organization', 400,
                    error_details={'code': 'DIRECTOR_WORKSPACE_MISMATCH'},
                ).to_response()

        loop = goal_loop_service.create_loop(
            project=project,
            agent=agent,
            title=title,
            goal_text=goal_text,
            done_definition=done_definition,
            rounds_limit=rounds_limit,
            created_by=current_user.id,
            director=director,
            time_budget_hours=time_budget_hours,
            stall_limit=stall_limit,
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


@projects_bp.route('/goal-loops/<int:loop_id>', methods=['PUT'])
@unified_auth_required
def update_goal_loop(loop_id: int):
    """调整长跑护栏（时长预算/轮数上限/受阻容忍）——用户设置"跑多久/多少轮才停"。

    limit_reached/stalled 的循环调参后 resume 即可继续推进；done/stopped 拒绝调整。
    """
    try:
        loop = _get_loop(loop_id)
        if not loop:
            return ApiResponse.error('Goal loop not found', 404).to_response()
        managed, error = _get_managed_project_or_error(loop.project_id)
        if error:
            return error

        data = request.get_json(silent=True) or {}
        rounds_limit = data.get('rounds_limit')
        time_budget_hours = data.get('time_budget_hours')
        stall_limit = data.get('stall_limit')

        try:
            if rounds_limit is not None and not (1 <= int(rounds_limit) <= 2000):
                return ApiResponse.error('rounds_limit must be within 1..2000', 400).to_response()
            if time_budget_hours not in (None, '', 0) and not (1 <= int(time_budget_hours) <= 720):
                return ApiResponse.error('time_budget_hours must be within 1..720 (30 days)', 400).to_response()
            if stall_limit not in (None, '', 0) and not (1 <= int(stall_limit) <= 50):
                return ApiResponse.error('stall_limit must be within 1..50', 400).to_response()
        except (TypeError, ValueError):
            return ApiResponse.error('rounds_limit/time_budget_hours/stall_limit must be integers', 400).to_response()

        if rounds_limit is None and time_budget_hours is None and stall_limit is None:
            return ApiResponse.error('nothing to update', 400).to_response()

        try:
            goal_loop_service.update_guardrails(
                loop_id,
                rounds_limit=rounds_limit,
                time_budget_hours=time_budget_hours,
                stall_limit=stall_limit,
            )
        except LookupError:
            return ApiResponse.error('Goal loop not found', 404).to_response()
        except ValueError:
            return ApiResponse.error(
                'Terminal loops (done/stopped) cannot be adjusted', 409,
                error_details={'code': 'GOAL_LOOP_TERMINAL'},
            ).to_response()

        return ApiResponse.success(
            data=_loop_with_tasks(_get_loop(loop_id)), message='Goal loop updated'
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
