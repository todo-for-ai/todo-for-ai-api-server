from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func, or_, and_

from core.auth import get_current_user, unified_auth_required
from models import TaskLog, User, Task, Project, db

from ..agent_access_control import ensure_agent_detail_access
from ..base import ApiResponse, get_request_args
from . import agent_workspace_insights_bp
from .shared import _get_agent_or_404, _iso, _touched_task_ids_subquery, _parse_iso_datetime

@agent_workspace_insights_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/insights/interactions', methods=['GET'])
@unified_auth_required
def list_agent_interactions(workspace_id: int, agent_id: int):
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

    # 新增筛选参数 - 使用 request.args 获取带类型转换的参数
    min_interactions = request.args.get('min_interactions', type=int)
    max_interactions = request.args.get('max_interactions', type=int)
    min_tasks = request.args.get('min_tasks', type=int)
    max_tasks = request.args.get('max_tasks', type=int)
    from_date = _parse_iso_datetime(request.args.get('from'))
    to_date = _parse_iso_datetime(request.args.get('to'))
    sort_by = request.args.get('sort_by', 'last_interaction_at')
    sort_order = request.args.get('sort_order', 'desc')

    touched_task_ids = _touched_task_ids_subquery(workspace_id, agent_id)

    interaction_query = (
        db.session.query(
            TaskLog.actor_user_id.label('user_id'),
            User.email.label('email'),
            User.username.label('username'),
            User.nickname.label('nickname'),
            User.full_name.label('full_name'),
            User.avatar_url.label('avatar_url'),
            User.created_at.label('user_created_at'),
            User.last_login_at.label('user_last_login_at'),
            func.count(TaskLog.id).label('interaction_count'),
            func.count(func.distinct(TaskLog.task_id)).label('task_count'),
            func.count(func.distinct(Task.project_id)).label('project_count'),
            func.sum(func.length(TaskLog.content)).label('total_content_length'),
            func.max(TaskLog.created_at).label('last_interaction_at'),
            func.min(TaskLog.created_at).label('first_interaction_at'),
        )
        .join(User, User.id == TaskLog.actor_user_id)
        .join(Task, Task.id == TaskLog.task_id)
        .filter(
            TaskLog.task_id.in_(db.session.query(touched_task_ids.c.task_id)),
            TaskLog.actor_user_id.isnot(None),
        )
        .group_by(
            TaskLog.actor_user_id,
            User.email,
            User.username,
            User.nickname,
            User.full_name,
            User.avatar_url,
            User.created_at,
            User.last_login_at,
        )
    )

    # 搜索筛选
    if search_text:
        like = f"%{search_text}%"
        interaction_query = interaction_query.filter(
            or_(
                User.email.like(like),
                User.username.like(like),
                User.nickname.like(like),
                User.full_name.like(like),
            )
        )

    # 时间范围筛选（聚合列不能进 WHERE，统一走 HAVING）
    having_clauses = []
    if from_date:
        having_clauses.append(func.max(TaskLog.created_at) >= from_date)
    if to_date:
        having_clauses.append(func.max(TaskLog.created_at) <= to_date)
    if min_interactions is not None:
        having_clauses.append(func.count(TaskLog.id) >= min_interactions)
    if max_interactions is not None:
        having_clauses.append(func.count(TaskLog.id) <= max_interactions)
    if min_tasks is not None:
        having_clauses.append(func.count(func.distinct(TaskLog.task_id)) >= min_tasks)
    if max_tasks is not None:
        having_clauses.append(func.count(func.distinct(TaskLog.task_id)) <= max_tasks)

    if having_clauses:
        interaction_query = interaction_query.having(and_(*having_clauses))

    # 排序
    allowed_sort_fields = {
        'user_id': TaskLog.actor_user_id,
        'interaction_count': func.count(TaskLog.id),
        'task_count': func.count(func.distinct(TaskLog.task_id)),
        'project_count': func.count(func.distinct(Task.project_id)),
        'last_interaction_at': func.max(TaskLog.created_at),
        'first_interaction_at': func.min(TaskLog.created_at),
    }
    sort_column = allowed_sort_fields.get(sort_by, func.max(TaskLog.created_at))
    if sort_order.lower() == 'asc':
        interaction_query = interaction_query.order_by(sort_column.asc(), TaskLog.actor_user_id.asc())
    else:
        interaction_query = interaction_query.order_by(sort_column.desc(), TaskLog.actor_user_id.desc())

    total = interaction_query.count()
    rows = (
        interaction_query
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    items = []
    for row in rows:
        display_name = row.full_name or row.nickname or row.username or row.email or f"User #{row.user_id}"

        # 计算活跃度分数
        interaction_count = int(row.interaction_count or 0)
        days_since_first = (datetime.utcnow() - row.first_interaction_at).days if row.first_interaction_at else 0
        avg_interactions_per_day = round(interaction_count / max(days_since_first, 1), 2)

        # 计算最后活跃天数
        days_since_last = (datetime.utcnow() - row.last_interaction_at).days if row.last_interaction_at else 999

        items.append(
            {
                'user_id': int(row.user_id),
                'display_name': display_name,
                'email': row.email,
                'username': row.username,
                'nickname': row.nickname,
                'full_name': row.full_name,
                'avatar_url': row.avatar_url,
                'user_created_at': _iso(row.user_created_at),
                'user_last_login_at': _iso(row.user_last_login_at),
                'interaction_count': interaction_count,
                'task_count': int(row.task_count or 0),
                'project_count': int(row.project_count or 0),
                'total_content_length': int(row.total_content_length or 0),
                'avg_content_length': round(int(row.total_content_length or 0) / max(interaction_count, 1)),
                'avg_interactions_per_day': avg_interactions_per_day,
                'first_interaction_at': _iso(row.first_interaction_at),
                'last_interaction_at': _iso(row.last_interaction_at),
                'days_since_last_interaction': days_since_last,
                'activity_score': min(100, int(interaction_count * 2 + (30 - min(days_since_last, 30)) * 1.5)),
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
            'filters': {
                'sort_by': sort_by,
                'sort_order': sort_order,
            }
        },
        'Agent interactions retrieved successfully',
    ).to_response()


