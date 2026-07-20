"""
Shared dispatch-policy and assignment helpers.

Used by both the task operation submodules (task_handoffs) and dispatch.py.
Extracted from the former task_operations.py monolith to avoid circular
imports once the task routes were split across multiple modules.
"""

from datetime import datetime, timedelta

from ._shared import (
    db,
    Agent,
    AgentKind,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    Project,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    build_task_snapshot,
    active_assignment_filter,
    expire_stale_assignments_for_task,
    find_active_assignment,
)


def cancel_assignment_for_handoff(assignment, now):
    """Cancel an active assignment because its work is being handed off."""
    assignment.state = TaskAssignmentState.CANCELLED
    assignment.completed_at = now
    db.session.add(assignment)

    run = (
        AgentRun.query.filter_by(assignment_id=assignment.id)
        .order_by(AgentRun.started_at.desc())
        .first()
    )
    if run and run.status in [AgentRunStatus.RUNNING, AgentRunStatus.WAITING_HUMAN]:
        run.status = AgentRunStatus.CANCELLED
        run.ended_at = now
        db.session.add(run)

    return run


def create_assignment_with_run(task, agent, current_user, now, lease_seconds, run_metadata):
    """Create a CLAIMED assignment plus its RUNNING execution run for an Agent."""
    assignment = TaskAssignment(
        task_id=task.id,
        agent_id=agent.id,
        assigned_by_user_id=current_user.id,
        state=TaskAssignmentState.CLAIMED,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        claimed_at=now,
        last_heartbeat_at=now,
        progress_rate=0,
        created_by=current_user.email,
    )
    db.session.add(assignment)

    agent.last_seen_at = now
    if agent.status == AgentStatus.OFFLINE:
        agent.status = AgentStatus.ACTIVE

    run = AgentRun(
        task_id=task.id,
        agent_id=agent.id,
        assignment=assignment,
        status=AgentRunStatus.RUNNING,
        started_at=now,
        input_snapshot=build_task_snapshot(task),
        run_metadata=run_metadata,
        created_by=current_user.email,
    )
    db.session.add(run)

    return assignment, run



DISPATCH_MAX_ASSIGNMENTS = 20
DISPATCH_PREVIEW_CANDIDATE_LIMIT = 5
DISPATCH_POLICY_DEFAULTS = {
    "auto_dispatch_enabled": False,
    "project_id": None,
    "max_assignments": DISPATCH_MAX_ASSIGNMENTS,
    "lease_seconds": 1800,
    "match_capabilities": True,
    "require_capability_match": False,
    "candidate_agent_ids": [],
    "include_self": False,
}


def normalize_dispatch_policy(data, current_user=None):
    """Validate and normalize a coordinator dispatch policy payload."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("dispatch policy must be an object")

    policy = dict(DISPATCH_POLICY_DEFAULTS)

    if "auto_dispatch_enabled" in data:
        policy["auto_dispatch_enabled"] = bool(data.get("auto_dispatch_enabled"))

    if "project_id" in data:
        project_id = data.get("project_id")
        if project_id in ("", None):
            policy["project_id"] = None
        else:
            project_id = int(project_id)
            if project_id <= 0:
                raise ValueError("project_id must be a positive integer")
            if current_user is not None:
                project = Project.query.filter_by(id=project_id, owner_id=current_user.id).first()
                if not project:
                    raise ValueError("project_id does not belong to current user")
            policy["project_id"] = project_id

    if "max_assignments" in data:
        max_assignments = int(data.get("max_assignments") or DISPATCH_MAX_ASSIGNMENTS)
        policy["max_assignments"] = max(1, min(max_assignments, DISPATCH_MAX_ASSIGNMENTS))

    if "lease_seconds" in data:
        lease_seconds = int(data.get("lease_seconds") or 1800)
        policy["lease_seconds"] = max(60, min(lease_seconds, 24 * 60 * 60))

    if "match_capabilities" in data:
        policy["match_capabilities"] = data.get("match_capabilities") is not False

    if "require_capability_match" in data:
        policy["require_capability_match"] = bool(data.get("require_capability_match"))

    if "include_self" in data:
        policy["include_self"] = bool(data.get("include_self"))

    if "candidate_agent_ids" in data:
        candidate_agent_ids = data.get("candidate_agent_ids")
        if candidate_agent_ids in (None, ""):
            policy["candidate_agent_ids"] = []
        elif not isinstance(candidate_agent_ids, list):
            raise ValueError("candidate_agent_ids must be a list of agent ids")
        else:
            normalized_ids = []
            for raw_agent_id in candidate_agent_ids:
                agent_id = int(raw_agent_id)
                if agent_id <= 0:
                    raise ValueError("candidate_agent_ids must contain positive integers")
                if agent_id not in normalized_ids:
                    normalized_ids.append(agent_id)
            if current_user is not None and normalized_ids:
                owned_count = Agent.query.filter(
                    Agent.owner_id == current_user.id,
                    Agent.id.in_(normalized_ids),
                ).count()
                if owned_count != len(normalized_ids):
                    raise ValueError("candidate_agent_ids must belong to current user")
            policy["candidate_agent_ids"] = normalized_ids

    if policy["match_capabilities"] is False:
        policy["require_capability_match"] = False

    return policy


def get_coordinator_dispatch_policy(coordinator, current_user=None):
    config = coordinator.config or {}
    stored_policy = config.get("dispatch_policy") if isinstance(config, dict) else None
    return normalize_dispatch_policy(stored_policy or {}, current_user=current_user)


def resolve_dispatch_options(coordinator, data, current_user=None):
    policy = get_coordinator_dispatch_policy(coordinator, current_user=current_user)
    overrides = {}
    for key in DISPATCH_POLICY_DEFAULTS:
        if key in data:
            overrides[key] = data[key]

    resolved = normalize_dispatch_policy({**policy, **overrides}, current_user=current_user)
    return resolved, policy


def collect_claimable_tasks(current_user, project_id=None, limit=50):
    """Return owner tasks that currently have no active assignment, priority-ordered."""
    query = Task.query.join(Project).filter(
        Project.owner_id == current_user.id,
        Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]),
    )
    if project_id:
        query = query.filter(Task.project_id == project_id)

    candidates = query.order_by(Task.priority.desc(), Task.created_at.asc()).limit(limit).all()
    claimable = []
    for task in candidates:
        expire_stale_assignments_for_task(task.id)
        if find_active_assignment(task.id):
            continue
        claimable.append(task)
    return claimable


def find_available_worker_agents(current_user, coordinator, candidate_agent_ids=None, include_self=False):
    """Return ACTIVE worker Agents that have spare capacity (no live assignment).

    Worker pool excludes the dispatching coordinator (unless ``include_self``) and
    any other coordinator-kind Agents, since coordinators orchestrate rather than
    execute. An Agent already holding a live assignment is considered busy and is
    skipped so a single dispatch round spreads work rather than piling it on one Agent.
    """
    query = Agent.query.filter(
        Agent.owner_id == current_user.id,
        Agent.status == AgentStatus.ACTIVE,
    )
    if candidate_agent_ids:
        query = query.filter(Agent.id.in_(candidate_agent_ids))

    now = datetime.utcnow()
    available = []
    for agent in query.all():
        if agent.id == coordinator.id and not include_self:
            continue
        if agent.kind == AgentKind.COORDINATOR and agent.id != coordinator.id:
            continue
        busy = TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent.id,
            active_assignment_filter(now),
        ).first()
        if busy:
            continue
        available.append(agent)
    return available


def serialize_dispatch_candidate(worker, match, match_capabilities=True):
    strategy = "capability_match" if (match_capabilities and match["score"] > 0) else "priority_fifo"
    return {
        "agent": worker.to_dict(include_stats=False),
        "score": match["score"],
        "strategy": strategy,
        "matched_capabilities": match.get("matched_capabilities", []),
        "matched_tags": match.get("matched_tags", []),
        "matched_text": match.get("matched_text", []),
        "missing_required": match.get("missing_required", []),
        "experience_bonus": match.get("experience_bonus", 0),
    }

