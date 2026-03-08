from datetime import datetime

from sqlalchemy import case, func

from core.auth import get_current_user, unified_auth_required
from models import AgentTaskAttempt, AgentTaskAttemptState, Project, Task, TaskLog, db

from ..agent_access_control import ensure_agent_detail_access
from ..base import ApiResponse, get_request_args
from . import agent_workspace_insights_bp
from .shared import _get_agent_or_404, _iso, _parse_iso_datetime, _value_to_int_list

@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/projects', methods=['GET'])
@unified_auth_required
def list_agent_projects(workspace_id: int, agent_id: int):
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
    search_text = str(args.get('search') or '').strip().lower()

    allowed_project_ids = set(_value_to_int_list(agent.allowed_project_ids))

    attempt_stats_rows = (
        db.session.query(
            Task.project_id.label('project_id'),
            func.count(func.distinct(AgentTaskAttempt.task_id)).label('attempt_task_count'),
            func.sum(
                case(
                    (AgentTaskAttempt.state == AgentTaskAttemptState.COMMITTED, 1),
                    else_=0,
                )
            ).label('committed_count'),
            func.max(func.coalesce(AgentTaskAttempt.ended_at, AgentTaskAttempt.started_at)).label('last_attempt_at'),
        )
        .join(Task, Task.id == AgentTaskAttempt.task_id)
        .join(Project, Project.id == Task.project_id)
        .filter(
            AgentTaskAttempt.workspace_id == workspace_id,
            AgentTaskAttempt.agent_id == agent_id,
            Project.organization_id == workspace_id,
        )
        .group_by(Task.project_id)
        .all()
    )
    attempt_stats = {int(row.project_id): row for row in attempt_stats_rows}

    log_stats_rows = (
        db.session.query(
            Task.project_id.label('project_id'),
            func.count(TaskLog.id).label('log_count'),
            func.count(func.distinct(TaskLog.task_id)).label('log_task_count'),
            func.max(TaskLog.created_at).label('last_log_at'),
        )
        .join(Task, Task.id == TaskLog.task_id)
        .join(Project, Project.id == Task.project_id)
        .filter(
            TaskLog.actor_agent_id == agent_id,
            Project.organization_id == workspace_id,
        )
        .group_by(Task.project_id)
        .all()
    )
    log_stats = {int(row.project_id): row for row in log_stats_rows}

    project_ids = set(allowed_project_ids)
    project_ids.update(attempt_stats.keys())
    project_ids.update(log_stats.keys())

    if not project_ids:
        return ApiResponse.success(
            {
                'items': [],
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total': 0,
                    'has_prev': False,
                    'has_next': False,
                },
            },
            'Agent projects retrieved successfully',
        ).to_response()

    project_rows = Project.query.filter(
        Project.id.in_(list(project_ids)),
        Project.organization_id == workspace_id,
    ).all()

    items = []
    for project in project_rows:
        attempt_row = attempt_stats.get(project.id)
        log_row = log_stats.get(project.id)

        attempt_task_count = int(getattr(attempt_row, 'attempt_task_count', 0) or 0)
        log_task_count = int(getattr(log_row, 'log_task_count', 0) or 0)
        touched_task_count = max(attempt_task_count, log_task_count)
        committed_count = int(getattr(attempt_row, 'committed_count', 0) or 0)
        interaction_log_count = int(getattr(log_row, 'log_count', 0) or 0)
        last_attempt_at = getattr(attempt_row, 'last_attempt_at', None)
        last_log_at = getattr(log_row, 'last_log_at', None)

        last_activity_at = last_attempt_at
        if last_log_at and (not last_activity_at or last_log_at > last_activity_at):
            last_activity_at = last_log_at

        item = {
            'project_id': project.id,
            'project_name': project.name,
            'project_status': project.status.value if hasattr(project.status, 'value') else str(project.status),
            'project_color': project.color,
            'is_explicitly_allowed': project.id in allowed_project_ids,
            'touched_task_count': touched_task_count,
            'committed_task_count': committed_count,
            'interaction_log_count': interaction_log_count,
            'last_activity_at': _iso(last_activity_at),
        }
        items.append(item)

    if search_text:
        items = [
            row
            for row in items
            if search_text in str(row.get('project_name') or '').lower()
        ]

    items.sort(
        key=lambda row: (
            _parse_iso_datetime(row.get('last_activity_at')) or datetime.min,
            int(row.get('project_id') or 0),
        ),
        reverse=True,
    )

    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page

    return ApiResponse.success(
        {
            'items': items[start:end],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Agent projects retrieved successfully',
    ).to_response()


