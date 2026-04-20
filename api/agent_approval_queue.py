"""
Pending approvals queue API - list and stats for interaction requests awaiting human decision.
"""

from flask import Blueprint, request

from core.auth import get_current_user, unified_auth_required
from models import Agent, AgentTaskEvent, Task, db

from .agent_common import ensure_workspace_access, get_workspace_or_404
from .base import ApiResponse

approval_queue_bp = Blueprint('agent_approval_queue', __name__)

INTERACTION_REQUEST_EVENT_TYPE = 'interaction_request'
INTERACTION_APPROVAL_EVENT_TYPE = 'interaction_approval'


def _resolve_agent_name(agent_id: int) -> str:
    """Resolve agent display name from agent_id, falling back to raw id."""
    agent = db.session.get(Agent, agent_id)
    if agent:
        return agent.display_name or agent.name or f'Agent #{agent_id}'
    return f'Agent #{agent_id}'


@approval_queue_bp.route(
    '/workspaces/<int:workspace_id>/approvals/pending', methods=['GET']
)
@unified_auth_required
def list_pending_approvals(workspace_id: int):
    """List interaction requests that have not yet received an approval decision."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(per_page, 100)

    # Base query: all interaction_request events in this workspace
    request_query = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
        )
        .order_by(AgentTaskEvent.event_timestamp.desc(), AgentTaskEvent.id.desc())
    )

    # Subquery: interaction_ids that already have an approval response
    approved_interaction_ids_query = (
        db.session.query(
            AgentTaskEvent.payload['interaction_id'].as_string().label('interaction_id')
        )
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type == INTERACTION_APPROVAL_EVENT_TYPE,
        )
        .subquery()
    )

    # Exclude requests that already have an approval
    request_query = request_query.filter(
        db.func.coalesce(
            AgentTaskEvent.payload['interaction_id'].as_string(), ''
        ).notin_(db.session.query(approved_interaction_ids_query.c.interaction_id))
    )

    # Paginate
    total = request_query.count()
    offset = (page - 1) * per_page
    rows = request_query.limit(per_page).offset(offset).all()

    # Batch-load task titles and agent names to avoid N+1
    task_ids = list({row.task_id for row in rows})
    task_map = {}
    if task_ids:
        tasks = db.session.query(Task.id, Task.title).filter(Task.id.in_(task_ids)).all()
        task_map = {t.id: t.title for t in tasks}

    agent_ids = list({row.agent_id for row in rows})
    agent_map = {}
    if agent_ids:
        agents = db.session.query(Agent.id, Agent.name, Agent.display_name).filter(Agent.id.in_(agent_ids)).all()
        agent_map = {a.id: (a.display_name or a.name) for a in agents}

    items = []
    for row in rows:
        payload = row.payload or {}
        interaction_id = str(payload.get('interaction_id') or '').strip()
        governance = payload.get('governance') or {}
        items.append({
            'event_id': row.id,
            'interaction_id': interaction_id,
            'task_id': row.task_id,
            'task_title': task_map.get(row.task_id),
            'agent_id': row.agent_id,
            'agent_name': agent_map.get(row.agent_id, f'Agent #{row.agent_id}'),
            'interaction_type': payload.get('interaction_type'),
            'risk_tier': governance.get('risk_tier'),
            'sensitivity_level': governance.get('sensitivity_level'),
            'description': payload.get('description'),
            'created_at': row.event_timestamp.isoformat() if row.event_timestamp else None,
        })

    pages = (total + per_page - 1) // per_page if total > 0 else 1

    result = {
        'items': items,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': pages,
            'has_prev': page > 1,
            'has_next': (offset + per_page) < total,
            'prev_num': page - 1 if page > 1 else None,
            'next_num': page + 1 if (offset + per_page) < total else None,
        },
    }

    return ApiResponse.success(result, 'Pending approvals retrieved').to_response()


@approval_queue_bp.route(
    '/workspaces/<int:workspace_id>/approvals/stats', methods=['GET']
)
@unified_auth_required
def approval_queue_stats(workspace_id: int):
    """Return counts for the approval queue: pending and approved-today."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    from datetime import datetime, timedelta

    # --- pending count ---
    # interaction_request events whose interaction_id does NOT appear in any
    # interaction_approval event within this workspace.
    approved_interaction_ids_query = (
        db.session.query(
            AgentTaskEvent.payload['interaction_id'].as_string().label('interaction_id')
        )
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type == INTERACTION_APPROVAL_EVENT_TYPE,
        )
        .subquery()
    )

    pending_count = (
        db.session.query(AgentTaskEvent.id)
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
            db.func.coalesce(
                AgentTaskEvent.payload['interaction_id'].as_string(), ''
            ).notin_(db.session.query(approved_interaction_ids_query.c.interaction_id)),
        )
        .count()
    )

    # --- approved today count ---
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    approved_today_count = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type == INTERACTION_APPROVAL_EVENT_TYPE,
            AgentTaskEvent.event_timestamp >= today_start,
        )
        .count()
    )

    return ApiResponse.success(
        {
            'pending': pending_count,
            'approved_today': approved_today_count,
        },
        'Approval queue stats',
    ).to_response()
