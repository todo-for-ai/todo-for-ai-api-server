"""Agent help request routes — agents can explicitly request human assistance."""

from flask import g, request

from models import db, TaskLog, TaskLogActorType
from api.base import ApiResponse
from api.agent_common import agent_session_required

from . import tasks_bp


@tasks_bp.route('/agent/<int:task_id>/help-request', methods=['POST'])
@agent_session_required
def agent_help_request(task_id):
    """Agent requests human help on a task. Creates a chat message and pushes notification."""
    agent = g.current_agent
    data = request.get_json()
    if not data or not data.get('content'):
        return ApiResponse.error('content is required (describe what help you need)').to_response()

    content = data['content']
    help_type = data.get('help_type', 'general')  # general, blocked, clarification, review

    # Create a chat message from the agent
    help_content = f'**[求助 - {help_type}]** {content}'
    log = TaskLog(
        task_id=task_id,
        actor_type=TaskLogActorType.AGENT,
        actor_agent_id=agent.id,
        content=help_content,
        content_type='text/markdown',
        created_by=f'agent:{agent.id}',
    )
    db.session.add(log)
    db.session.commit()

    # Push help request notification to users
    from api.user_websocket import push_to_task_room, push_to_user
    from models import Task
    task = db.session.get(Task, task_id)

    push_to_task_room(task_id, 'help_request', {
        'task_id': task_id,
        'message_id': log.id,
        'agent_id': agent.id,
        'agent_name': agent.display_name or agent.name,
        'help_type': help_type,
        'content': content,
    })

    # Also push to task owner directly
    if task and task.created_by:
        try:
            owner_id = int(task.created_by.split(':')[-1]) if ':' in str(task.created_by) else None
            if owner_id:
                push_to_user(owner_id, 'help_request', {
                    'task_id': task_id,
                    'agent_name': agent.display_name or agent.name,
                    'help_type': help_type,
                    'content': content,
                })
        except (ValueError, TypeError):
            pass

    return ApiResponse.created(data={
        'message': log.to_dict(),
        'help_type': help_type,
    }).to_response()
