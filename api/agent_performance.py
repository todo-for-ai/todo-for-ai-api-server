"""Agent performance metrics API."""

from datetime import datetime, timedelta

from flask import Blueprint, request

from models import db, Agent, AgentStatus, Task, TaskStatus, AgentAuditEvent
from api.base import ApiResponse
from core.auth import unified_auth_required

perf_bp = Blueprint('agent_performance', __name__)


@perf_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/performance', methods=['GET'])
@unified_auth_required
def get_agent_performance(workspace_id, agent_id):
    """Return aggregated performance metrics for an agent.

    Query params:
        days (int): lookback window in days, default 30.
    """
    days = request.args.get('days', 30, type=int)
    since = datetime.utcnow() - timedelta(days=days)

    agent = db.session.get(Agent, agent_id)
    if not agent or agent.workspace_id != workspace_id:
        return ApiResponse.error('Agent not found', 404).to_response()

    # ------------------------------------------------------------------
    # 1. Task-level metrics
    #    Agents are linked to tasks through the Task.assignees JSON array
    #    which may contain entries like {"type": "agent", "id": <agent_id>}.
    #    Also check AgentAuditEvent for task completion/error signals.
    # ------------------------------------------------------------------
    tasks_completed = 0
    tasks_total = 0
    avg_duration_ms = 0
    error_count = 0

    # Count audit events authored by this agent in the window
    audit_q = (
        db.session.query(AgentAuditEvent)
        .filter(
            AgentAuditEvent.actor_agent_id == agent_id,
            AgentAuditEvent.occurred_at >= since,
        )
    )
    audit_events = audit_q.all()
    tasks_total = len(audit_events)

    durations = []
    for evt in audit_events:
        if evt.event_type == 'task_complete':
            tasks_completed += 1
        if evt.event_type in ('task_error', 'task_failure'):
            error_count += 1
        if evt.duration_ms is not None and evt.duration_ms > 0:
            durations.append(evt.duration_ms)

    if durations:
        avg_duration_ms = int(sum(durations) / len(durations))

    success_rate = round(tasks_completed / tasks_total * 100, 1) if tasks_total > 0 else 0.0
    error_rate = round(error_count / tasks_total * 100, 1) if tasks_total > 0 else 0.0

    # ------------------------------------------------------------------
    # 2. Daily activity for last 7 days
    # ------------------------------------------------------------------
    daily_activity = []
    for i in range(6, -1, -1):
        day = datetime.utcnow().date() - timedelta(days=i)
        day_start = datetime.combine(day, datetime.min.time())
        day_end = datetime.combine(day, datetime.max.time())

        day_count = (
            db.session.query(AgentAuditEvent)
            .filter(
                AgentAuditEvent.actor_agent_id == agent_id,
                AgentAuditEvent.occurred_at >= day_start,
                AgentAuditEvent.occurred_at <= day_end,
            )
            .count()
        )
        daily_activity.append({'date': day.isoformat(), 'events': day_count})

    metrics = {
        'agent_id': agent_id,
        'workspace_id': workspace_id,
        'period_days': days,
        'tasks_completed': tasks_completed,
        'tasks_total': tasks_total,
        'success_rate': success_rate,
        'avg_duration_ms': avg_duration_ms,
        'error_count': error_count,
        'error_rate': error_rate,
        'daily_activity': daily_activity,
    }

    return ApiResponse.success(data=metrics).to_response()
