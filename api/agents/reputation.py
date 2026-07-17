"""
Agent reputation CRUD, recalculation, and history endpoints.
"""

from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentReputation,
    TaskAssignment,
    TaskAssignmentState,
    get_request_args,
    paginate_query,
)

@agents_bp.route("/<int:agent_id>/reputation", methods=["GET"])
@unified_auth_required
def get_agent_reputation(agent_id):
    """Get the reputation record for an Agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    rep = AgentReputation.get_or_create(agent_id)
    db.session.commit()
    return ApiResponse.success(rep.to_dict()).to_response()


@agents_bp.route("/reputations", methods=["GET"])
@unified_auth_required
def list_reputations():
    """List reputation records for all user's agents, ranked by score."""
    user = get_current_user()
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).all()]

    if not agent_ids:
        return ApiResponse.success([]).to_response()

    reputations = AgentReputation.query.filter(
        AgentReputation.agent_id.in_(agent_ids)
    ).order_by(AgentReputation.score.desc()).all()

    # Ensure all agents have reputation records
    for aid in agent_ids:
        AgentReputation.get_or_create(aid)
    db.session.commit()

    return ApiResponse.success([r.to_dict() for r in reputations]).to_response()


@agents_bp.route("/<int:agent_id>/reputation/recalculate", methods=["POST"])
@unified_auth_required
def recalculate_reputation(agent_id):
    """Recalculate an Agent's reputation from scratch based on task history."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    # Count task outcomes
    completed = TaskAssignment.query.filter_by(
        agent_id=agent_id, state=TaskAssignmentState.DONE
    ).count()
    failed = TaskAssignment.query.filter_by(
        agent_id=agent_id, state=TaskAssignmentState.FAILED
    ).count()
    total = completed + failed

    # Calculate base score
    if total == 0:
        score = 50.0
    else:
        success_rate = completed / total
        score = 20 + success_rate * 60  # 20-80 range based on success rate

    # Adjust for on-time performance
    on_time_assignments = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent_id,
        TaskAssignment.state == TaskAssignmentState.DONE,
        TaskAssignment.output_summary.isnot(None),
    ).count()
    on_time_rate = on_time_assignments / max(1, completed)

    score += on_time_rate * 20  # Up to +20 for on-time
    score = max(0, min(100, score))

    rep = AgentReputation.get_or_create(agent_id)
    rep.score = score
    rep.total_tasks = total
    rep.completed_tasks = completed
    rep.failed_tasks = failed
    rep.on_time_rate = on_time_rate
    rep.last_updated_at = datetime.utcnow()
    db.session.commit()

    return ApiResponse.success(rep.to_dict(), "Reputation recalculated").to_response()


@agents_bp.route("/<int:agent_id>/reputation/history", methods=["GET"])
@unified_auth_required
def get_agent_reputation_history(agent_id):
    """Return the timeline of notable reputation changes for an Agent.

    Reconstructed from the ``reputation.update`` audit events emitted by
    ``AgentReputation.record_outcome`` (failures and quality-feedback deltas).
    Each point carries the resulting score so the frontend can chart the trend.
    Successful-only outcomes are intentionally not audited (too high-frequency),
    so this is a feed of *notable* events rather than every task.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except (TypeError, ValueError):
        limit = 100

    q = AuditLog.query.filter(
        AuditLog.action == "reputation.update",
        AuditLog.resource_type == "agent",
        AuditLog.resource_id == agent_id,
    )
    since = request.args.get("since")
    if since:
        q = q.filter(AuditLog.created_at >= since)
    until = request.args.get("until")
    if until:
        q = q.filter(AuditLog.created_at <= until)

    # Newest-first for the limit window, then present oldest-first for charting.
    rows = q.order_by(AuditLog.created_at.desc()).limit(limit).all()
    rows.reverse()

    points = []
    for r in rows:
        d = r.detail or {}
        points.append({
            "at": r.created_at.isoformat() if r.created_at else None,
            "audit_id": r.id,
            "new_score": d.get("new_score"),
            "score_delta": d.get("score_delta"),
            "quality_delta": d.get("quality_delta"),
            "success": d.get("success"),
            "total_tasks": d.get("total_tasks"),
            # Originating task/step context (only present for outcomes recorded
            # after the context fields were added; older audit rows lack them).
            "task_id": d.get("task_id"),
            "step_key": d.get("step_key"),
            "workflow_run_id": d.get("workflow_run_id"),
            "parent_workflow_run_id": d.get("parent_workflow_run_id"),
            "sub_workflow_run_id": d.get("sub_workflow_run_id"),
            "duration_sec": d.get("duration_sec"),
        })

    rep = AgentReputation.get_or_create(agent_id)
    db.session.commit()
    return ApiResponse.success({
        "agent_id": agent_id,
        "current_score": rep.score,
        "points": points,
    }).to_response()


# ---------------------------------------------------------------------------
# Agent Experience (Collective Intelligence) endpoints
# ---------------------------------------------------------------------------
