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
