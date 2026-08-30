"""Task review routes — approve or reject tasks in REVIEW status."""

from flask import request

from models import db, Task, TaskStatus, TaskLog, TaskLogActorType
from api.base import ApiResponse, paginate_query
from core.auth import unified_auth_required, get_current_user

from . import tasks_bp


@tasks_bp.route('/workspaces/<int:workspace_id>/reviews/pending', methods=['GET'])
@unified_auth_required
def list_pending_reviews(workspace_id):
    """List tasks in REVIEW status belonging to projects in the given workspace.

    The workspace is represented by the Organization model. We find all
    projects owned by the organization, then filter tasks by REVIEW status.
    """
    from models import Project

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    project_ids = (
        db.session.query(Project.id)
        .filter(Project.organization_id == workspace_id)
        .subquery()
    )

    query = (
        Task.query
        .filter(Task.project_id.in_(db.session.query(project_ids.c.id)))
        .filter(Task.status == TaskStatus.REVIEW)
        .order_by(Task.updated_at.desc())
    )

    result = paginate_query(query, page=page, per_page=per_page)
    return ApiResponse.success(data=result).to_response()


@tasks_bp.route('/<int:task_id>/review', methods=['POST'])
@unified_auth_required
def submit_review(task_id):
    """Submit a review decision for a task.

    Body: { decision: 'approve'|'reject', comment?: string }
    """
    data = request.get_json()
    if not data or not data.get('decision'):
        return ApiResponse.error('decision is required (approve or reject)').to_response()

    decision = data['decision']
    if decision not in ('approve', 'reject'):
        return ApiResponse.error('decision must be approve or reject').to_response()

    task = db.session.get(Task, task_id)
    if not task:
        return ApiResponse.error('Task not found', 404).to_response()

    if task.status != TaskStatus.REVIEW:
        return ApiResponse.error('Task is not in REVIEW status', 400).to_response()

    user = get_current_user()
    comment = data.get('comment', '')

    if decision == 'approve':
        task.status = TaskStatus.DONE
        task.completion_rate = 100
        from datetime import datetime
        task.completed_at = datetime.utcnow()

        log_content = 'Review approved'
        if comment:
            log_content += f': {comment}'
    else:
        # Reject: send back to IN_PROGRESS with feedback
        task.status = TaskStatus.IN_PROGRESS

        log_content = 'Review rejected'
        if comment:
            log_content += f': {comment}'
            # Append to feedback_content
            existing_feedback = task.feedback_content or ''
            separator = '\n\n' if existing_feedback else ''
            task.feedback_content = existing_feedback + separator + f'[Rejected] {comment}'

    # Log the review decision
    log = TaskLog(
        task_id=task_id,
        actor_type=TaskLogActorType.HUMAN,
        actor_user_id=user.id if user else None,
        content=log_content,
        content_type='text/markdown',
    )
    db.session.add(log)
    db.session.commit()

    return ApiResponse.success(data=task.to_dict()).to_response()
