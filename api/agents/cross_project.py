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
    ProjectRole,
    normalize_match_terms,
    validate_json_request,
    ACTIVE_ASSIGNMENT_STATES,
    expire_stale_assignments_for_task,
    find_active_assignment,
    _score_task_with_caps,
)


# Cross-Project Agent Collaboration endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/cross-project/authorize", methods=["POST"])
@unified_auth_required
def authorize_cross_project_agent():
    """Authorize an Agent to work in a different project.

    The authorizing user must be an ADMIN or OWNER of the target project.
    The agent's owner must be a member of the target project or the authorizer
    must be the agent's owner.
    """
    user = get_current_user()
    data = validate_json_request()

    agent_id = data.get("agent_id")
    project_id = data.get("project_id")
    if not agent_id or not project_id:
        return ApiResponse.error("agent_id and project_id are required").to_response()

    # Verify agent exists and user owns it
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found or not owned by you").to_response()

    # Verify project exists and user has admin access
    project = Project.query.get(project_id)
    if not project:
        return ApiResponse.not_found("Project not found").to_response()

    user_role = ProjectMember.get_role(project_id, user.id)
    if user_role not in (ProjectRole.OWNER, ProjectRole.ADMIN):
        return ApiResponse.error("You must be ADMIN or OWNER of the target project").to_response()

    # Check if already authorized
    existing = CrossProjectAgent.query.filter_by(
        agent_id=agent_id, project_id=project_id
    ).first()
    if existing:
        existing.is_active = True
        existing.role_in_project = data.get("role_in_project", existing.role_in_project)
        if "capabilities_override" in data:
            existing.capabilities_override = data["capabilities_override"]
        if "max_concurrent_tasks" in data:
            existing.max_concurrent_tasks = data["max_concurrent_tasks"]
        if "expires_at" in data:
            existing.expires_at = data["expires_at"]
        db.session.commit()
        return ApiResponse.success(existing.to_dict(), "Cross-project authorization updated").to_response()

    auth = CrossProjectAgent.create(
        agent_id=agent_id,
        project_id=project_id,
        authorized_by=user.id,
        role_in_project=data.get("role_in_project", "contributor"),
        capabilities_override=data.get("capabilities_override"),
        max_concurrent_tasks=data.get("max_concurrent_tasks", 3),
        expires_at=data.get("expires_at"),
    )
    db.session.commit()

    notify_sse("cross_project_authorized", {
        "agent_id": agent_id,
        "project_id": project_id,
        "role_in_project": auth.role_in_project,
    })
    return ApiResponse.success(auth.to_dict(), "Agent authorized for cross-project access").to_response()


@agents_bp.route("/cross-project/revoke", methods=["POST"])
@unified_auth_required
def revoke_cross_project_agent():
    """Revoke an Agent's cross-project authorization."""
    user = get_current_user()
    data = validate_json_request()

    agent_id = data.get("agent_id")
    project_id = data.get("project_id")
    if not agent_id or not project_id:
        return ApiResponse.error("agent_id and project_id are required").to_response()

    auth = CrossProjectAgent.query.filter_by(
        agent_id=agent_id, project_id=project_id
    ).first()
    if not auth:
        return ApiResponse.not_found("Cross-project authorization not found").to_response()

    # Verify user is the agent owner or project admin
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        user_role = ProjectMember.get_role(project_id, user.id)
        if user_role not in (ProjectRole.OWNER, ProjectRole.ADMIN):
            return ApiResponse.error("Not authorized to revoke this access").to_response()

    auth.is_active = False
    db.session.commit()
    return ApiResponse.success(None, "Cross-project authorization revoked").to_response()


@agents_bp.route("/<int:agent_id>/cross-project", methods=["GET"])
@unified_auth_required
def list_agent_cross_projects(agent_id):
    """List all projects an Agent is authorized to work in."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    authorizations = CrossProjectAgent.get_active_for_agent(agent_id)
    return ApiResponse.success(
        [a.to_dict() for a in authorizations],
        f"Agent authorized in {len(authorizations)} projects",
    ).to_response()


@agents_bp.route("/projects/<int:project_id>/external-agents", methods=["GET"])
@unified_auth_required
def list_project_external_agents(project_id):
    """List all external Agents authorized to work in this project."""
    user = get_current_user()
    project = Project.query.get(project_id)
    if not project:
        return ApiResponse.not_found("Project not found").to_response()

    # Verify user has access to the project
    user_role = ProjectMember.get_role(project_id, user.id)
    if not user_role and project.owner_id != user.id:
        return ApiResponse.error("Access denied").to_response()

    authorizations = CrossProjectAgent.get_active_for_project(project_id)
    return paginate_query(
        CrossProjectAgent.query.filter(
            CrossProjectAgent.project_id == project_id,
            CrossProjectAgent.is_active == True,
        ).order_by(CrossProjectAgent.created_at.desc()),
        "agents",
    )


@agents_bp.route("/cross-project/discover-agents", methods=["GET"])
@unified_auth_required
def discover_cross_project_agents():
    """Discover agents across all projects the user has access to.

    Returns agents that could be assigned to tasks, including external
    agents authorized for the user's projects.
    """
    user = get_current_user()
    args = get_request_args()

    # Get all projects the user has access to
    user_projects = ProjectMember.query.filter_by(user_id=user.id).with_entities(
        ProjectMember.project_id
    ).all()
    owned_projects = Project.query.filter_by(owner_id=user.id).with_entities(
        Project.id
    ).all()
    project_ids = list(set(
        [p.project_id for p in user_projects] + [p.id for p in owned_projects]
    ))

    if not project_ids:
        return ApiResponse.success([], "No projects found").to_response()

    # Find cross-project agents for these projects
    query = CrossProjectAgent.query.filter(
        CrossProjectAgent.project_id.in_(project_ids),
        CrossProjectAgent.is_active == True,
    )

    # Optional capability filter
    capability = args.get("capability")
    if capability:
        query = query.join(Agent, CrossProjectAgent.agent_id == Agent.id).filter(
            Agent.capabilities.contains([capability])
        )

    authorizations = query.order_by(CrossProjectAgent.created_at.desc()).all()

    # Deduplicate by agent_id
    seen = set()
    result = []
    for auth in authorizations:
        if auth.agent_id not in seen:
            seen.add(auth.agent_id)
            result.append(auth.to_dict())

    return ApiResponse.success(result, f"Found {len(result)} cross-project agents").to_response()


@agents_bp.route("/cross-project/capable-agents", methods=["GET"])
@unified_auth_required
def find_capable_agents_cross_project():
    """Find agents across all accessible projects that have specific capabilities.

    Query params: capabilities (comma-separated), project_id (optional filter)
    """
    user = get_current_user()
    args = get_request_args()

    capabilities_str = args.get("capabilities", "")
    if not capabilities_str:
        return ApiResponse.error("capabilities parameter is required").to_response()

    capabilities = [c.strip() for c in capabilities_str.split(",") if c.strip()]
    if not capabilities:
        return ApiResponse.error("No valid capabilities provided").to_response()

    # Get accessible project IDs
    user_projects = ProjectMember.query.filter_by(user_id=user.id).with_entities(
        ProjectMember.project_id
    ).all()
    owned_projects = Project.query.filter_by(owner_id=user.id).with_entities(
        Project.id
    ).all()
    project_ids = list(set(
        [p.project_id for p in user_projects] + [p.id for p in owned_projects]
    ))

    # Filter by specific project if requested
    filter_project_id = args.get("project_id", type=int)
    if filter_project_id:
        if filter_project_id not in project_ids:
            return ApiResponse.error("No access to specified project").to_response()
        project_ids = [filter_project_id]

    # Find agents: own agents + cross-project authorized agents
    # 1. Own agents with matching capabilities
    own_agents = Agent.query.filter(
        Agent.owner_id == user.id,
        Agent.status == AgentStatus.ACTIVE,
    ).all()

    # 2. Cross-project authorized agents
    cross_auths = CrossProjectAgent.query.filter(
        CrossProjectAgent.project_id.in_(project_ids),
        CrossProjectAgent.is_active == True,
    ).all()
    cross_agent_ids = [a.agent_id for a in cross_auths]
    cross_agents = Agent.query.filter(
        Agent.id.in_(cross_agent_ids),
        Agent.status == AgentStatus.ACTIVE,
    ).all() if cross_agent_ids else []

    # Combine and deduplicate
    all_agents = {}
    for a in own_agents:
        all_agents[a.id] = {"agent": a, "source": "own", "projects": project_ids}
    for auth in cross_auths:
        agent = next((a for a in cross_agents if a.id == auth.agent_id), None)
        if agent and agent.id not in all_agents:
            all_agents[agent.id] = {
                "agent": agent,
                "source": "cross_project",
                "projects": [auth.project_id],
                "role_in_project": auth.role_in_project,
            }

    # Score each agent for the requested capabilities
    results = []
    for agent_id, info in all_agents.items():
        agent = info["agent"]
        agent_caps = normalize_match_terms(agent.capabilities or [])
        req_caps = normalize_match_terms(capabilities)
        matched = agent_caps.intersection(req_caps)

        if matched:
            results.append({
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_kind": agent.kind.value if agent.kind else None,
                "matched_capabilities": sorted(matched),
                "match_score": len(matched) * 10,
                "source": info["source"],
                "available_projects": info["projects"],
                "role_in_project": info.get("role_in_project"),
                "collaboration_role": agent.collaboration_role or "standalone",
            })

    # Sort by match score
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return ApiResponse.success(results, f"Found {len(results)} capable agents").to_response()


# ---------------------------------------------------------------------------
# Capability Adaptation endpoints
# ---------------------------------------------------------------------------

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
