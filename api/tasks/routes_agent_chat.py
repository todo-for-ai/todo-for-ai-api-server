"""Agent chat routes — allow agents to send chat messages on tasks."""

from flask import g, request

from models import db, TaskLog, TaskLogActorType
from api.base import ApiResponse
from api.agent_common import agent_session_required

from . import tasks_bp


@tasks_bp.route('/agent/<int:task_id>/chat', methods=['POST'])
@agent_session_required
def send_agent_chat(task_id):
    """Agent sends a chat message or reply on a task."""
    agent = g.current_agent
    data = request.get_json()
    if not data or not data.get('content'):
        return ApiResponse.error('content is required').to_response()

    content = data['content']
    parent_id = data.get('parent_id')

    if parent_id is not None:
        parent = db.session.get(TaskLog, parent_id)
        if not parent or parent.task_id != task_id:
            return ApiResponse.error('parent_id not found for this task', 404).to_response()

    log = TaskLog(
        task_id=task_id,
        actor_type=TaskLogActorType.AGENT,
        actor_agent_id=agent.id,
        content=content,
        content_type='text/markdown',
        parent_id=parent_id,
        created_by=f'agent:{agent.id}',
    )
    db.session.add(log)
    db.session.commit()

    # Push to user WebSocket
    from api.user_websocket import push_to_task_room
    push_to_task_room(task_id, 'task_comment', {
        'task_id': task_id,
        'message_id': log.id,
        'actor_type': 'agent',
        'actor_agent_id': agent.id,
        'content': content,
    })

    return ApiResponse.created(data=log.to_dict()).to_response()


@tasks_bp.route('/agent/<int:task_id>/chat', methods=['GET'])
@agent_session_required
def get_agent_chat(task_id):
    """Agent 拉取任务聊天（游标增量）。

    交互式会话的读取半边：daemon 在每次 attempt 构建 prompt 时带
    after_id（上次已读游标，存工作区锚点）拉取新增留言，把用户在
    平台上的追问注入引擎 prompt。返回按 id 升序。"""
    after_id = request.args.get('after_id', 0, type=int)
    limit = min(max(request.args.get('limit', 100, type=int), 1), 200)

    query = (
        TaskLog.query
        .filter(TaskLog.task_id == task_id, TaskLog.id > after_id)
        .order_by(TaskLog.id.asc())
        .limit(limit)
    )
    rows = query.all()
    messages = [
        {
            'id': row.id,
            'actor_type': row.actor_type.value if row.actor_type else None,
            'actor_user_id': row.actor_user_id,
            'actor_agent_id': row.actor_agent_id,
            'content': row.content,
            'parent_id': row.parent_id,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
    last_id = rows[-1].id if rows else after_id
    return ApiResponse.success(data={
        'task_id': task_id,
        'messages': messages,
        'last_id': last_id,
    }).to_response()
