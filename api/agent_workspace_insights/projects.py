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
    sort_by = args.get('sort_by') or 'last_activity_at'
    sort_order = args.get('sort_order') or 'desc'

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
            func.count(func.distinct(func.date(AgentTaskAttempt.started_at))).label('attempt_interaction_days'),
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
            func.count(func.distinct(func.date(TaskLog.created_at))).label('log_interaction_days'),
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
        attempt_interaction_days = int(getattr(attempt_row, 'attempt_interaction_days', 0) or 0)
        log_interaction_days = int(getattr(log_row, 'log_interaction_days', 0) or 0)
        interaction_days = max(attempt_interaction_days, log_interaction_days)

        last_activity_at = last_attempt_at
        if last_log_at and (not last_activity_at or last_log_at > last_activity_at):
            last_activity_at = last_log_at

        # 计算提交率 (避免除以0)
        submission_rate = (committed_count / touched_task_count * 100) if touched_task_count > 0 else 0

        # 计算活跃度分数 (基于多个因素)
        # 因素: 提交率(40%) + 交互日数(30%) + 最后活动时间(30%)
        now = datetime.utcnow()
        days_since_last_activity = (now - last_activity_at).days if last_activity_at else 999

        # 活跃度衰减: 30天内活跃为满分，超过30天线性衰减
        recency_score = max(0, 100 - (days_since_last_activity * 100 / 30)) if days_since_last_activity < 30 else 0
        activity_score = (submission_rate * 0.4) + (min(interaction_days * 10, 30)) + (recency_score * 0.3)
        activity_score = min(100, max(0, activity_score))

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
            'interaction_days': interaction_days,
            'submission_rate': round(submission_rate, 2),
            'activity_score': round(activity_score, 2),
        }
        items.append(item)

    if search_text:
        items = [
            row
            for row in items
            if search_text in str(row.get('project_name') or '').lower()
        ]

    # 支持的排序字段
    allowed_sort_fields = {
        'project_id': 'project_id',
        'project_name': 'project_name',
        'touched_task_count': 'touched_task_count',
        'committed_task_count': 'committed_task_count',
        'interaction_log_count': 'interaction_log_count',
        'interaction_days': 'interaction_days',
        'submission_rate': 'submission_rate',
        'activity_score': 'activity_score',
        'last_activity_at': 'last_activity_at',
    }

    # 获取排序字段和方向
    sort_field = allowed_sort_fields.get(sort_by, 'last_activity_at')
    reverse_order = sort_order.lower() == 'desc'

    # 执行排序
    def get_sort_key(row):
        value = row.get(sort_field)
        if sort_field == 'last_activity_at':
            return _parse_iso_datetime(value) or datetime.min
        elif sort_field == 'project_name':
            return str(value or '').lower()
        else:
            return value if value is not None else 0

    items.sort(key=get_sort_key, reverse=reverse_order)

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


