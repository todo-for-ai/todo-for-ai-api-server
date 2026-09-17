"""Task agent control — 用户侧停止正在执行的 agent（交互式会话）."""

from api.base import ApiResponse
from api.agent_common import write_agent_audit
from core.auth import unified_auth_required, get_current_user
from models import (
    db,
    Task,
    TaskStatus,
    AgentTaskAttempt,
    AgentTaskAttemptState,
)

from . import tasks_bp


@tasks_bp.route('/<int:task_id>/agent/stop', methods=['POST'])
@unified_auth_required
def stop_agent_execution(task_id):
    """停止任务当前这次 agent 执行（交互式「停止」按钮）。

    双通道投递取消：WS 在线走 cancel_task 命令即时生效；离线场景
    由续约响应的 cancel_requested 兜底（≤一个续约周期）。任务置
    CANCELLED 后 daemon 以 cancelled 提交，attempt 走 ABORTED，
    复用既有 commit 语义，不新增状态。"""
    from api.agent_runtime_websocket import (
        find_active_attempt_agent_id,
        is_agent_connected,
        send_command_to_agent,
    )

    user = get_current_user()
    task = Task.query.get(task_id)
    if not task:
        return ApiResponse.error('Task not found', 404, error_details={'code': 'TASK_NOT_FOUND'}).to_response()
    if not user.can_access_project(task.project):
        return ApiResponse.error('Permission denied', 403, error_details={'code': 'PERMISSION_DENIED'}).to_response()

    attempt = AgentTaskAttempt.query.filter_by(
        task_id=task_id, state=AgentTaskAttemptState.ACTIVE,
    ).order_by(AgentTaskAttempt.id.desc()).first()
    if not attempt:
        return ApiResponse.error(
            'No active agent attempt on this task', 404,
            error_details={'code': 'NO_ACTIVE_ATTEMPT'},
        ).to_response()

    already_cancelled = task.status == TaskStatus.CANCELLED
    if not already_cancelled and task.status not in (TaskStatus.DONE,):
        task.cancel()

    agent_id = attempt.agent_id
    ws_online = is_agent_connected(agent_id)
    if ws_online:
        send_command_to_agent(agent_id, 'cancel_task', {
            'task_id': task_id,
            'attempt_id': attempt.attempt_id,
        })

    write_agent_audit(
        event_type='task.agent_stop_requested',
        actor_type='human',
        actor_id=user.id,
        target_type='task',
        target_id=task_id,
        workspace_id=attempt.workspace_id,
        payload={
            'attempt_id': attempt.attempt_id,
            'agent_id': agent_id,
            'transport': 'ws_command' if ws_online else 'lease_poll',
        },
    )
    db.session.commit()

    from api.user_websocket import push_to_task_room
    push_to_task_room(task_id, 'task_updated', {
        'task_id': task_id,
        'status': task.status.value if task.status else None,
        'reason': 'user_stop',
    })

    return ApiResponse.success(data={
        'task_id': task_id,
        'attempt_id': attempt.attempt_id,
        'agent_id': agent_id,
        'task_status': task.status.value if task.status else None,
        'transport': 'ws_command' if ws_online else 'lease_poll',
    }, message='Stop requested').to_response()
