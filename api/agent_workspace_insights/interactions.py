from sqlalchemy import func, or_

from core.auth import get_current_user, unified_auth_required
from models import TaskLog, User, db

from ..agent_access_control import ensure_agent_detail_access
from ..base import ApiResponse, get_request_args
from . import agent_workspace_insights_bp
from .shared import _get_agent_or_404, _iso, _touched_task_ids_subquery

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

    touched_task_ids = _touched_task_ids_subquery(workspace_id, agent_id)

    interaction_query = (
        db.session.query(
            TaskLog.actor_user_id.label('user_id'),
            User.email.label('email'),
            User.username.label('username'),
            User.nickname.label('nickname'),
            User.full_name.label('full_name'),
            User.avatar_url.label('avatar_url'),
            func.count(TaskLog.id).label('interaction_count'),
            func.count(func.distinct(TaskLog.task_id)).label('task_count'),
            func.max(TaskLog.created_at).label('last_interaction_at'),
        )
        .join(User, User.id == TaskLog.actor_user_id)
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
        )
    )

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

    total = interaction_query.count()
    rows = (
        interaction_query
        .order_by(func.max(TaskLog.created_at).desc(), TaskLog.actor_user_id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    items = []
    for row in rows:
        display_name = row.full_name or row.nickname or row.username or row.email or f"User #{row.user_id}"
        items.append(
            {
                'user_id': int(row.user_id),
                'display_name': display_name,
                'email': row.email,
                'avatar_url': row.avatar_url,
                'interaction_count': int(row.interaction_count or 0),
                'task_count': int(row.task_count or 0),
                'last_interaction_at': _iso(row.last_interaction_at),
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
        'Agent interactions retrieved successfully',
    ).to_response()


