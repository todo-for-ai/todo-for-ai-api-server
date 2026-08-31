"""
Shared constants, helpers, and re-exports for the ``api.agents`` package.

Every submodule imports from here instead of repeating the same
model / utility imports and constant definitions.
"""

import re
from datetime import datetime, timedelta

from flask import make_response, request
from sqlalchemy import and_, or_, false as sa_false

from core.auth import get_current_user, unified_auth_required
from models import (
    Agent,
    AgentKind,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    AuditLog,
    Notification,
    Project,
    ProjectMember,
    ProjectRole,
    RunLog,
    SharedContext,
    StepStatus,
    TaskTemplate,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskEvent,
    TaskStatus,
    TaskPriority,
    TaskHistory,
    ActionType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    WorkflowTrigger,
    db,
)
from models.agent import (
    AgentChannel,
    AgentChannelMember,
    AgentChannelMessage,
    CollaborationTemplate,
    KnowledgeEntry,
    WorkflowVersion,
    CollaborationProtocol,
    ProtocolMessage,
    ProtocolType,
    ProtocolStatus,
    AgentReputation,
    AgentExperience,
    CrossProjectAgent,
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxLevel,
    SandboxViolationType,
    SandboxExecutionStatus,
    AgentConflict,
    ConflictType,
    ConflictSeverity,
    ConflictStatus,
    ConflictResolutionStrategy,
    OrchestrationRun,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    mark_stale_agents_offline,
)
from ..base import ApiResponse, get_request_args, paginate_query, validate_json_request
from ..sse import notify_sse
from . import agents_bp  # noqa: E402  (defined in __init__.py)

# ── SSE helpers ──────────────────────────────────────────────────────

# Per-request list of pending SSE notifications that get flushed after commit.
# Each entry: (user_id, event_type, payload_dict)
_pending_sse: list = []


def _queue_sse(user_id, event_type, payload):
    _pending_sse.append((user_id, event_type, payload))


def _client_ip():
    """Best-effort extraction of the client IP from Flask request."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def flush_sse_notifications():
    """Send all queued SSE notifications and clear the queue."""
    notifications = list(_pending_sse)
    _pending_sse.clear()
    for user_id, event_type, payload in notifications:
        notify_sse(user_id, event_type, payload)


# ── Constants ────────────────────────────────────────────────────────

CAPABILITY_TOKEN_PATTERN = re.compile(r"[^a-z0-9一-鿿]+")

LOCKED_TERMINAL_ASSIGNMENT_STATES = {
    TaskAssignmentState.FAILED,
    TaskAssignmentState.CANCELLED,
    TaskAssignmentState.EXPIRED,
}

# Collaboration event types that humans and Agents may post directly to a task
# timeline. Lifecycle events (claim/assignment_update/etc.) are emitted by the
# server only and are intentionally excluded so they cannot be spoofed.
POSTABLE_EVENT_TYPES = {
    "message",
    "note",
    "question",
    "answer",
    "handoff",
    "blocker",
    "decision",
    "info",
}

POSTABLE_EVENT_CONTENT_MAX = 8000

AGENT_ASSIGNMENT_TARGET_STATES = {
    TaskAssignmentState.CLAIMED,
    TaskAssignmentState.RUNNING,
    TaskAssignmentState.WAITING_HUMAN,
    TaskAssignmentState.REVIEW,
    TaskAssignmentState.DONE,
    TaskAssignmentState.FAILED,
}

HUMAN_ACTIVE_TARGET_STATES = {
    TaskAssignmentState.RUNNING,
    TaskAssignmentState.WAITING_HUMAN,
    TaskAssignmentState.REVIEW,
    TaskAssignmentState.CANCELLED,
}

HUMAN_REVIEW_TARGET_STATES = {
    TaskAssignmentState.RUNNING,
    TaskAssignmentState.DONE,
    TaskAssignmentState.CANCELLED,
}

HUMAN_WAITING_TARGET_STATES = {
    TaskAssignmentState.RUNNING,
    TaskAssignmentState.CANCELLED,
}

HUMAN_DONE_TARGET_STATES = {
    TaskAssignmentState.RUNNING,
    TaskAssignmentState.DONE,
    TaskAssignmentState.CANCELLED,
}

ACTIVE_ASSIGNMENT_STATES = {
    TaskAssignmentState.CLAIMED,
    TaskAssignmentState.RUNNING,
    TaskAssignmentState.WAITING_HUMAN,
    TaskAssignmentState.REVIEW,
}


# ── Error class ──────────────────────────────────────────────────────

class AssignmentUpdateError(ValueError):
    """Rejected assignment update that should not mutate assignment state."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


# ── Helper functions ─────────────────────────────────────────────────

def parse_enum(enum_cls, value, field_name):
    try:
        return enum_cls(value)
    except ValueError:
        valid_values = ", ".join(item.value for item in enum_cls)
        raise ValueError(f"Invalid {field_name}. Must be one of: {valid_values}")


def get_owned_agent_or_response(agent_id, current_user):
    agent = Agent.query.filter_by(id=agent_id, owner_id=current_user.id).first()
    if not agent:
        return None, ApiResponse.error("Agent not found", 404).to_response()
    return agent, None


def get_owned_task_or_response(task_id, current_user):
    task = Task.query.join(Project).filter(Task.id == task_id, Project.owner_id == current_user.id).first()
    if not task:
        return None, ApiResponse.error("Task not found", 404).to_response()
    return task, None


def serialize_claim_response(agent, assignment, run):
    return {
        "agent": agent.to_dict(include_stats=True),
        "assignment": assignment.to_dict(include_task=True, include_agent=True),
        "run": run.to_dict(),
    }


def serialize_review_queue_item(assignment):
    action = "review"
    if assignment.state == TaskAssignmentState.WAITING_HUMAN:
        action = "human_feedback"
    elif assignment.state in [TaskAssignmentState.REVIEW, TaskAssignmentState.DONE]:
        action = "final_review"

    return {
        "assignment": assignment.to_dict(include_task=True, include_agent=True),
        "task": assignment.task.to_dict(include_project=True) if assignment.task else None,
        "agent": assignment.agent.to_dict(include_stats=False) if assignment.agent else None,
        "action": action,
        "available_actions": ["resume", "approve", "cancel"],
    }


def paginate_serialized(query, page, per_page, serializer, max_per_page=100):
    per_page = min(per_page, max_per_page)
    offset = (page - 1) * per_page
    total = query.count()
    items = query.limit(per_page).offset(offset).all()
    pages = (total + per_page - 1) // per_page if total > 0 else 1

    return {
        "items": [serializer(item) for item in items],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "has_prev": page > 1,
            "has_next": (offset + per_page) < total,
            "prev_num": page - 1 if page > 1 else None,
            "next_num": page + 1 if (offset + per_page) < total else None,
        },
    }


def record_task_event(task_id, event_type, current_user=None, agent=None, payload=None, actor_type=None):
    event = TaskEvent.record(
        task_id=task_id,
        event_type=event_type,
        actor_type=actor_type or ("agent" if agent else "human"),
        actor_user_id=current_user.id if current_user and not agent else None,
        actor_agent_id=agent.id if agent else None,
        payload=payload or {},
    )
    db.session.add(event)

    # Queue an SSE notification for the owner so the live stream can push it.
    owner_id = current_user.id if current_user else None
    if owner_id:
        sse_payload = {
            "event_id": None,  # filled after flush
            "task_id": task_id,
            "event_type": event_type,
            "actor_type": event.actor_type,
            "actor_agent_id": event.actor_agent_id,
            **(payload or {}),
        }
        _queue_sse(owner_id, event_type, sse_payload)

        # Also persist a Notification so the user can catch up on missed events.
        Notification.create_notification(
            user_id=owner_id,
            event_type=event_type,
            task_id=task_id,
            agent_id=event.actor_agent_id,
            payload=sse_payload,
        )

    return event


def record_assignment_update_rejected(current_user, assignment, data, actor_agent, reason):
    record_task_event(
        assignment.task_id,
        "assignment_update_rejected",
        current_user=current_user if not actor_agent else None,
        agent=actor_agent,
        payload={
            "assignment_id": assignment.id,
            "agent_id": assignment.agent_id,
            "current_state": assignment.state.value if assignment.state else None,
            "requested_state": data.get("state"),
            "requested_task_status": data.get("task_status"),
            "reason": reason,
        },
    )


def parse_requested_update_enums(data):
    requested_state = None
    requested_task_status = None

    if "state" in data:
        try:
            requested_state = parse_enum(TaskAssignmentState, data["state"], "state")
        except ValueError as e:
            raise AssignmentUpdateError(str(e), 400)

    if "task_status" in data:
        try:
            requested_task_status = parse_enum(TaskStatus, data["task_status"], "task_status")
        except ValueError as e:
            raise AssignmentUpdateError(str(e), 400)

    return requested_state, requested_task_status


def validate_agent_assignment_update(assignment, data):
    requested_state, requested_task_status = parse_requested_update_enums(data)
    current_state = assignment.state
    target_state = requested_state or current_state

    if current_state in LOCKED_TERMINAL_ASSIGNMENT_STATES:
        raise AssignmentUpdateError("Terminal assignments cannot be updated", 409)

    if current_state in HUMAN_BLOCKING_ASSIGNMENT_STATES or current_state == TaskAssignmentState.DONE:
        raise AssignmentUpdateError("Assignment is waiting for human coordination", 409)

    if requested_state and target_state not in AGENT_ASSIGNMENT_TARGET_STATES:
        raise AssignmentUpdateError("Agent cannot move assignment to the requested state", 400)

    if requested_state == TaskAssignmentState.CLAIMED and current_state != TaskAssignmentState.CLAIMED:
        raise AssignmentUpdateError("Agent can only keep an already claimed assignment in claimed state", 400)

    if requested_task_status in {TaskStatus.TODO, TaskStatus.DONE, TaskStatus.CANCELLED}:
        raise AssignmentUpdateError("Agent updates cannot directly set final task status", 400)

    if requested_task_status == TaskStatus.IN_PROGRESS and target_state not in {
        TaskAssignmentState.CLAIMED,
        TaskAssignmentState.RUNNING,
    }:
        raise AssignmentUpdateError("Task status in_progress requires a running assignment", 400)

    if requested_task_status == TaskStatus.REVIEW and target_state not in {
        TaskAssignmentState.WAITING_HUMAN,
        TaskAssignmentState.REVIEW,
        TaskAssignmentState.DONE,
    }:
        raise AssignmentUpdateError("Task status review requires a review or human-waiting assignment", 400)


def validate_human_assignment_update(assignment, data):
    requested_state, requested_task_status = parse_requested_update_enums(data)
    current_state = assignment.state
    target_state = requested_state or current_state
    has_feedback = bool(data.get("feedback_content"))

    if current_state in LOCKED_TERMINAL_ASSIGNMENT_STATES:
        raise AssignmentUpdateError("Terminal assignments cannot be updated", 409)

    if current_state == TaskAssignmentState.WAITING_HUMAN:
        if requested_state and target_state not in HUMAN_WAITING_TARGET_STATES:
            raise AssignmentUpdateError("Human feedback items can only be resumed or cancelled", 400)
        if target_state == TaskAssignmentState.RUNNING and not has_feedback:
            raise AssignmentUpdateError("Resuming a human-feedback assignment requires feedback_content", 400)
    elif current_state == TaskAssignmentState.REVIEW:
        if requested_state and target_state not in HUMAN_REVIEW_TARGET_STATES:
            raise AssignmentUpdateError("Review assignments can only be approved, resumed, or cancelled", 400)
        if target_state == TaskAssignmentState.RUNNING and not has_feedback:
            raise AssignmentUpdateError("Requesting changes from review requires feedback_content", 400)
    elif current_state == TaskAssignmentState.DONE:
        if requested_state and target_state not in HUMAN_DONE_TARGET_STATES:
            raise AssignmentUpdateError("Completed assignments can only be approved, resumed, or cancelled", 400)
        if target_state == TaskAssignmentState.RUNNING and not has_feedback:
            raise AssignmentUpdateError("Reopening a completed assignment requires feedback_content", 400)
        if target_state == TaskAssignmentState.DONE and requested_task_status != TaskStatus.DONE:
            raise AssignmentUpdateError("Approving a completed assignment requires task_status done", 400)
    else:
        if requested_state and target_state not in HUMAN_ACTIVE_TARGET_STATES:
            raise AssignmentUpdateError("Active assignments can only be run, paused for input, reviewed, or cancelled", 400)

    if requested_task_status == TaskStatus.TODO:
        raise AssignmentUpdateError("Assignment updates cannot move a task back to todo", 400)

    if requested_task_status == TaskStatus.DONE and target_state != TaskAssignmentState.DONE:
        raise AssignmentUpdateError("Task status done requires assignment state done", 400)

    if requested_task_status == TaskStatus.CANCELLED and target_state != TaskAssignmentState.CANCELLED:
        raise AssignmentUpdateError("Task status cancelled requires assignment state cancelled", 400)

    if requested_task_status == TaskStatus.IN_PROGRESS and target_state != TaskAssignmentState.RUNNING:
        raise AssignmentUpdateError("Task status in_progress requires assignment state running", 400)

    if requested_task_status == TaskStatus.REVIEW and target_state not in {
        TaskAssignmentState.WAITING_HUMAN,
        TaskAssignmentState.REVIEW,
        TaskAssignmentState.DONE,
    }:
        raise AssignmentUpdateError("Task status review requires a review or human-waiting assignment", 400)


def build_task_snapshot(task):
    project = task.project
    return {
        "task": task.to_dict(include_project=True),
        "project": project.to_dict(include_stats=False) if project else None,
    }


def active_assignment_filter(now):
    return or_(
        and_(
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            or_(TaskAssignment.lease_expires_at.is_(None), TaskAssignment.lease_expires_at >= now),
        ),
        TaskAssignment.state.in_(HUMAN_BLOCKING_ASSIGNMENT_STATES),
    )


def expire_assignment(assignment, now=None):
    now = now or datetime.utcnow()
    if (
        assignment.state not in LEASED_EXECUTION_STATES
        or assignment.lease_expires_at is None
        or assignment.lease_expires_at >= now
    ):
        return None

    old_state = assignment.state
    assignment.state = TaskAssignmentState.EXPIRED
    assignment.completed_at = now
    db.session.add(assignment)

    run = AgentRun.query.filter_by(assignment_id=assignment.id).order_by(AgentRun.started_at.desc()).first()
    if run and run.status in [AgentRunStatus.RUNNING, AgentRunStatus.WAITING_HUMAN]:
        run.status = AgentRunStatus.EXPIRED
        run.ended_at = now
        db.session.add(run)

    record_task_event(
        assignment.task_id,
        "assignment_expired",
        actor_type="system",
        payload={
            "assignment_id": assignment.id,
            "agent_id": assignment.agent_id,
            "run_id": run.id if run else None,
            "old_state": old_state.value if old_state else None,
            "new_state": TaskAssignmentState.EXPIRED.value,
            "lease_expires_at": assignment.lease_expires_at.isoformat() if assignment.lease_expires_at else None,
            "last_heartbeat_at": assignment.last_heartbeat_at.isoformat() if assignment.last_heartbeat_at else None,
            "expired_at": now.isoformat(),
        },
    )

    return assignment


def expire_stale_assignments(current_user=None, agent_id=None, task_id=None):
    now = datetime.utcnow()
    query = TaskAssignment.query.filter(
        TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
        TaskAssignment.lease_expires_at.isnot(None),
        TaskAssignment.lease_expires_at < now,
    )

    if current_user:
        query = query.join(Task, TaskAssignment.task_id == Task.id).join(
            Project,
            Task.project_id == Project.id,
        ).filter(Project.owner_id == current_user.id)

    if agent_id:
        query = query.filter(TaskAssignment.agent_id == agent_id)

    if task_id:
        query = query.filter(TaskAssignment.task_id == task_id)

    stale_assignments = query.all()
    for assignment in stale_assignments:
        expire_assignment(assignment, now)

    return stale_assignments


def normalize_match_terms(values):
    terms = set()
    for value in values or []:
        if value is None:
            continue

        raw_value = str(value).strip().lower()
        if not raw_value:
            continue

        terms.add(raw_value)
        for token in CAPABILITY_TOKEN_PATTERN.split(raw_value):
            if len(token) >= 2:
                terms.add(token)

    return terms


# Capability hierarchy: parent → children (possessing parent implies possessing children)
_CAPABILITY_HIERARCHY = {
    "code_review": {"review", "code_review", "reading"},
    "testing": {"testing", "test", "qa"},
    "deployment": {"deployment", "deploy", "release", "ci_cd"},
    "documentation": {"documentation", "docs", "writing"},
    "research": {"research", "analysis", "investigation"},
    "coordination": {"coordination", "management", "planning", "orchestration"},
    "frontend": {"frontend", "ui", "react", "vue", "css", "html", "web"},
    "backend": {"backend", "api", "server", "database", "sql"},
    "devops": {"devops", "infrastructure", "cloud", "docker", "kubernetes"},
    "security": {"security", "audit", "compliance"},
}


def _expand_capabilities(capabilities: set[str]) -> set[str]:
    """Expand a capability set through the hierarchy.

    If an agent has "code_review", it implicitly has "review" and "reading".
    Also checks reverse: if agent has "review", it matches "code_review" requirements.
    """
    expanded = set(capabilities)
    for parent, children in _CAPABILITY_HIERARCHY.items():
        if parent in capabilities:
            expanded.update(children)
        # Reverse: having any child also partially matches parent requirements
        if capabilities.intersection(children):
            expanded.add(parent)
    return expanded


def score_task_for_agent(task, agent):
    capabilities = normalize_match_terms(agent.capabilities or [])
    if not capabilities:
        return {
            "score": 0,
            "matched_capabilities": [],
            "matched_tags": [],
            "matched_text": [],
            "missing_required": [],
        }

    task_tags = normalize_match_terms(task.tags or [])
    project = task.project
    searchable_text = normalize_match_terms(
        [
            task.title,
            task.content,
            project.name if project else None,
            project.description if project else None,
            project.project_context if project else None,
        ]
    )

    matched_tags = sorted(capabilities.intersection(task_tags))
    matched_text = sorted(capabilities.intersection(searchable_text).difference(matched_tags))

    # Check required_capabilities: the agent must possess all of them
    required_caps = task.required_capabilities or []
    missing_required = []
    if required_caps:
        expanded_caps = _expand_capabilities(capabilities)
        for req in required_caps:
            req_norm = normalize_match_terms([req])
            if not req_norm.intersection(expanded_caps):
                missing_required.append(req)

    # Score: tag matches are strongest, text matches moderate, required_capabilities bonus
    score = len(matched_tags) * 10 + len(matched_text) * 2
    if required_caps and not missing_required:
        score += 50  # bonus for meeting all requirements
    if missing_required:
        score = max(0, score - 30)  # penalty for missing required capabilities

    # Workload penalty: agents with more active assignments get lower scores
    active_count = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent.id,
        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
    ).count()
    score = max(0, score - active_count * 15)

    # Reputation bonus: higher reputation agents get priority
    rep = AgentReputation.query.filter_by(agent_id=agent.id).first()
    if rep and rep.score > 50:
        score += int((rep.score - 50) * 0.5)  # Up to +25 bonus for score 100

    # Experience bonus: agents with relevant past experiences get priority
    task_domain = None
    if task.tags:
        task_domain = task.tags[0] if task.tags else None
    relevant_experiences = AgentExperience.find_relevant_experiences(
        agent_id=agent.id,
        domain=task_domain,
        capabilities=list(capabilities)[:3] if capabilities else None,
        include_shared=False,
        limit=5,
    )
    exp_bonus = 0
    if relevant_experiences:
        # Each relevant experience adds a small bonus, capped at +20
        exp_bonus = min(20, len(relevant_experiences) * 5)
        # Weight by average confidence
        avg_conf = sum(e.confidence for e in relevant_experiences) / len(relevant_experiences)
        exp_bonus = int(exp_bonus * avg_conf)
        score += exp_bonus

    # Skill profile bonus (P3.1): profiled skills matching the task add priority
    from services.skill_profile import skill_profile_bonus as _sp_bonus

    profile_bonus = _sp_bonus(agent, set(matched_tags) | set(matched_text))
    score += profile_bonus

    # Load throttle (P3.3): agents forecast as overloaded are deprioritized
    from services.insight_actions import compute_load_throttle

    throttle_penalty, load_forecast = compute_load_throttle(agent)
    score = max(0, score - throttle_penalty)

    return {
        "score": score,
        "matched_capabilities": sorted(set(matched_tags + matched_text)),
        "matched_tags": matched_tags,
        "matched_text": matched_text,
        "missing_required": missing_required,
        "experience_bonus": exp_bonus if relevant_experiences else 0,
        "skill_profile_bonus": profile_bonus,
        "load_throttle_penalty": throttle_penalty,
        "load_forecast": load_forecast,
    }


def _score_task_with_caps(task, capabilities, agent=None):
    """Score a task for an agent using provided capabilities list instead of agent.capabilities.

    Used for cross-project contexts where effective capabilities may differ
    from the agent's own capabilities.
    """
    cap_set = normalize_match_terms(capabilities or [])
    if not cap_set:
        return {"score": 0, "matched_capabilities": [], "matched_tags": [],
                "matched_text": [], "missing_required": []}

    task_tags = normalize_match_terms(task.tags or [])
    project = task.project
    searchable_text = normalize_match_terms([
        task.title, task.content,
        project.name if project else None,
        project.description if project else None,
        project.project_context if project else None,
    ])

    matched_tags = sorted(cap_set.intersection(task_tags))
    matched_text = sorted(cap_set.intersection(searchable_text).difference(matched_tags))

    required_caps = task.required_capabilities or []
    missing_required = []
    if required_caps:
        expanded_caps = _expand_capabilities(cap_set)
        for req in required_caps:
            req_norm = normalize_match_terms([req])
            if not req_norm.intersection(expanded_caps):
                missing_required.append(req)

    score = len(matched_tags) * 10 + len(matched_text) * 2
    if required_caps and not missing_required:
        score += 50
    if missing_required:
        score = max(0, score - 30)

    # Cross-project penalty: prefer own-project tasks
    score = max(0, score - 5)

    # Workload penalty
    if agent:
        active_count = TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent.id,
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
        ).count()
        score = max(0, score - active_count * 15)

        # Reputation bonus
        rep = AgentReputation.query.filter_by(agent_id=agent.id).first()
        if rep and rep.score > 50:
            score += int((rep.score - 50) * 0.5)

    return {
        "score": score,
        "matched_capabilities": sorted(set(matched_tags + matched_text)),
        "matched_tags": matched_tags,
        "matched_text": matched_text,
        "missing_required": missing_required,
    }


def expire_stale_assignments_for_task(task_id):
    return expire_stale_assignments(task_id=task_id)


def find_active_assignment(task_id, for_update=False):
    now = datetime.utcnow()
    query = TaskAssignment.query.filter(
        TaskAssignment.task_id == task_id,
        active_assignment_filter(now),
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def find_claimable_task(current_user, agent=None, project_id=None, match_capabilities=True):
    query = Task.query.join(Project).filter(
        Project.owner_id == current_user.id,
        Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]),
    )

    if project_id:
        query = query.filter(Task.project_id == project_id)

    candidates = query.order_by(Task.priority.desc(), Task.created_at.asc()).limit(50).all()

    # If agent is authorized for cross-project work, also search those projects
    cross_project_tasks = []
    if agent and not project_id:
        cross_auths = CrossProjectAgent.get_active_for_agent(agent.id)
        for auth in cross_auths:
            cross_tasks = Task.query.filter(
                Task.project_id == auth.project_id,
                Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]),
            ).order_by(Task.priority.desc(), Task.created_at.asc()).limit(20).all()
            for t in cross_tasks:
                expire_stale_assignments_for_task(t.id)
                if not find_active_assignment(t.id):
                    # Use effective capabilities for cross-project context
                    effective_caps = CrossProjectAgent.get_effective_capabilities(agent.id, auth.project_id)
                    match = _score_task_with_caps(t, effective_caps, agent) if match_capabilities else {
                        "score": 0, "matched_capabilities": [], "matched_tags": [],
                        "matched_text": [], "missing_required": [], "strategy": "cross_project",
                    }
                    match["strategy"] = "cross_project"
                    match["project_id"] = auth.project_id
                    match["role_in_project"] = auth.role_in_project
                    cross_project_tasks.append((t, match))

    fallback = None
    matched_candidates = []

    for index, task in enumerate(candidates):
        expire_stale_assignments_for_task(task.id)
        if find_active_assignment(task.id):
            continue

        match = score_task_for_agent(task, agent) if agent and match_capabilities else {
            "score": 0,
            "matched_capabilities": [],
            "matched_tags": [],
            "matched_text": [],
            "missing_required": [],
        }
        match["strategy"] = "capability_match" if agent and match_capabilities else "priority_fifo"

        if fallback is None:
            fallback = (task, match)

        if match["score"] > 0:
            matched_candidates.append((task, match, index))

    # Add cross-project candidates
    for t, match in cross_project_tasks:
        if match["score"] > 0:
            matched_candidates.append((t, match, len(candidates) + len(cross_project_tasks)))

    if matched_candidates:
        matched_candidates.sort(key=lambda item: (-item[1]["score"], item[2]))
        task, match, _ = matched_candidates[0]
        return task, match

    if fallback:
        return fallback

    return None, None


def _check_parent_blocking(task):
    """When a subtask changes state, update the parent's blocked status.

    Delegates to Task.try_unblock_parent which handles both directions:
    - block parent when subtasks are still open
    - unblock parent when all subtasks are terminal
    """
    task.try_unblock_parent()


def apply_assignment_update(current_user, assignment, task, data, actor_agent=None):
    if actor_agent:
        validate_agent_assignment_update(assignment, data)
    else:
        validate_human_assignment_update(assignment, data)

    old_state = assignment.state
    now = datetime.utcnow()

    if "state" in data:
        assignment.state = parse_enum(TaskAssignmentState, data["state"], "state")

    if "progress_rate" in data:
        assignment.progress_rate = max(0, min(100, int(data["progress_rate"])))

    if "notes" in data:
        assignment.notes = data["notes"]

    if "lease_seconds" in data and not assignment.is_terminal:
        lease_seconds = max(60, min(int(data["lease_seconds"]), 24 * 60 * 60))
        assignment.lease_expires_at = now + timedelta(seconds=lease_seconds)

    if actor_agent:
        assignment.last_heartbeat_at = now
        actor_agent.last_seen_at = now
        if actor_agent.status == AgentStatus.OFFLINE:
            actor_agent.status = AgentStatus.ACTIVE

    run = AgentRun.query.filter_by(assignment_id=assignment.id).order_by(AgentRun.started_at.desc()).first()

    if "output_summary" in data and run:
        run.output_summary = data["output_summary"]

    if "error" in data and run:
        run.error = data["error"]

    if "run_metadata" in data and run:
        run.run_metadata = data["run_metadata"] or {}

    feedback_content = data.get("feedback_content")
    if feedback_content:
        task.feedback_content = feedback_content
        task.feedback_at = now

    if assignment.state == TaskAssignmentState.RUNNING:
        task.status = TaskStatus.IN_PROGRESS
        if run:
            run.status = AgentRunStatus.RUNNING
    elif assignment.state == TaskAssignmentState.WAITING_HUMAN:
        task.status = TaskStatus.REVIEW
        if run:
            run.status = AgentRunStatus.WAITING_HUMAN
    elif assignment.state == TaskAssignmentState.REVIEW:
        task.status = TaskStatus.REVIEW
    elif assignment.state == TaskAssignmentState.DONE:
        assignment.completed_at = now
        assignment.progress_rate = 100
        task.status = TaskStatus.REVIEW
        if run:
            run.status = AgentRunStatus.SUCCEEDED
            run.ended_at = now
    elif assignment.state == TaskAssignmentState.FAILED:
        assignment.completed_at = now
        if run:
            run.status = AgentRunStatus.FAILED
            run.ended_at = now
    elif assignment.state == TaskAssignmentState.CANCELLED:
        assignment.completed_at = now
        if run:
            run.status = AgentRunStatus.CANCELLED
            run.ended_at = now

    if data.get("task_status"):
        task.status = parse_enum(TaskStatus, data["task_status"], "task_status")

    # Subtask dependency: when a subtask reaches a terminal state, check
    # whether the parent task should be unblocked (all subtasks done/review)
    # or remain blocked.
    _check_parent_blocking(task)

    record_task_event(
        task.id,
        "assignment_updated",
        current_user=current_user if not actor_agent else None,
        agent=actor_agent,
        payload={
            "assignment_id": assignment.id,
            "old_state": old_state.value if old_state else None,
            "new_state": assignment.state.value if assignment.state else None,
            "progress_rate": assignment.progress_rate,
            "has_feedback": bool(feedback_content),
            "feedback_excerpt": feedback_content[:240] if feedback_content else None,
        },
    )

    return run


# ── Dispatch helpers (defined in _dispatch_helpers, re-exported here so
#    dispatch.py and task operation submodules can import from a single hub) ──
from ._dispatch_helpers import (  # noqa: E402,F401
    DISPATCH_MAX_ASSIGNMENTS,
    DISPATCH_PREVIEW_CANDIDATE_LIMIT,
    DISPATCH_POLICY_DEFAULTS,
    normalize_dispatch_policy,
    get_coordinator_dispatch_policy,
    resolve_dispatch_options,
    collect_claimable_tasks,
    find_available_worker_agents,
    serialize_dispatch_candidate,
    create_assignment_with_run,
    cancel_assignment_for_handoff,
)
