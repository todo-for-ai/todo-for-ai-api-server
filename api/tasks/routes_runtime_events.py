"""Task runtime events — 用户侧查询 agent 执行事件流（交互式控制台数据源）."""

from flask import request

from models import AgentTaskEvent, Task
from api.base import ApiResponse
from core.auth import unified_auth_required, get_current_user

from . import tasks_bp


@tasks_bp.route('/<int:task_id>/runtime-events', methods=['GET'])
@unified_auth_required
def get_task_runtime_events(task_id):
    """按游标拉取任务的 runtime 事件（AgentTaskEvent）。

    AgentRunConsole 的 REST 兜底通道：WS `task_runtime_event` 推送
    断线/错过的增量都能用 after_id 补齐。返回按 id 升序，默认给
    最近 limit 条（首次进入看尾部，增量从 after_id 追）。"""
    user = get_current_user()

    task = Task.query.get(task_id)
    if not task:
        return ApiResponse.error('Task not found', 404, error_details={'code': 'TASK_NOT_FOUND'}).to_response()
    if not user.can_access_project(task.project):
        return ApiResponse.error('Permission denied', 403, error_details={'code': 'PERMISSION_DENIED'}).to_response()

    after_id = request.args.get('after_id', 0, type=int)
    limit = min(max(request.args.get('limit', 200, type=int), 1), 500)

    query = AgentTaskEvent.query.filter(
        AgentTaskEvent.task_id == task_id,
        AgentTaskEvent.id > after_id,
    )
    rows = query.order_by(AgentTaskEvent.id.desc()).limit(limit).all()
    rows.reverse()

    items = [
        {
            'id': row.id,
            'attempt_id': row.attempt_id,
            'event_type': row.event_type,
            'seq': row.seq,
            'message': (row.message or '')[:4000],
            'payload': row.payload or {},
            'event_timestamp': row.event_timestamp.isoformat() if row.event_timestamp else None,
        }
        for row in rows
    ]
    return ApiResponse.success(data={
        'task_id': task_id,
        'items': items,
        'last_id': rows[-1].id if rows else after_id,
    }).to_response()
