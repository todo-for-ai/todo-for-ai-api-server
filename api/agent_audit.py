"""
Agent Audit Events API

List and query audit events for a workspace.
"""

from flask import Blueprint, request
from sqlalchemy import func

from models import db, AgentAuditEvent
from core.auth import unified_auth_required, get_current_user
from api.agent_common import get_workspace_or_404, ensure_workspace_access
from api.base import ApiResponse

audit_bp = Blueprint('agent_audit', __name__)


@audit_bp.route('/workspaces/<int:workspace_id>/audit-events', methods=['GET'])
@unified_auth_required
def list_audit_events(workspace_id):
    """List audit events for a workspace with filters and pagination."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    query = AgentAuditEvent.query.filter_by(workspace_id=workspace_id)

    # Optional filters
    event_type = request.args.get('event_type')
    if event_type:
        query = query.filter(AgentAuditEvent.event_type == event_type)

    actor_type = request.args.get('actor_type')
    if actor_type:
        query = query.filter(AgentAuditEvent.actor_type == actor_type)

    target_type = request.args.get('target_type')
    if target_type:
        query = query.filter(AgentAuditEvent.target_type == target_type)

    task_id = request.args.get('task_id', type=int)
    if task_id:
        query = query.filter(AgentAuditEvent.task_id == task_id)

    level = request.args.get('level')
    if level:
        query = query.filter(AgentAuditEvent.level == level)

    start_date = request.args.get('start_date')
    if start_date:
        query = query.filter(AgentAuditEvent.occurred_at >= start_date)

    end_date = request.args.get('end_date')
    if end_date:
        query = query.filter(AgentAuditEvent.occurred_at <= end_date)

    # Default ordering: newest first
    query = query.order_by(AgentAuditEvent.occurred_at.desc(), AgentAuditEvent.id.desc())

    # Pagination
    page = max(request.args.get('page', 1, type=int), 1)
    per_page = min(max(request.args.get('per_page', 20, type=int), 1), 100)

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [item.to_dict() for item in items],
            'total': total,
            'page': page,
            'per_page': per_page,
        },
        'Audit events retrieved successfully',
    ).to_response()


@audit_bp.route('/workspaces/<int:workspace_id>/audit-events/stats', methods=['GET'])
@unified_auth_required
def audit_events_stats(workspace_id):
    """Return aggregated stats for audit events in a workspace."""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    base_filter = AgentAuditEvent.query.filter_by(workspace_id=workspace_id)

    total = base_filter.count()

    by_level_rows = (
        db.session.query(AgentAuditEvent.level, func.count(AgentAuditEvent.id))
        .filter(AgentAuditEvent.workspace_id == workspace_id)
        .group_by(AgentAuditEvent.level)
        .all()
    )
    by_level = {row[0]: row[1] for row in by_level_rows}

    by_actor_type_rows = (
        db.session.query(AgentAuditEvent.actor_type, func.count(AgentAuditEvent.id))
        .filter(AgentAuditEvent.workspace_id == workspace_id)
        .group_by(AgentAuditEvent.actor_type)
        .all()
    )
    by_actor_type = {row[0]: row[1] for row in by_actor_type_rows}

    return ApiResponse.success(
        {
            'total': total,
            'by_level': by_level,
            'by_actor_type': by_actor_type,
        },
        'Audit event stats retrieved successfully',
    ).to_response()
