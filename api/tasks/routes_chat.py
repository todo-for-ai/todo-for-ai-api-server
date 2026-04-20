"""Task chat threading routes."""

from flask import request

from models import db, TaskLog, TaskLogActorType
from api.base import ApiResponse, paginate_query
from core.auth import unified_auth_required, get_current_user

from . import tasks_bp


@tasks_bp.route('/<int:task_id>/chat', methods=['GET'])
@unified_auth_required
def get_task_chat(task_id):
    """Get threaded chat messages for a task."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    # Fetch top-level messages (parent_id is None) ordered chronologically
    query = (
        TaskLog.query
        .filter(TaskLog.task_id == task_id, TaskLog.parent_id.is_(None))
        .order_by(TaskLog.created_at.asc())
    )

    result = paginate_query(query, page=page, per_page=per_page)
    items = result['items']

    # Attach replies to each top-level message
    for msg in items:
        msg_id = msg['id']
        replies = (
            TaskLog.query
            .filter(TaskLog.parent_id == msg_id)
            .order_by(TaskLog.created_at.asc())
            .all()
        )
        msg['replies'] = [r.to_dict() for r in replies]

    return ApiResponse.success(data=result).to_response()


@tasks_bp.route('/<int:task_id>/chat', methods=['POST'])
@unified_auth_required
def send_task_chat(task_id):
    """Send a chat message (or reply) on a task."""
    data = request.get_json()
    if not data or not data.get('content'):
        return ApiResponse.error('content is required').to_response()

    content = data['content']
    parent_id = data.get('parent_id')

    # If replying, verify parent belongs to same task
    if parent_id is not None:
        parent = db.session.get(TaskLog, parent_id)
        if not parent or parent.task_id != task_id:
            return ApiResponse.error('parent_id not found for this task', 404).to_response()

    user = get_current_user()
    log = TaskLog(
        task_id=task_id,
        actor_type=TaskLogActorType.HUMAN,
        actor_user_id=user.id if user else None,
        content=content,
        content_type='text/markdown',
        parent_id=parent_id,
    )
    db.session.add(log)
    db.session.commit()

    return ApiResponse.created(data=log.to_dict()).to_response()
