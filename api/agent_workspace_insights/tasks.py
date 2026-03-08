from typing import Dict

from flask import request
from sqlalchemy import func, or_

from core.auth import get_current_user, unified_auth_required
from models import AgentTaskAttempt, Project, Task, TaskLog, TaskStatus, db

from ..agent_access_control import ensure_agent_detail_access
from ..base import ApiResponse, get_request_args
from . import agent_workspace_insights_bp
from .shared import _get_agent_or_404, _iso, _touched_task_ids_subquery

@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/tasks', methods=['GET'])
@unified_auth_required
def list_agent_tasks(workspace_id: int, agent_id: int):
    user = get_current_user()
    agent, err = _get_agent_or_404(workspace_id, agent_id)
    if err:
        return err

    access_err = ensure_agent_detail_access(actor_user=user, target_agent=agent)
    if access_err:
        return access_err

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    search_text = str(args.get('search') or '').strip()
    status_filter = str(request.args.get('status') or '').strip().lower()
    project_id_filter = request.args.get('project_id', type=int)

    touched_task_ids = _touched_task_ids_subquery(workspace_id, agent_id)
    query = (
        Task.query
        .join(Project, Project.id == Task.project_id)
        .filter(
            Project.organization_id == workspace_id,
            Task.id.in_(db.session.query(touched_task_ids.c.task_id)),
        )
    )

    if search_text:
        like = f"%{search_text}%"
        query = query.filter(or_(Task.title.like(like), Task.content.like(like)))

    if status_filter:
        if status_filter not in {item.value for item in TaskStatus}:
            return ApiResponse.error('Invalid status filter', 400).to_response()
        query = query.filter(Task.status == TaskStatus(status_filter))

    if project_id_filter:
        query = query.filter(Task.project_id == project_id_filter)

    total = query.count()
    task_rows = (
        query
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    task_ids = [int(row.id) for row in task_rows]
    attempt_rows = (
        AgentTaskAttempt.query
        .filter(
            AgentTaskAttempt.workspace_id == workspace_id,
            AgentTaskAttempt.agent_id == agent_id,
            AgentTaskAttempt.task_id.in_(task_ids if task_ids else [-1]),
        )
        .order_by(AgentTaskAttempt.started_at.desc(), AgentTaskAttempt.id.desc())
        .all()
    )
    last_attempt_by_task: Dict[int, AgentTaskAttempt] = {}
    for row in attempt_rows:
        task_id = int(row.task_id)
        if task_id not in last_attempt_by_task:
            last_attempt_by_task[task_id] = row

    log_stats_rows = (
        db.session.query(
            TaskLog.task_id,
            func.count(TaskLog.id).label('log_count'),
            func.max(TaskLog.created_at).label('last_log_at'),
        )
        .filter(
            TaskLog.actor_agent_id == agent_id,
            TaskLog.task_id.in_(task_ids if task_ids else [-1]),
        )
        .group_by(TaskLog.task_id)
        .all()
    )
    log_stats = {int(row.task_id): row for row in log_stats_rows}

    items = []
    for task in task_rows:
        last_attempt = last_attempt_by_task.get(int(task.id))
        log_stat = log_stats.get(int(task.id))
        last_attempt_activity = None
        if last_attempt:
            last_attempt_activity = last_attempt.ended_at or last_attempt.started_at
        last_log_at = getattr(log_stat, 'last_log_at', None)
        last_activity_at = last_attempt_activity
        if last_log_at and (not last_activity_at or last_log_at > last_activity_at):
            last_activity_at = last_log_at

        items.append(
            {
                'task_id': int(task.id),
                'title': task.title,
                'status': task.status.value if hasattr(task.status, 'value') else str(task.status),
                'priority': task.priority.value if hasattr(task.priority, 'value') else str(task.priority),
                'project_id': int(task.project_id),
                'project_name': task.project.name if task.project else None,
                'updated_at': _iso(task.updated_at),
                'completed_at': _iso(task.completed_at),
                'last_activity_at': _iso(last_activity_at),
                'last_attempt': (
                    {
                        'attempt_id': last_attempt.attempt_id,
                        'state': last_attempt.state.value if hasattr(last_attempt.state, 'value') else str(last_attempt.state),
                        'started_at': _iso(last_attempt.started_at),
                        'ended_at': _iso(last_attempt.ended_at),
                        'failure_code': last_attempt.failure_code,
                        'failure_reason': last_attempt.failure_reason,
                    }
                    if last_attempt
                    else None
                ),
                'agent_log_count': int(getattr(log_stat, 'log_count', 0) or 0),
            }
        )

    return ApiResponse.success(
        {
            'items': items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Agent tasks retrieved successfully',
    ).to_response()



