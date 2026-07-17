"""
Cross-project agent authorization, task claiming, capability adaptation, and efficiency endpoints.
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
    AgentStatus,
    CrossProjectAgent,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Project,
    ProjectMember,
    AuditLog,
    AgentRun,
    AgentRunStatus,
    get_request_args,
    paginate_query,
    parse_enum,
    record_task_event,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    notify_sse,
)

@agents_bp.route("/<int:agent_id>/adapt-capabilities", methods=["GET"])
@unified_auth_required
def suggest_capability_adaptation(agent_id):
    """Suggest capability adaptations for an Agent based on its experiences.

    Returns suggested additions and removals based on success/failure patterns.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    suggestions = agent.adapt_capabilities_from_experiences()
    return ApiResponse.success(suggestions, "Capability adaptation suggestions").to_response()


@agents_bp.route("/<int:agent_id>/adapt-capabilities", methods=["POST"])
@unified_auth_required
def apply_capability_adaptation(agent_id):
    """Apply capability adaptations to an Agent.

    Accepts lists of capabilities to add and/or remove.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request()
    additions = data.get("additions", [])
    removals = data.get("removals", [])

    if not additions and not removals:
        return ApiResponse.error("No changes specified (additions or removals required)").to_response()

    old_caps = list(agent.capabilities or [])
    new_caps = agent.apply_capability_adaptation(additions=additions, removals=removals)
    db.session.commit()

    # Record adaptation event
    AuditLog.record(
        "agent_capability_adaptation",
        target_type="agent",
        target_id=agent.id,
        actor_type="system",
        detail={
            "old_capabilities": old_caps,
            "new_capabilities": new_caps,
            "additions": additions,
            "removals": removals,
        },
    )
    db.session.commit()

    notify_sse("agent_capabilities_adapted", {
        "agent_id": agent_id,
        "additions": additions,
        "removals": removals,
    })

    return ApiResponse.success({
        "old_capabilities": old_caps,
        "new_capabilities": new_caps,
        "additions_applied": additions,
        "removals_applied": removals,
    }, "Capabilities adapted").to_response()


# ---------------------------------------------------------------------------
# Cross-Project Task Discovery & Assignment endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/<int:agent_id>/cross-project-tasks", methods=["GET"])
@unified_auth_required
def find_cross_project_tasks(agent_id):
    """Find claimable tasks across all projects the Agent is authorized for.

    Returns tasks from the agent's owner's projects plus cross-project
    authorized projects, scored by capability match.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    limit = args.get("limit", 20, type=int)

    # Get cross-project authorizations
    cross_auths = CrossProjectAgent.get_active_for_agent(agent.id)
    if not cross_auths:
        return ApiResponse.success([], "No cross-project authorizations").to_response()

    results = []
    for auth in cross_auths:
        # Check concurrent task limit
        active_count = TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent.id,
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
        ).count()
        if active_count >= (auth.max_concurrent_tasks or 3):
            continue

        # Find available tasks in this project
        project = Project.query.get(auth.project_id)
        if not project:
            continue

        tasks = Task.query.filter(
            Task.project_id == auth.project_id,
            Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]),
        ).order_by(Task.priority.desc()).limit(20).all()

        for task in tasks:
            expire_stale_assignments_for_task(task.id)
            if find_active_assignment(task.id):
                continue

            effective_caps = CrossProjectAgent.get_effective_capabilities(agent.id, auth.project_id)
            match = _score_task_with_caps(task, effective_caps, agent)
            if match["score"] > 0:
                results.append({
                    "task": task.to_dict(),
                    "project": {"id": project.id, "name": project.name},
                    "match": match,
                    "role_in_project": auth.role_in_project,
                })

    # Sort by match score
    results.sort(key=lambda x: -x["match"]["score"])
    results = results[:limit]

    return ApiResponse.success(results, f"Found {len(results)} cross-project tasks").to_response()


@agents_bp.route("/<int:agent_id>/claim-cross-project-task/<int:task_id>", methods=["POST"])
@unified_auth_required
def claim_cross_project_task(agent_id, task_id):
    """Claim a task from a cross-project for an Agent.

    Verifies the agent has cross-project authorization for the task's project,
    then assigns the task with cross-project context.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    task = Task.query.get(task_id)
    if not task:
        return ApiResponse.not_found("Task not found").to_response()

    # Verify cross-project authorization
    if not CrossProjectAgent.is_authorized(agent_id, task.project_id):
        return ApiResponse.error("Agent not authorized for this task's project").to_response()

    # Check concurrent task limit
    auth = CrossProjectAgent.query.filter_by(
        agent_id=agent_id, project_id=task.project_id, is_active=True,
    ).first()
    if auth:
        active_count = TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent_id,
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
        ).count()
        if active_count >= (auth.max_concurrent_tasks or 3):
            return ApiResponse.error(
                f"Agent has reached max concurrent tasks ({auth.max_concurrent_tasks}) in this project"
            ).to_response()

    # Check for existing active assignment
    existing = find_active_assignment(task_id)
    if existing:
        return ApiResponse.error("Task already has an active assignment").to_response()

    # Create assignment with cross-project flag
    data = validate_json_request() or {}
    lease_seconds = int(data.get("lease_seconds") or 1800)
    lease_seconds = max(60, min(lease_seconds, 24 * 60 * 60))
    now = datetime.utcnow()

    assignment = TaskAssignment(
        task_id=task_id,
        agent_id=agent_id,
        assigned_by_user_id=user.id,
        state=TaskAssignmentState.CLAIMED,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        claimed_at=now,
        last_heartbeat_at=now,
        progress_rate=0,
        created_by=user.email,
    )
    db.session.add(assignment)
    if task.status == TaskStatus.TODO:
        task.status = TaskStatus.IN_PROGRESS
    db.session.commit()

    # Record in audit log
    AuditLog.record(
        "cross_project_task_claimed",
        target_type="task",
        target_id=task_id,
        actor_type="agent",
        actor_id=agent_id,
        detail={
            "agent_id": agent_id,
            "task_id": task_id,
            "project_id": task.project_id,
            "assignment_id": assignment.id,
        },
    )
    db.session.commit()

    notify_sse("task_assigned", {
        "task_id": task_id,
        "agent_id": agent_id,
        "assignment_id": assignment.id,
        "project_id": task.project_id,
        "cross_project": True,
    })

    return ApiResponse.success({
        "assignment": assignment.to_dict(),
        "task": task.to_dict(),
        "cross_project": True,
    }, "Cross-project task claimed").to_response()
@agents_bp.route("/cross-project-efficiency", methods=["GET"])
@unified_auth_required
def cross_project_efficiency():
    """Measure realized value of cross-project Agent authorizations.

    For each cross-project authorization into a project owned by the
    current user, counts completed (DONE) TaskAssignments in the host
    project within the window. Identifies utilized vs idle authorizations
    so owners can revoke unused grants and right-size cross-project access.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        days, limit = 30, 20

    from models.agent import CrossProjectAgent
    from models.task import Task
    from models.project import Project

    since = datetime.utcnow() - timedelta(days=days)

    authorizations = (
        CrossProjectAgent.query
        .join(Project, CrossProjectAgent.project_id == Project.id)
        .filter(Project.owner_id == user.id)
        .all()
    )

    if not authorizations:
        return ApiResponse.success({
            "authorizations": [],
            "total_authorizations": 0,
            "active_count": 0,
            "utilized_count": 0,
            "idle_count": 0,
            "utilization_rate": 0.0,
            "days": days,
        }).to_response()

    # completed assignments per (agent_id, project_id) within window
    rows = (
        TaskAssignment.query
        .join(Task, TaskAssignment.task_id == Task.id)
        .filter(
            TaskAssignment.state == TaskAssignmentState.DONE,
            TaskAssignment.completed_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id,
            Task.project_id,
        )
        .all()
    )
    done_map = {}
    for aid, pid in rows:
        done_map[(aid, pid)] = done_map.get((aid, pid), 0) + 1

    results = []
    active_count = 0
    utilized_count = 0
    for a in authorizations:
        if a.is_active:
            active_count += 1
        done = done_map.get((a.agent_id, a.project_id), 0)
        if done > 0:
            utilized_count += 1
        agent_name = a.agent.name if a.agent else f"Agent#{a.agent_id}"
        proj_name = a.project.name if a.project else f"Project#{a.project_id}"
        results.append({
            "authorization_id": a.id,
            "agent_id": a.agent_id,
            "agent_name": agent_name,
            "host_project_id": a.project_id,
            "host_project_name": proj_name,
            "tasks_completed_in_host": done,
            "is_active": bool(a.is_active),
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "utilized": done > 0,
        })

    results.sort(key=lambda r: r["tasks_completed_in_host"], reverse=True)
    total = len(results)
    idle = total - utilized_count
    rate = (utilized_count / total) if total else 0.0
    return ApiResponse.success({
        "authorizations": results[:limit],
        "total_authorizations": total,
        "active_count": active_count,
        "utilized_count": utilized_count,
        "idle_count": idle,
        "utilization_rate": round(rate, 3),
        "days": days,
    }).to_response()
