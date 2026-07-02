"""
Agent collaboration API.
"""

import csv
import io
import re
from datetime import datetime, timedelta
from flask import Blueprint, make_response, request
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
from .base import ApiResponse, get_request_args, paginate_query, validate_json_request
from .sse import notify_sse

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


from workflow_templates import WORKFLOW_TEMPLATES

agents_bp = Blueprint("agents", __name__)


CAPABILITY_TOKEN_PATTERN = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")

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


class AssignmentUpdateError(ValueError):
    """Rejected assignment update that should not mutate assignment state."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


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
    if relevant_experiences:
        # Each relevant experience adds a small bonus, capped at +20
        exp_bonus = min(20, len(relevant_experiences) * 5)
        # Weight by average confidence
        avg_conf = sum(e.confidence for e in relevant_experiences) / len(relevant_experiences)
        exp_bonus = int(exp_bonus * avg_conf)
        score += exp_bonus

    return {
        "score": score,
        "matched_capabilities": sorted(set(matched_tags + matched_text)),
        "matched_tags": matched_tags,
        "matched_text": matched_text,
        "missing_required": missing_required,
        "experience_bonus": exp_bonus if relevant_experiences else 0,
    }


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


@agents_bp.route("", methods=["GET"])
@unified_auth_required
def list_agents():
    """List current user's Agents."""
    try:
        current_user = get_current_user()
        args = get_request_args()

        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        expired_assignments = expire_stale_assignments(current_user=current_user)
        if stale_agents or expired_assignments:
            db.session.commit()

        query = Agent.query.filter_by(owner_id=current_user.id)

        status = request.args.get("status")
        if status:
            try:
                query = query.filter_by(status=parse_enum(AgentStatus, status, "status"))
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        if args["search"]:
            search_term = f"%{args['search']}%"
            query = query.filter(Agent.name.like(search_term) | Agent.description.like(search_term))

        if args["sort_by"] == "name":
            order_column = Agent.name
        elif args["sort_by"] == "last_seen_at":
            order_column = Agent.last_seen_at
        else:
            order_column = Agent.created_at

        query = query.order_by(order_column.desc() if args["sort_order"] == "desc" else order_column.asc())
        result = paginate_serialized(
            query,
            args["page"],
            args["per_page"],
            lambda agent: agent.to_dict(include_stats=True),
        )

        return ApiResponse.success(result, "Agents retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve Agents: {str(e)}", 500).to_response()


@agents_bp.route("", methods=["POST"])
@unified_auth_required
def create_agent():
    """Create an Agent identity."""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=["name"],
            optional_fields=["description", "kind", "status", "provider", "model", "capabilities", "config", "collaboration_role"],
        )

        if isinstance(data, tuple):
            return data

        try:
            kind = parse_enum(AgentKind, data.get("kind", AgentKind.ASSISTANT.value), "kind")
            status = parse_enum(AgentStatus, data.get("status", AgentStatus.ACTIVE.value), "status")
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        agent = Agent(
            owner_id=current_user.id,
            name=data["name"].strip(),
            description=data.get("description"),
            kind=kind,
            status=status,
            provider=data.get("provider"),
            model=data.get("model"),
            capabilities=data.get("capabilities") or [],
            config=data.get("config") or {},
            last_seen_at=datetime.utcnow() if status == AgentStatus.ACTIVE else None,
            created_by=current_user.email,
        )

        db.session.add(agent)
        db.session.commit()

        AuditLog.record(
            action="agent.created", resource_type="agent", resource_id=agent.id,
            actor_type="human", actor_user_id=current_user.id,
            detail={"name": agent.name, "kind": kind.value},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.created(agent.to_dict(include_stats=True), "Agent created successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create Agent: {str(e)}", 500).to_response()


@agents_bp.route("/self-register", methods=["POST"])
@unified_auth_required
def self_register_agent():
    """Allow an external Agent to self-register into the platform.

    If an agent with the same name and provider already exists, it updates
    the existing record instead of creating a duplicate. This supports
    idempotent registration by agents that restart frequently.
    """
    try:
        user = get_current_user()
        data = validate_json_request(
            required_fields=["name"],
            optional_fields=["description", "kind", "provider", "model", "capabilities", "config", "collaboration_role"],
        )

        if isinstance(data, tuple):
            return data

        name = data["name"].strip()
        provider = data.get("provider", "")

        # Check for existing agent with same name + provider (idempotent registration)
        existing = Agent.query.filter_by(owner_id=user.id, name=name, provider=provider).first() if provider else None

        if existing:
            # Update existing agent
            if data.get("description"):
                existing.description = data["description"]
            if data.get("model"):
                existing.model = data["model"]
            if data.get("capabilities"):
                existing.capabilities = data["capabilities"]
            if data.get("config"):
                existing.config = data["config"]
            if data.get("collaboration_role"):
                existing.collaboration_role = data["collaboration_role"]
            existing.status = AgentStatus.ACTIVE
            existing.last_seen_at = datetime.utcnow()
            db.session.commit()

            AuditLog.record("agent.self_register", target_type="agent", target_id=existing.id,
                            actor_type="agent", actor_user_id=user.id,
                            detail={"name": name, "action": "updated"}, ip_address=_client_ip())
            db.session.commit()

            return ApiResponse.success(existing.to_dict(include_stats=True), "Agent re-registered").to_response()

        # Create new agent
        try:
            kind = parse_enum(AgentKind, data.get("kind", AgentKind.AUTONOMOUS.value), "kind")
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        agent = Agent(
            owner_id=user.id,
            name=name,
            description=data.get("description"),
            kind=kind,
            status=AgentStatus.ACTIVE,
            provider=provider,
            model=data.get("model"),
            capabilities=data.get("capabilities") or [],
            config=data.get("config") or {},
            collaboration_role=data.get("collaboration_role"),
            last_seen_at=datetime.utcnow(),
            created_by=user.email,
        )
        db.session.add(agent)
        db.session.commit()

        AuditLog.record("agent.self_register", target_type="agent", target_id=agent.id,
                        actor_type="agent", actor_user_id=user.id,
                        detail={"name": name, "action": "created"}, ip_address=_client_ip())
        db.session.commit()

        return ApiResponse.created(agent.to_dict(include_stats=True), "Agent self-registered").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Self-registration failed: {str(e)}", 500).to_response()


@agents_bp.route("/discover", methods=["GET"])
@unified_auth_required
def discover_agents():
    """Find available Agents by capability, role, or kind.

    Query params:
      capability — filter by capability (can be repeated)
      collaboration_role — filter by role (leader/follower/standalone)
      kind — filter by kind (assistant/autonomous/coordinator/external)
      status — filter by status (default: active)
    """
    user = get_current_user()
    query = Agent.query.filter_by(owner_id=user.id)

    status = request.args.get("status", "active")
    if status:
        try:
            status_enum = parse_enum(AgentStatus, status, "status")
            query = query.filter_by(status=status_enum)
        except ValueError:
            pass

    kind = request.args.get("kind")
    if kind:
        try:
            kind_enum = parse_enum(AgentKind, kind, "kind")
            query = query.filter_by(kind=kind_enum)
        except ValueError:
            pass

    role = request.args.get("collaboration_role")
    if role:
        query = query.filter_by(collaboration_role=role)

    capabilities = request.args.getlist("capability")
    agents = query.all()

    # Post-filter by capabilities (JSON column query is DB-dependent)
    if capabilities:
        filtered = []
        for agent in agents:
            agent_caps = set(agent.capabilities or [])
            if any(c in agent_caps for c in capabilities):
                filtered.append(agent)
        agents = filtered

    return ApiResponse.success([a.to_dict(include_stats=True) for a in agents]).to_response()


@agents_bp.route("/review-queue", methods=["GET"])
@unified_auth_required
def list_review_queue():
    """List Agent assignments that need human attention."""
    try:
        current_user = get_current_user()
        args = get_request_args()

        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        expired_assignments = expire_stale_assignments(current_user=current_user)
        if stale_agents or expired_assignments:
            db.session.commit()

        query = TaskAssignment.query.join(Task, TaskAssignment.task_id == Task.id).join(
            Project,
            Task.project_id == Project.id,
        ).filter(Project.owner_id == current_user.id)

        action = request.args.get("action", "all")
        if action == "human_feedback":
            query = query.filter(TaskAssignment.state == TaskAssignmentState.WAITING_HUMAN)
        elif action == "final_review":
            query = query.filter(
                TaskAssignment.state.in_([TaskAssignmentState.REVIEW, TaskAssignmentState.DONE]),
                Task.status == TaskStatus.REVIEW,
            )
        elif action == "all":
            query = query.filter(
                or_(
                    TaskAssignment.state == TaskAssignmentState.WAITING_HUMAN,
                    TaskAssignment.state == TaskAssignmentState.REVIEW,
                    and_(TaskAssignment.state == TaskAssignmentState.DONE, Task.status == TaskStatus.REVIEW),
                )
            )
        else:
            return ApiResponse.error("Invalid action. Must be one of: all, human_feedback, final_review", 400).to_response()

        query = query.order_by(TaskAssignment.updated_at.desc(), TaskAssignment.created_at.desc())
        result = paginate_serialized(query, args["page"], args["per_page"], serialize_review_queue_item)

        return ApiResponse.success(result, "Agent review queue retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve Agent review queue: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>", methods=["GET"])
@unified_auth_required
def get_agent(agent_id):
    """Get Agent details."""
    current_user = get_current_user()
    agent, response = get_owned_agent_or_response(agent_id, current_user)
    if response:
        return response

    stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
    expired_assignments = expire_stale_assignments(current_user=current_user, agent_id=agent.id)
    if stale_agents or expired_assignments:
        db.session.commit()

    return ApiResponse.success(agent.to_dict(include_stats=True), "Agent retrieved successfully").to_response()


@agents_bp.route("/<int:agent_id>", methods=["PUT"])
@unified_auth_required
def update_agent(agent_id):
    """Update Agent settings."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        data = validate_json_request(
            optional_fields=["name", "description", "kind", "status", "provider", "model", "capabilities", "config", "collaboration_role"],
        )

        if isinstance(data, tuple):
            return data

        if "kind" in data:
            try:
                data["kind"] = parse_enum(AgentKind, data["kind"], "kind")
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        if "status" in data:
            try:
                data["status"] = parse_enum(AgentStatus, data["status"], "status")
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        if "name" in data:
            data["name"] = data["name"].strip()

        # Capability registration mode: merge (default) or replace
        cap_mode = data.pop("_capability_mode", "merge")
        if "capabilities" in data and cap_mode == "merge":
            existing = set(agent.capabilities or [])
            new_caps = set(data["capabilities"] or [])
            data["capabilities"] = sorted(existing | new_caps)

        agent.update_from_dict(data)
        if data.get("status") == AgentStatus.ACTIVE:
            agent.last_seen_at = datetime.utcnow()
        db.session.commit()

        # If capabilities or config changed, notify the owner via SSE + Notification
        config_changed_fields = [f for f in ("capabilities", "config") if f in data]
        if config_changed_fields:
            _queue_sse(
                current_user.id,
                "agent_config_changed",
                {"agent_id": agent.id, "agent_name": agent.name, "changed_fields": config_changed_fields},
            )
            Notification.create_notification(
                user_id=current_user.id,
                event_type="agent_config_changed",
                agent_id=agent.id,
                payload={"changed_fields": config_changed_fields},
            )

        return ApiResponse.success(agent.to_dict(include_stats=True), "Agent updated successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update Agent: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/heartbeat", methods=["POST"])
@unified_auth_required
def heartbeat_agent(agent_id):
    """Record Agent heartbeat."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status:
            try:
                agent.status = parse_enum(AgentStatus, status, "status")
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()
        else:
            agent.heartbeat()

        agent.last_seen_at = datetime.utcnow()
        db.session.commit()

        return ApiResponse.success(agent.to_dict(include_stats=True), "Agent heartbeat recorded").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to record Agent heartbeat: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/recommended-tasks", methods=["GET"])
@unified_auth_required
def list_recommended_tasks(agent_id):
    """List unassigned tasks that match this Agent's capabilities, sorted by score.

    Query params:
      limit — max tasks to return (default 10, max 50)
      project_id — filter by project
    """
    user = get_current_user()
    agent, response = get_owned_agent_or_response(agent_id, user)
    if response:
        return response

    limit = min(request.args.get("limit", 10, type=int), 50)
    project_id = request.args.get("project_id", type=int)

    # Find unassigned TODO tasks
    assigned_task_ids = {a.task_id for a in TaskAssignment.query.filter(
        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
    ).all()}

    query = Task.query.filter(
        Task.status == TaskStatus.TODO,
        Task.is_ai_task == True,
        ~Task.id.in_(assigned_task_ids),
    )
    if project_id:
        query = query.filter_by(project_id=project_id)
    tasks = query.order_by(Task.created_at.desc()).limit(200).all()

    # Score each task
    scored = []
    for task in tasks:
        result = score_task_for_agent(task, agent)
        if result["score"] > 0:
            scored.append({
                "task": task.to_dict(),
                "score": result["score"],
                "matched_capabilities": result["matched_capabilities"],
                "matched_tags": result["matched_tags"],
                "matched_text": result["matched_text"],
                "missing_required": result["missing_required"],
            })

    scored.sort(key=lambda x: -x["score"])
    return ApiResponse.success(scored[:limit], f"Found {len(scored)} recommended tasks").to_response()


@agents_bp.route("/<int:agent_id>/assignments", methods=["GET"])
@unified_auth_required
def list_agent_assignments(agent_id):
    """List assignments for an Agent."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        expired_assignments = expire_stale_assignments(current_user=current_user, agent_id=agent.id)
        if stale_agents or expired_assignments:
            db.session.commit()

        args = get_request_args()
        query = TaskAssignment.query.filter_by(agent_id=agent.id)

        state = request.args.get("state")
        if state:
            try:
                query = query.filter_by(state=parse_enum(TaskAssignmentState, state, "state"))
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        query = query.order_by(TaskAssignment.created_at.desc())
        result = paginate_serialized(
            query,
            args["page"],
            args["per_page"],
            lambda assignment: assignment.to_dict(include_task=True, include_agent=True),
        )

        return ApiResponse.success(result, "Agent assignments retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve Agent assignments: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/claim", methods=["POST"])
@unified_auth_required
def claim_task(agent_id):
    """Claim a task for execution."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if agent.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Agent is not available to claim tasks", 409).to_response()

        data = request.get_json(silent=True) or {}
        lease_seconds = int(data.get("lease_seconds") or 1800)
        lease_seconds = max(60, min(lease_seconds, 24 * 60 * 60))

        task = None
        capability_match = None
        is_manual_dispatch = bool(data.get("task_id") and data.get("dispatch_source") == "human")
        if data.get("task_id"):
            task, response = get_owned_task_or_response(data["task_id"], current_user)
            if response:
                return response
            expire_stale_assignments_for_task(task.id)
            active_assignment = find_active_assignment(task.id, for_update=True)
            if active_assignment:
                return ApiResponse.error(
                    "Task already has an active assignment",
                    409,
                    assignment=active_assignment.to_dict(include_agent=True),
                ).to_response()
        else:
            task, capability_match = find_claimable_task(
                current_user,
                agent=agent,
                project_id=data.get("project_id"),
                match_capabilities=data.get("match_capabilities", True) is not False,
            )

        if not task:
            db.session.commit()
            return ApiResponse.success(None, "No claimable task found").to_response()

        now = datetime.utcnow()
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

        if task.status == TaskStatus.TODO:
            task.status = TaskStatus.IN_PROGRESS

        agent.last_seen_at = now
        if agent.status == AgentStatus.OFFLINE:
            agent.status = AgentStatus.ACTIVE
        run_metadata = data.get("run_metadata") if isinstance(data.get("run_metadata"), dict) else {}
        if capability_match:
            run_metadata = {
                **run_metadata,
                "capability_match": capability_match,
            }
        run_metadata = {
            **run_metadata,
            "claim_mode": "manual_dispatch" if is_manual_dispatch else "agent_claim",
        }

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
        db.session.flush()

        record_task_event(
            task.id,
            "task_claimed",
            current_user=current_user if is_manual_dispatch else None,
            agent=None if is_manual_dispatch else agent,
            payload={
                "assignment_id": assignment.id,
                "agent_id": agent.id,
                "run_id": run.id,
                "lease_seconds": lease_seconds,
                "claim_mode": run_metadata["claim_mode"],
                "run_metadata": run_metadata,
                "capability_match": capability_match,
            },
        )

        db.session.commit()
        flush_sse_notifications()

        AuditLog.record(
            action="task.claimed", resource_type="task", resource_id=task.id,
            actor_type="agent" if not is_manual_dispatch else "human",
            actor_user_id=current_user.id if is_manual_dispatch else None,
            actor_agent_id=agent.id if not is_manual_dispatch else None,
            project_id=task.project_id,
            detail={"agent_id": agent.id, "assignment_id": assignment.id, "run_id": run.id},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.created(serialize_claim_response(agent, assignment, run), "Task claimed successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to claim task: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/assignments/<int:assignment_id>", methods=["PUT"])
@unified_auth_required
def update_assignment(agent_id, assignment_id):
    """Update assignment progress/state."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        assignment = TaskAssignment.query.filter_by(id=assignment_id, agent_id=agent.id).first()
        if not assignment:
            return ApiResponse.error("Assignment not found", 404).to_response()

        task, response = get_owned_task_or_response(assignment.task_id, current_user)
        if response:
            return response

        expired_assignment = expire_assignment(assignment)
        if expired_assignment:
            db.session.commit()
            return ApiResponse.error(
                "Assignment lease has expired",
                409,
                assignment=assignment.to_dict(include_task=True, include_agent=True),
            ).to_response()

        data = validate_json_request(
            optional_fields=[
                "state",
                "progress_rate",
                "notes",
                "feedback_content",
                "output_summary",
                "error",
                "lease_seconds",
                "task_status",
                "run_metadata",
            ],
        )

        if isinstance(data, tuple):
            return data

        try:
            run = apply_assignment_update(current_user, assignment, task, data, actor_agent=agent)
        except AssignmentUpdateError as e:
            record_assignment_update_rejected(current_user, assignment, data, agent, str(e))
            db.session.commit()
            return ApiResponse.error(str(e), e.status_code).to_response()
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            {
                "assignment": assignment.to_dict(include_task=True, include_agent=True),
                "run": run.to_dict() if run else None,
            },
            "Assignment updated successfully",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update assignment: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/assignments", methods=["GET"])
@unified_auth_required
def list_task_assignments(task_id):
    """List Agent assignments for a task."""
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        expire_stale_assignments_for_task(task.id)
        db.session.commit()

        args = get_request_args()
        query = TaskAssignment.query.filter_by(task_id=task.id)

        state = request.args.get("state")
        if state == "active":
            now = datetime.utcnow()
            query = query.filter(
                active_assignment_filter(now),
            )
        elif state:
            try:
                query = query.filter_by(state=parse_enum(TaskAssignmentState, state, "state"))
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        query = query.order_by(TaskAssignment.created_at.desc())
        result = paginate_serialized(
            query,
            args["page"],
            args["per_page"],
            lambda assignment: assignment.to_dict(include_task=False, include_agent=True, include_runs=True),
        )

        return ApiResponse.success(result, "Task assignments retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve task assignments: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/assignments/<int:assignment_id>", methods=["PUT"])
@unified_auth_required
def update_task_assignment(task_id, assignment_id):
    """Update a task assignment as the current human user."""
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        assignment = TaskAssignment.query.filter_by(id=assignment_id, task_id=task.id).first()
        if not assignment:
            return ApiResponse.error("Assignment not found", 404).to_response()

        expired_assignment = expire_assignment(assignment)
        if expired_assignment:
            db.session.commit()
            return ApiResponse.error(
                "Assignment lease has expired",
                409,
                assignment=assignment.to_dict(include_task=True, include_agent=True),
            ).to_response()

        data = validate_json_request(
            optional_fields=[
                "state",
                "progress_rate",
                "notes",
                "feedback_content",
                "output_summary",
                "error",
                "lease_seconds",
                "task_status",
                "run_metadata",
            ],
        )

        if isinstance(data, tuple):
            return data

        try:
            run = apply_assignment_update(current_user, assignment, task, data, actor_agent=None)
        except AssignmentUpdateError as e:
            record_assignment_update_rejected(current_user, assignment, data, None, str(e))
            db.session.commit()
            return ApiResponse.error(str(e), e.status_code).to_response()
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            {
                "assignment": assignment.to_dict(include_task=True, include_agent=True),
                "run": run.to_dict() if run else None,
            },
            "Task assignment updated successfully",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update task assignment: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/events", methods=["GET"])
@unified_auth_required
def list_task_events(task_id):
    """List collaboration events for a task."""
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        expired_assignments = expire_stale_assignments_for_task(task.id)
        if expired_assignments:
            db.session.commit()

        args = get_request_args()
        query = TaskEvent.query.filter_by(task_id=task.id)

        # Incremental polling: only return events newer than a known id. This
        # lets the UI / Agents cheaply poll for live collaboration updates.
        since_id = request.args.get("since_id", type=int)
        if since_id:
            query = query.filter(TaskEvent.id > since_id)
            events = query.order_by(TaskEvent.id.asc()).limit(args["per_page"]).all()
            latest_id = events[-1].id if events else since_id
            return ApiResponse.success(
                {
                    "items": [event.to_dict() for event in events],
                    "latest_id": latest_id,
                    "since_id": since_id,
                },
                "Task events retrieved successfully",
            ).to_response()

        query = query.order_by(TaskEvent.created_at.desc())
        result = paginate_query(query, args["page"], args["per_page"])

        return ApiResponse.success(result, "Task events retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve task events: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/events", methods=["POST"])
@unified_auth_required
def post_task_event(task_id):
    """Post a collaboration message to a task timeline as a human or an Agent.

    This is the inter-agent communication primitive: an Agent (identified by
    ``agent_id``, which must belong to the caller) or the human owner can leave
    messages, hand off work, raise blockers, or record decisions that other
    Agents read via ``GET /tasks/<id>/events``.
    """
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        data = validate_json_request(
            optional_fields=["event_type", "content", "agent_id", "to_agent_id", "payload"],
        )
        if isinstance(data, tuple):
            return data

        event_type = (data.get("event_type") or "message").strip().lower()
        if event_type not in POSTABLE_EVENT_TYPES:
            valid = ", ".join(sorted(POSTABLE_EVENT_TYPES))
            return ApiResponse.error(
                f"Invalid event_type. Must be one of: {valid}", 400
            ).to_response()

        content = data.get("content")
        if content is not None and not isinstance(content, str):
            return ApiResponse.error("content must be a string", 400).to_response()
        if content is not None:
            content = content.strip()
            if len(content) > POSTABLE_EVENT_CONTENT_MAX:
                return ApiResponse.error(
                    f"content exceeds {POSTABLE_EVENT_CONTENT_MAX} characters", 400
                ).to_response()

        extra_payload = data.get("payload")
        if extra_payload is not None and not isinstance(extra_payload, dict):
            return ApiResponse.error("payload must be an object", 400).to_response()

        if not content and not extra_payload:
            return ApiResponse.error("content or payload is required", 400).to_response()

        actor_agent = None
        agent_id = data.get("agent_id")
        if agent_id is not None:
            actor_agent, response = get_owned_agent_or_response(agent_id, current_user)
            if response:
                return response

        # Optional directed @mention: address this message to a specific Agent so
        # it surfaces in that Agent's inbox (GET /agents/<id>/inbox).
        target_agent = None
        to_agent_id = data.get("to_agent_id")
        if to_agent_id is not None:
            target_agent, response = get_owned_agent_or_response(to_agent_id, current_user)
            if response:
                return response

        payload = dict(extra_payload) if extra_payload else {}
        if content:
            payload["content"] = content
        if target_agent is not None:
            payload["to_agent_id"] = target_agent.id
            payload["to_agent_name"] = target_agent.name

        # --- request/response pairing for question/answer protocol ---
        # When an Agent posts a "question", mark it as awaiting an answer so
        # other Agents / the inbox can surface it.  When an "answer" is posted,
        # automatically link it to the most recent unanswered question on this
        # task directed at the answering Agent (or the most recent question
        # overall if to_agent_id is not set).
        if event_type == "question":
            payload["awaiting_answer"] = True

        if event_type == "answer":
            # Find the most recent unanswered question on this task.
            # Use Python-side filtering for JSON payload compatibility across
            # SQLite and PostgreSQL.
            recent_questions = (
                TaskEvent.query
                .filter(TaskEvent.task_id == task.id, TaskEvent.event_type == "question")
                .order_by(TaskEvent.id.desc())
                .limit(20)
                .all()
            )
            latest_question = None
            for q in recent_questions:
                q_payload = q.payload or {}
                if not q_payload.get("awaiting_answer", False):
                    continue
                if actor_agent and q_payload.get("to_agent_id") == actor_agent.id:
                    latest_question = q
                    break
                if not actor_agent and not q_payload.get("to_agent_id"):
                    latest_question = q
                    break
            # Fallback: any unanswered question
            if not latest_question:
                for q in recent_questions:
                    if (q.payload or {}).get("awaiting_answer", False):
                        latest_question = q
                        break
            if latest_question:
                payload["reply_to_event_id"] = latest_question.id
                # Mark the question as answered
                q_payload = dict(latest_question.payload or {})
                q_payload["awaiting_answer"] = False
                q_payload["answered_by_event_id"] = None  # filled after flush
                latest_question.payload = q_payload
                db.session.add(latest_question)
                # We'll fill answered_by_event_id after we get our event id
                payload["_pending_answered_question_id"] = latest_question.id

        event = record_task_event(
            task.id,
            event_type,
            current_user=current_user if not actor_agent else None,
            agent=actor_agent,
            payload=payload,
        )

        # Back-fill answered_by_event_id on the original question
        pending_q_id = payload.pop("_pending_answered_question_id", None)
        if pending_q_id and event.id:
            q_event = TaskEvent.query.get(pending_q_id)
            if q_event:
                q_payload = dict(q_event.payload or {})
                q_payload["answered_by_event_id"] = event.id
                q_event.payload = q_payload
                db.session.add(q_event)

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            event.to_dict(), "Task event posted successfully", 201
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to post task event: {str(e)}", 500).to_response()


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


@agents_bp.route("/tasks/<int:task_id>/handoff", methods=["POST"])
@unified_auth_required
def handoff_task(task_id):
    """Hand off a task from its current Agent to another Agent.

    This is the collaboration primitive for transferring live work: the active
    assignment (if any) is cancelled and a fresh assignment + run is created for
    the target Agent, with a ``handoff`` event recording the transfer so the
    timeline shows who passed the work to whom and why.
    """
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        data = validate_json_request(
            required_fields=["to_agent_id"],
            optional_fields=["from_assignment_id", "lease_seconds", "reason", "notes"],
        )
        if isinstance(data, tuple):
            return data

        target_agent, response = get_owned_agent_or_response(data["to_agent_id"], current_user)
        if response:
            return response

        if target_agent.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Target Agent is not available to receive a handoff", 409).to_response()

        lease_seconds = int(data.get("lease_seconds") or 1800)
        lease_seconds = max(60, min(lease_seconds, 24 * 60 * 60))

        now = datetime.utcnow()
        expire_stale_assignments_for_task(task.id)

        source_assignment = find_active_assignment(task.id)
        if data.get("from_assignment_id") is not None:
            requested = TaskAssignment.query.filter_by(
                id=data["from_assignment_id"], task_id=task.id
            ).first()
            if not requested:
                return ApiResponse.error("Source assignment not found", 404).to_response()
            if source_assignment and requested.id != source_assignment.id:
                return ApiResponse.error(
                    "from_assignment_id does not match the task's active assignment", 409
                ).to_response()
            source_assignment = requested

        if source_assignment and source_assignment.agent_id == target_agent.id:
            return ApiResponse.error("Task is already assigned to the target Agent", 409).to_response()

        from_agent_id = source_assignment.agent_id if source_assignment else None
        if source_assignment and not source_assignment.is_terminal:
            cancel_assignment_for_handoff(source_assignment, now)

        reason = data.get("reason")
        notes = data.get("notes")
        run_metadata = {
            "claim_mode": "handoff",
            "handoff": {
                "from_agent_id": from_agent_id,
                "from_assignment_id": source_assignment.id if source_assignment else None,
                "reason": reason,
            },
        }

        assignment, run = create_assignment_with_run(
            task, target_agent, current_user, now, lease_seconds, run_metadata
        )
        if notes:
            assignment.notes = notes

        if task.status == TaskStatus.TODO:
            task.status = TaskStatus.IN_PROGRESS

        db.session.flush()

        record_task_event(
            task.id,
            "handoff",
            current_user=current_user,
            payload={
                "from_agent_id": from_agent_id,
                "from_assignment_id": source_assignment.id if source_assignment else None,
                "to_agent_id": target_agent.id,
                "assignment_id": assignment.id,
                "run_id": run.id,
                "lease_seconds": lease_seconds,
                "reason": reason,
                "content": reason or notes,
            },
        )

        db.session.commit()
        flush_sse_notifications()

        AuditLog.record(
            action="task.handoff", resource_type="task", resource_id=task_id,
            actor_type="human", actor_user_id=current_user.id,
            project_id=task.project_id,
            detail={
                "from_agent_id": source_assignment.agent_id if source_assignment else None,
                "to_agent_id": to_agent.id,
                "assignment_id": assignment.id,
            },
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.success(
            {
                "from_assignment": source_assignment.to_dict(include_agent=True) if source_assignment else None,
                "assignment": assignment.to_dict(include_task=True, include_agent=True),
                "run": run.to_dict(),
            },
            "Task handed off successfully",
            201,
        ).to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to hand off task: {str(e)}", 500).to_response()


DISPATCH_MAX_ASSIGNMENTS = 20


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


@agents_bp.route("/<int:agent_id>/dispatch", methods=["POST"])
@unified_auth_required
def dispatch_tasks(agent_id):
    """Coordinator auto-dispatch: assign claimable tasks to suitable worker Agents.

    This is the orchestration primitive of the multi-Agent platform. A coordinator
    Agent distributes the owner's unassigned, claimable tasks across available
    (online, idle) worker Agents using capability scoring. Each match becomes a
    fresh assignment + run, and a ``task_dispatched`` event records that the
    coordinator handed the work out, so the timeline shows who routed what to whom.

    Matching is greedy by capability score; each worker takes at most one task per
    round to spread load. Body options: ``project_id``, ``max_assignments``,
    ``lease_seconds``, ``match_capabilities``, ``require_capability_match``,
    ``candidate_agent_ids``, ``include_self``.
    """
    try:
        current_user = get_current_user()
        coordinator, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if coordinator.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Coordinator Agent is not available to dispatch tasks", 409).to_response()

        data = request.get_json(silent=True) or {}

        lease_seconds = int(data.get("lease_seconds") or 1800)
        lease_seconds = max(60, min(lease_seconds, 24 * 60 * 60))

        max_assignments = int(data.get("max_assignments") or DISPATCH_MAX_ASSIGNMENTS)
        max_assignments = max(1, min(max_assignments, DISPATCH_MAX_ASSIGNMENTS))

        match_capabilities = data.get("match_capabilities", True) is not False
        require_capability_match = bool(data.get("require_capability_match"))
        include_self = bool(data.get("include_self"))

        candidate_agent_ids = data.get("candidate_agent_ids")
        if candidate_agent_ids is not None and not isinstance(candidate_agent_ids, list):
            return ApiResponse.error("candidate_agent_ids must be a list of agent ids", 400).to_response()

        project_id = data.get("project_id")

        now = datetime.utcnow()
        mark_stale_agents_offline(owner_id=current_user.id)

        workers = find_available_worker_agents(
            current_user, coordinator, candidate_agent_ids=candidate_agent_ids, include_self=include_self
        )
        tasks = collect_claimable_tasks(current_user, project_id=project_id)

        result = {
            "coordinator": coordinator.to_dict(include_stats=True),
            "assignments": [],
            "summary": {
                "claimable_tasks": len(tasks),
                "available_agents": len(workers),
                "dispatched": 0,
                "skipped_no_match": 0,
            },
        }

        if not workers or not tasks:
            db.session.commit()
            return ApiResponse.success(result, "No tasks dispatched").to_response()

        # Greedy best-score matching: each worker takes at most one task this round.
        scored = []
        for task in tasks:
            for worker in workers:
                match = (
                    score_task_for_agent(task, worker)
                    if match_capabilities
                    else {"score": 0, "matched_capabilities": [], "matched_tags": [], "matched_text": []}
                )
                scored.append((task, worker, match))

        scored.sort(key=lambda item: -item[2]["score"])

        used_tasks = set()
        used_agents = set()
        for task, worker, match in scored:
            if len(result["assignments"]) >= max_assignments:
                break
            if task.id in used_tasks or worker.id in used_agents:
                continue
            if require_capability_match and match["score"] <= 0:
                continue

            strategy = "capability_match" if (match_capabilities and match["score"] > 0) else "priority_fifo"
            run_metadata = {
                "claim_mode": "auto_dispatch",
                "dispatched_by_agent_id": coordinator.id,
                "capability_match": {**match, "strategy": strategy},
            }
            assignment, run = create_assignment_with_run(
                task, worker, current_user, now, lease_seconds, run_metadata
            )
            if task.status == TaskStatus.TODO:
                task.status = TaskStatus.IN_PROGRESS

            db.session.flush()

            record_task_event(
                task.id,
                "task_dispatched",
                agent=coordinator,
                payload={
                    "assignment_id": assignment.id,
                    "run_id": run.id,
                    "to_agent_id": worker.id,
                    "dispatched_by_agent_id": coordinator.id,
                    "lease_seconds": lease_seconds,
                    "strategy": strategy,
                    "score": match["score"],
                    "matched_capabilities": match["matched_capabilities"],
                    "content": f"Dispatched to {worker.name}",
                },
            )

            used_tasks.add(task.id)
            used_agents.add(worker.id)
            result["assignments"].append(
                {
                    "assignment": assignment.to_dict(include_task=True, include_agent=True),
                    "run": run.to_dict(),
                    "agent": worker.to_dict(include_stats=False),
                    "strategy": strategy,
                    "score": match["score"],
                    "matched_capabilities": match["matched_capabilities"],
                }
            )

        result["summary"]["dispatched"] = len(result["assignments"])
        result["summary"]["skipped_no_match"] = max(0, len(tasks) - len(used_tasks))

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            result,
            f"Dispatched {len(result['assignments'])} task(s)",
            201 if result["assignments"] else 200,
        ).to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to dispatch tasks: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/subtasks", methods=["POST"])
@unified_auth_required
def create_subtask(task_id):
    """Create a child task under a parent task (Agent-driven task decomposition).

    This is the multi-Agent decomposition primitive: an Agent can break a large
    task into smaller pieces that other Agents can claim independently. The
    parent-child relationship is recorded via ``parent_task_id`` and a
    ``subtask_created`` event is posted to the parent's timeline.
    """
    try:
        current_user = get_current_user()
        parent, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        data = validate_json_request(
            required_fields=["title"],
            optional_fields=["content", "priority", "tags", "agent_id"],
        )
        if isinstance(data, tuple):
            return data

        title = data["title"].strip()
        if not title or len(title) > 500:
            return ApiResponse.error("title must be 1-500 characters", 400).to_response()

        actor_agent = None
        if data.get("agent_id"):
            actor_agent, response = get_owned_agent_or_response(data["agent_id"], current_user)
            if response:
                return response

        subtask = Task(
            project_id=parent.project_id,
            owner_id=current_user.id,
            title=title,
            content=data.get("content"),
            priority=parse_enum(TaskPriority, data.get("priority", TaskPriority.MEDIUM.value), "priority"),
            tags=data.get("tags") if isinstance(data.get("tags"), list) else None,
            parent_task_id=parent.id,
            is_ai_task=True,
            creator_type="agent" if actor_agent else "human",
            creator_identifier=str(actor_agent.id) if actor_agent else current_user.email,
            created_by=current_user.email,
        )
        db.session.add(subtask)

        record_task_event(
            parent.id,
            "subtask_created",
            current_user=current_user if not actor_agent else None,
            agent=actor_agent,
            payload={
                "subtask_id": None,  # filled after flush
                "subtask_title": title,
                "content": f"Created subtask: {title}",
            },
        )

        db.session.flush()

        # Back-fill subtask_id
        parent_event = TaskEvent.query.filter_by(task_id=parent.id).order_by(TaskEvent.id.desc()).first()
        if parent_event and parent_event.payload.get("subtask_title") == title:
            p = dict(parent_event.payload)
            p["subtask_id"] = subtask.id
            parent_event.payload = p
            db.session.add(parent_event)

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            subtask.to_dict(include_project=True),
            "Subtask created successfully",
            201,
        ).to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create subtask: {str(e)}", 500).to_response()


INBOX_SCAN_WINDOW = 400


def serialize_inbox_event(event):
    data = event.to_dict()
    if event.task:
        data["task"] = {
            "id": event.task.id,
            "title": event.task.title,
            "status": event.task.status.value if event.task.status else None,
            "project_id": event.task.project_id,
        }
    return data


@agents_bp.route("/<int:agent_id>/inbox", methods=["GET"])
@unified_auth_required
def agent_inbox(agent_id):
    """Return collaboration events directed at a specific Agent (its @mention inbox).

    Other Agents (or the human owner) can address a message to an Agent by setting
    ``to_agent_id`` when posting a task event. This endpoint surfaces those directed
    messages across all of the owner's tasks so an Agent can poll "what was sent to
    me", supporting incremental ``since_id`` polling like the per-task timeline.
    """
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        args = get_request_args()
        per_page = min(args["per_page"], 100)
        since_id = request.args.get("since_id", type=int)
        include_read = request.args.get("include_self", "false").lower() == "true"

        base = (
            TaskEvent.query.join(Task, TaskEvent.task_id == Task.id)
            .join(Project, Task.project_id == Project.id)
            .filter(Project.owner_id == current_user.id)
        )

        def directed_to_agent(event):
            payload = event.payload or {}
            if payload.get("to_agent_id") != agent.id:
                return False
            # Skip the Agent's own outgoing messages unless explicitly requested.
            if not include_read and event.actor_agent_id == agent.id:
                return False
            return True

        if since_id:
            window = (
                base.filter(TaskEvent.id > since_id)
                .order_by(TaskEvent.id.asc())
                .limit(INBOX_SCAN_WINDOW)
                .all()
            )
            directed = [event for event in window if directed_to_agent(event)][:per_page]
            latest_id = directed[-1].id if directed else since_id
            return ApiResponse.success(
                {
                    "items": [serialize_inbox_event(event) for event in directed],
                    "latest_id": latest_id,
                    "since_id": since_id,
                    "agent_id": agent.id,
                },
                "Agent inbox retrieved successfully",
            ).to_response()

        window = base.order_by(TaskEvent.id.desc()).limit(INBOX_SCAN_WINDOW).all()
        directed = [event for event in window if directed_to_agent(event)][:per_page]
        return ApiResponse.success(
            {
                "items": [serialize_inbox_event(event) for event in directed],
                "agent_id": agent.id,
                "count": len(directed),
            },
            "Agent inbox retrieved successfully",
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve agent inbox: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Notifications — persistent unread events for the current user
# ---------------------------------------------------------------------------


@agents_bp.route("/notifications", methods=["GET"])
@unified_auth_required
def list_notifications():
    """Return the current user's notifications (newest first).

    Query params:
        since_id  – only return notifications with id > since_id (incremental)
        unread_only – "true" to filter to unread only (default false)
        per_page – page size (default 50, max 200)
    """
    try:
        current_user = get_current_user()
        args = get_request_args()
        per_page = min(args["per_page"], 200)
        since_id = request.args.get("since_id", type=int)
        unread_only = request.args.get("unread_only", "false").lower() == "true"

        query = Notification.query.filter_by(user_id=current_user.id)
        if since_id:
            query = query.filter(Notification.id > since_id)
        if unread_only:
            query = query.filter_by(is_read=False)

        query = query.order_by(Notification.id.desc())
        items = query.limit(per_page).all()

        unread_count = Notification.query.filter_by(
            user_id=current_user.id, is_read=False
        ).count()

        return ApiResponse.success(
            {
                "items": [n.to_dict() for n in items],
                "unread_count": unread_count,
                "since_id": since_id,
            },
            "Notifications retrieved",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve notifications: {str(e)}", 500).to_response()


@agents_bp.route("/notifications/read", methods=["POST"])
@unified_auth_required
def mark_notifications_read():
    """Mark one or more notifications as read.

    Body (JSON):
        ids: list of notification IDs to mark read
        all: boolean — if true, mark ALL unread notifications as read (ignores ids)
    """
    try:
        current_user = get_current_user()
        data = validate_json_request(
            optional_fields=["ids", "all"],
        )
        if isinstance(data, tuple):
            return data

        mark_all = data.get("all", False)
        ids = data.get("ids", [])

        if mark_all:
            count = Notification.query.filter_by(
                user_id=current_user.id, is_read=False
            ).update({"is_read": True, "read_at": datetime.utcnow()})
            db.session.commit()
            return ApiResponse.success(
                {"marked_count": count}, "All notifications marked as read"
            ).to_response()

        if not ids:
            return ApiResponse.error("Provide ids or all=true", 400).to_response()

        now = datetime.utcnow()
        count = (
            Notification.query.filter(
                Notification.id.in_(ids),
                Notification.user_id == current_user.id,
                Notification.is_read == False,
            )
            .update({"is_read": True, "read_at": now}, synchronize_session="fetch")
        )
        db.session.commit()
        return ApiResponse.success(
            {"marked_count": count}, "Notifications marked as read"
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to mark notifications: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Shared Context — key-value store for cross-Agent collaboration
# ---------------------------------------------------------------------------


def _task_owned_by_user(task_id, current_user):
    """Return the Task if it belongs to the user, else None."""
    return Task.query.join(Project).filter(
        Task.id == task_id,
        Project.owner_id == current_user.id,
    ).first()


@agents_bp.route("/tasks/<int:task_id>/shared-context", methods=["GET"])
@unified_auth_required
def list_shared_context(task_id):
    """List all shared context entries for a task.

    Query params:
        key – filter to a specific key (optional)
    """
    try:
        current_user = get_current_user()
        task = _task_owned_by_user(task_id, current_user)
        if not task:
            return ApiResponse.error("Task not found or access denied", 404).to_response()

        query = SharedContext.query.filter_by(task_id=task_id)
        key_filter = request.args.get("key")
        if key_filter:
            query = query.filter_by(key=key_filter)

        items = query.order_by(SharedContext.key.asc()).all()
        return ApiResponse.success(
            [item.to_dict() for item in items],
            "Shared context retrieved",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve shared context: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/shared-context", methods=["PUT"])
@unified_auth_required
def upsert_shared_context(task_id):
    """Create or update a shared context entry (upsert by task_id + key).

    Body:
        key   – context key (required, 1-255 chars)
        value – context value (required)
        agent_id – optional Agent ID authoring this entry
    """
    try:
        current_user = get_current_user()
        task = _task_owned_by_user(task_id, current_user)
        if not task:
            return ApiResponse.error("Task not found or access denied", 404).to_response()

        data = validate_json_request(
            required_fields=["key", "value"],
            optional_fields=["agent_id"],
        )
        if isinstance(data, tuple):
            return data

        key = data["key"].strip()
        if not key or len(key) > 255:
            return ApiResponse.error("key must be 1-255 characters", 400).to_response()

        # Validate agent_id if provided
        agent_id = data.get("agent_id")
        if agent_id:
            agent = Agent.query.filter_by(id=agent_id, owner_id=current_user.id).first()
            if not agent:
                return ApiResponse.error("Agent not found or not owned by you", 404).to_response()

        # Upsert: find existing entry with same task_id + key
        existing = SharedContext.query.filter_by(task_id=task_id, key=key).first()
        if existing:
            existing.value = data["value"]
            existing.author_agent_id = agent_id
            existing.author_user_id = current_user.id
            db.session.commit()
            return ApiResponse.success(existing.to_dict(), "Shared context updated").to_response()

        entry = SharedContext(
            task_id=task_id,
            key=key,
            value=data["value"],
            author_agent_id=agent_id,
            author_user_id=current_user.id,
        )
        db.session.add(entry)
        db.session.commit()
        return ApiResponse.created(entry.to_dict(), "Shared context created").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to upsert shared context: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/shared-context/<int:entry_id>", methods=["DELETE"])
@unified_auth_required
def delete_shared_context(task_id, entry_id):
    """Delete a shared context entry."""
    try:
        current_user = get_current_user()
        task = _task_owned_by_user(task_id, current_user)
        if not task:
            return ApiResponse.error("Task not found or access denied", 404).to_response()

        entry = SharedContext.query.filter_by(id=entry_id, task_id=task_id).first()
        if not entry:
            return ApiResponse.error("Shared context entry not found", 404).to_response()

        db.session.delete(entry)
        db.session.commit()
        return ApiResponse.success(None, "Shared context entry deleted").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete shared context: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Run Logs — append-only execution log for an Agent run
# ---------------------------------------------------------------------------

_RUN_LOG_LEVELS = {"debug", "info", "warn", "error"}
_RUN_LOG_MAX_PER_CALL = 50


@agents_bp.route("/runs/<int:run_id>/logs", methods=["GET"])
@unified_auth_required
def list_run_logs(run_id):
    """Return log entries for a specific AgentRun (oldest first).

    Query params:
        since_id  – only return entries with id > since_id (incremental)
        level     – filter by level (debug/info/warn/error)
        per_page  – page size (default 100, max 500)
    """
    try:
        current_user = get_current_user()

        # Verify the run belongs to the user
        run = AgentRun.query.get(run_id)
        if not run:
            return ApiResponse.error("Run not found", 404).to_response()
        task = Task.query.get(run.task_id)
        if not task or task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied", 403).to_response()

        args = get_request_args()
        per_page = min(args["per_page"], 500)
        since_id = request.args.get("since_id", type=int)
        level_filter = request.args.get("level")

        query = RunLog.query.filter_by(run_id=run_id)
        if since_id:
            query = query.filter(RunLog.id > since_id)
        if level_filter and level_filter in _RUN_LOG_LEVELS:
            query = query.filter_by(level=level_filter)

        query = query.order_by(RunLog.id.asc())
        items = query.limit(per_page).all()

        latest_id = items[-1].id if items else (since_id or 0)

        return ApiResponse.success(
            {
                "items": [item.to_dict() for item in items],
                "latest_id": latest_id,
                "since_id": since_id,
                "run_id": run_id,
            },
            "Run logs retrieved",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve run logs: {str(e)}", 500).to_response()


@agents_bp.route("/runs/<int:run_id>/logs", methods=["POST"])
@unified_auth_required
def append_run_logs(run_id):
    """Append log entries to a specific AgentRun.

    Body (JSON):
        entries: list of { level, message, meta? }
    """
    try:
        current_user = get_current_user()

        run = AgentRun.query.get(run_id)
        if not run:
            return ApiResponse.error("Run not found", 404).to_response()
        task = Task.query.get(run.task_id)
        if not task or task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied", 403).to_response()

        data = validate_json_request(required_fields=["entries"])
        if isinstance(data, tuple):
            return data

        entries = data["entries"]
        if not isinstance(entries, list) or len(entries) > _RUN_LOG_MAX_PER_CALL:
            return ApiResponse.error(
                f"entries must be a list of at most {_RUN_LOG_MAX_PER_CALL} items", 400
            ).to_response()

        created = []
        for entry in entries:
            level = entry.get("level", "info")
            if level not in _RUN_LOG_LEVELS:
                level = "info"
            msg = str(entry.get("message", ""))
            meta = entry.get("meta")

            log = RunLog(run_id=run_id, level=level, message=msg, meta=meta)
            db.session.add(log)
            created.append(log)

        db.session.commit()
        return ApiResponse.created(
            [item.to_dict() for item in created],
            f"Appended {len(created)} log entries",
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to append run logs: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Task Templates — reusable task blueprints
# ---------------------------------------------------------------------------


@agents_bp.route("/task-templates", methods=["GET"])
@unified_auth_required
def list_task_templates():
    """List the current user's task templates."""
    try:
        current_user = get_current_user()
        templates = TaskTemplate.query.filter_by(owner_id=current_user.id).order_by(TaskTemplate.name.asc()).all()
        return ApiResponse.success(
            [t.to_dict() for t in templates],
            "Task templates retrieved",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to list task templates: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates", methods=["POST"])
@unified_auth_required
def create_task_template():
    """Create a new task template.

    Body:
        name (required), description, title_template, content_template,
        priority, tags, is_ai_task, capabilities
    """
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=["name"],
            optional_fields=[
                "description", "title_template", "content_template",
                "priority", "tags", "is_ai_task", "capabilities",
            ],
        )
        if isinstance(data, tuple):
            return data

        template = TaskTemplate(
            owner_id=current_user.id,
            name=data["name"],
            description=data.get("description", ""),
            title_template=data.get("title_template", ""),
            content_template=data.get("content_template", ""),
            priority=data.get("priority", "medium"),
            tags=data.get("tags", []),
            is_ai_task=data.get("is_ai_task", False),
            capabilities=data.get("capabilities", []),
        )
        db.session.add(template)
        db.session.commit()
        return ApiResponse.created(template.to_dict(), "Task template created").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create task template: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates/<int:template_id>", methods=["PUT"])
@unified_auth_required
def update_task_template(template_id):
    """Update a task template."""
    try:
        current_user = get_current_user()
        template = TaskTemplate.query.filter_by(id=template_id, owner_id=current_user.id).first()
        if not template:
            return ApiResponse.error("Task template not found", 404).to_response()

        data = validate_json_request(
            optional_fields=[
                "name", "description", "title_template", "content_template",
                "priority", "tags", "is_ai_task", "capabilities",
            ],
        )
        if isinstance(data, tuple):
            return data

        for field in ["name", "description", "title_template", "content_template", "priority", "is_ai_task"]:
            if field in data:
                setattr(template, field, data[field])
        if "tags" in data:
            template.tags = data["tags"]
        if "capabilities" in data:
            template.capabilities = data["capabilities"]

        db.session.commit()
        return ApiResponse.success(template.to_dict(), "Task template updated").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update task template: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates/<int:template_id>", methods=["DELETE"])
@unified_auth_required
def delete_task_template(template_id):
    """Delete a task template."""
    try:
        current_user = get_current_user()
        template = TaskTemplate.query.filter_by(id=template_id, owner_id=current_user.id).first()
        if not template:
            return ApiResponse.error("Task template not found", 404).to_response()

        db.session.delete(template)
        db.session.commit()
        return ApiResponse.success(None, "Task template deleted").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete task template: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates/<int:template_id>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_task_template(template_id):
    """Create a new task from a template.

    Body:
        project_id (required) — the project to create the task in.
        title — override template title (optional)
        content — override template content (optional)
    """
    try:
        current_user = get_current_user()
        template = TaskTemplate.query.filter_by(id=template_id, owner_id=current_user.id).first()
        if not template:
            return ApiResponse.error("Task template not found", 404).to_response()

        data = validate_json_request(
            required_fields=["project_id"],
            optional_fields=["title", "content"],
        )
        if isinstance(data, tuple):
            return data

        from models import Task as TaskModel, TaskStatus as TS, TaskPriority as TP, TaskHistory, ActionType

        project = Project.query.get(data["project_id"])
        if not project or project.owner_id != current_user.id:
            return ApiResponse.error("Project not found or access denied", 404).to_response()

        title = data.get("title") or template.title_template or template.name
        content = data.get("content") or template.content_template or ""

        try:
            priority = TP(template.priority)
        except ValueError:
            priority = TP.MEDIUM

        task = TaskModel.create(
            project_id=project.id,
            title=title,
            content=content,
            status=TS.TODO,
            priority=priority,
            tags=template.tags or [],
            is_ai_task=template.is_ai_task,
            creator_id=current_user.id,
            created_by=current_user.email,
        )
        project.last_activity_at = datetime.utcnow()
        db.session.commit()

        TaskHistory.log_action(
            task_id=task.id,
            action=ActionType.CREATED,
            changed_by='api',
            comment=f'Task created from template "{template.name}"',
        )

        return ApiResponse.created(
            task.to_dict(include_project=True, include_stats=True),
            f'Task created from template "{template.name}"',
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to instantiate task template: {str(e)}", 500).to_response()


# =========================================================================
# Workflow API
# =========================================================================


def _workflow_owned_by_user(workflow_id, user):
    """Return the workflow if it belongs to *user*, else None."""
    return Workflow.query.filter_by(id=workflow_id, owner_id=user.id).first()


# --- Workflow CRUD --------------------------------------------------------


@agents_bp.route("/workflows", methods=["GET"])
@unified_auth_required
def list_workflows():
    """List workflow definitions owned by the current user."""
    user = get_current_user()
    args = get_request_args()
    is_active = args.get("is_active", type=bool)
    query = Workflow.query.filter_by(owner_id=user.id)
    if is_active is not None:
        query = query.filter_by(is_active=is_active)
    query = query.order_by(Workflow.updated_at.desc())
    result = paginate_query(query, args)
    items = [w.to_dict(include_steps=True) for w in result["items"]]
    return ApiResponse.paginated(items, result["pagination"]).to_response()


@agents_bp.route("/workflows", methods=["POST"])
@unified_auth_required
def create_workflow():
    """Create a new workflow definition with steps."""
    user = get_current_user()
    data = validate_json_request()
    name = (data.get("name") or "").strip()
    if not name:
        return ApiResponse.error("name is required", 400).to_response()

    workflow = Workflow.create(
        owner_id=user.id,
        name=name,
        description=data.get("description", ""),
        definition=data.get("definition", {}),
        is_active=data.get("is_active", True),
        max_parallel_steps=data.get("max_parallel_steps", 0),
    )

    # Create steps if provided
    for step_data in data.get("steps", []):
        step_key = (step_data.get("step_key") or "").strip()
        if not step_key:
            continue
        WorkflowStep.create(
            workflow_id=workflow.id,
            step_key=step_key,
            name=step_data.get("name", step_key),
            description=step_data.get("description", ""),
            order=step_data.get("order", 0),
            required_capabilities=step_data.get("required_capabilities", []),
            agent_id=step_data.get("agent_id"),
            task_template_id=step_data.get("task_template_id"),
            depends_on=step_data.get("depends_on", []),
            condition=step_data.get("condition"),
            sub_workflow_id=step_data.get("sub_workflow_id"),
            timeout_seconds=step_data.get("timeout_seconds"),
            retry_count=step_data.get("retry_count", 0),
            on_failure=step_data.get("on_failure", "abort"),
        )

    db.session.commit()
    return ApiResponse.created(
        workflow.to_dict(include_steps=True), "Workflow created"
    ).to_response()


@agents_bp.route("/workflows/<int:workflow_id>", methods=["GET"])
@unified_auth_required
def get_workflow(workflow_id):
    """Get a single workflow definition with steps."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()
    return ApiResponse.success(workflow.to_dict(include_steps=True)).to_response()


@agents_bp.route("/workflows/<int:workflow_id>", methods=["PUT"])
@unified_auth_required
def update_workflow(workflow_id):
    """Update a workflow definition and its steps (full replacement).

    Automatically creates a version snapshot before applying changes,
    so running instances are not affected.
    """
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    # Snapshot the current version before modifying
    old_version = workflow.version or 1
    WorkflowVersion.create(
        workflow_id=workflow.id,
        version_number=old_version,
        definition=workflow.definition or {},
        steps_snapshot=[s.to_dict() for s in workflow.steps],
        change_summary=f"Auto-snapshot before update (v{old_version} → v{old_version + 1})",
        created_by=user.email,
    )

    # Bump version
    workflow.version = old_version + 1

    data = validate_json_request()
    if "name" in data:
        workflow.name = data["name"]
    if "description" in data:
        workflow.description = data["description"]
    if "definition" in data:
        workflow.definition = data["definition"]
    if "is_active" in data:
        workflow.is_active = data["is_active"]
    if "max_parallel_steps" in data:
        workflow.max_parallel_steps = data["max_parallel_steps"]

    # Replace steps if provided
    if "steps" in data:
        # Delete existing steps
        for old_step in workflow.steps:
            db.session.delete(old_step)
        # Create new steps
        for step_data in data["steps"]:
            step_key = (step_data.get("step_key") or "").strip()
            if not step_key:
                continue
            WorkflowStep.create(
                workflow_id=workflow.id,
                step_key=step_key,
                name=step_data.get("name", step_key),
                description=step_data.get("description", ""),
                order=step_data.get("order", 0),
                required_capabilities=step_data.get("required_capabilities", []),
                agent_id=step_data.get("agent_id"),
                task_template_id=step_data.get("task_template_id"),
                depends_on=step_data.get("depends_on", []),
                condition=step_data.get("condition"),
            sub_workflow_id=step_data.get("sub_workflow_id"),
                timeout_seconds=step_data.get("timeout_seconds"),
                retry_count=step_data.get("retry_count", 0),
                on_failure=step_data.get("on_failure", "abort"),
            )

    # Also update the snapshot of the new version
    # (so we have a complete version history: v1 → v2 → ...)

    db.session.commit()
    return ApiResponse.success(
        workflow.to_dict(include_steps=True), "Workflow updated"
    ).to_response()


@agents_bp.route("/workflows/<int:workflow_id>", methods=["DELETE"])
@unified_auth_required
def delete_workflow(workflow_id):
    """Delete a workflow definition."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()
    db.session.delete(workflow)
    db.session.commit()
    return ApiResponse.success(None, "Workflow deleted").to_response()


# --- Workflow Runs --------------------------------------------------------


@agents_bp.route("/workflows/<int:workflow_id>/runs", methods=["POST"])
@unified_auth_required
def launch_workflow(workflow_id):
    """Launch a new run of a workflow.

    Creates a WorkflowRun, resolves the DAG, and starts all steps whose
    depends_on list is empty (i.e. entry-point steps).
    """
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()
    if not workflow.is_active:
        return ApiResponse.error("Workflow is not active", 400).to_response()

    data = validate_json_request()
    project_id = data.get("project_id")
    if not project_id:
        return ApiResponse.error("project_id is required", 400).to_response()
    project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
    if not project:
        return ApiResponse.error("Project not found", 404).to_response()

    root_task_id = data.get("root_task_id")

    # Create the run
    wf_run = WorkflowRun.create(
        workflow_id=workflow.id,
        root_task_id=root_task_id,
        project_id=project_id,
        owner_id=user.id,
        status=WorkflowStatus.PENDING,
        context=data.get("context", {}),
    )

    # Create step runs for every step in the definition
    steps = WorkflowStep.query.filter_by(workflow_id=workflow.id).order_by(WorkflowStep.order).all()
    for step in steps:
        sr = WorkflowStepRun.create(
            run_id=wf_run.id,
            step_key=step.step_key,
            status=StepStatus.PENDING,
            attempt=1,
        )

    db.session.commit()

    # Now kick off entry-point steps (those with no dependencies)
    _advance_workflow(wf_run)

    db.session.commit()
    flush_sse_notifications()

    AuditLog.record(
        action="workflow.launched", resource_type="workflow_run", resource_id=wf_run.id,
        actor_type="human", actor_user_id=user.id,
        project_id=wf_run.project_id,
        detail={"workflow_id": workflow.id, "workflow_name": workflow.name},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.created(
        wf_run.to_dict(include_step_runs=True), "Workflow launched"
    ).to_response()


@agents_bp.route("/workflow-runs", methods=["GET"])
@unified_auth_required
def list_workflow_runs():
    """List workflow runs for the current user."""
    user = get_current_user()
    args = get_request_args()
    query = WorkflowRun.query.filter_by(owner_id=user.id)
    if args.get("workflow_id", type=int):
        query = query.filter_by(workflow_id=args.get("workflow_id", type=int))
    if args.get("status"):
        try:
            query = query.filter_by(status=WorkflowStatus(args.get("status")))
        except ValueError:
            pass
    query = query.order_by(WorkflowRun.created_at.desc())
    result = paginate_query(query, args)
    items = [r.to_dict(include_step_runs=True) for r in result["items"]]
    return ApiResponse.paginated(items, result["pagination"]).to_response()


@agents_bp.route("/workflows/step-stats", methods=["GET"])
@unified_auth_required
def workflow_step_stats():
    """Per-step-key execution stats across the current user's workflow runs.

    For each ``step_key``: total runs, succeeded, failed, skipped, success
    rate, and average duration (finished_at - started_at, in seconds) for
    completed steps. Reveals which steps are bottlenecks or chronic failure
    points across all workflows the user has launched.
    """
    user = get_current_user()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 30))))
    except (TypeError, ValueError):
        limit = 30

    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(WorkflowRun.owner_id == user.id)
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.status,
            WorkflowStepRun.started_at,
            WorkflowStepRun.finished_at,
            WorkflowStepRun.attempt,
        )
        .all()
    )
    agg: dict = {}
    for step_key, status, started, finished, attempt in rows:
        entry = agg.setdefault(step_key, {
            "step_key": step_key, "total": 0, "succeeded": 0,
            "failed": 0, "skipped": 0, "durations": [], "retry_count": 0,
        })
        entry["total"] += 1
        # attempt defaults to 1 on first try; >1 means a retry happened
        if attempt and attempt > 1:
            entry["retry_count"] += attempt - 1
        if status == StepStatus.SUCCEEDED:
            entry["succeeded"] += 1
        elif status == StepStatus.FAILED:
            entry["failed"] += 1
        elif status == StepStatus.SKIPPED:
            entry["skipped"] += 1
        if started and finished and finished > started:
            entry["durations"].append((finished - started).total_seconds())

    items = []
    for step_key, e in agg.items():
        durations = e["durations"]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else None
        denom = e["total"] - e["skipped"] or 1
        items.append({
            "step_key": step_key,
            "total": e["total"],
            "succeeded": e["succeeded"],
            "failed": e["failed"],
            "skipped": e["skipped"],
            "success_rate": round(e["succeeded"] / denom, 3),
            "avg_duration_seconds": avg_dur,
            "sample_size_duration": len(durations),
            "retries": e["retry_count"],
            "avg_retries": round(e["retry_count"] / denom, 2),
        })
    items.sort(key=lambda x: x["total"], reverse=True)
    return ApiResponse.success({"items": items[:limit]}).to_response()


@agents_bp.route("/workflows/failure-correlation", methods=["GET"])
@unified_auth_required
def workflow_failure_correlation():
    """Cross-dimension correlation between failed workflow steps and
    collaboration conflicts / sandbox violations.

    For every failed step (status=failed) within the window, checks whether a
    conflict (AgentConflict) or sandbox violation (SandboxViolation) involving
    the same Agent occurred within ±window_hours of the step's finished_at.
    Reports totals and co-occurrence rates, plus the top agents whose failures
    most often coincide with conflicts/violations. Reveals whether failures
    cluster with coordination breakdowns or sandbox escapes.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        window_hours = max(0, min(168, int(request.args.get("window_hours", 2))))
    except (TypeError, ValueError):
        days = 30
        window_hours = 2

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]

    failed_steps = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.agent_id.in_(agent_ids) if agent_ids else sa_false(),
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(
            WorkflowStepRun.id, WorkflowStepRun.step_key, WorkflowStepRun.agent_id,
            WorkflowStepRun.finished_at, WorkflowStepRun.task_id, WorkflowStepRun.run_id,
        )
        .all()
    )

    total_failed = len(failed_steps)
    if total_failed == 0:
        return ApiResponse.success({
            "days": days,
            "window_hours": window_hours,
            "total_failed_steps": 0,
            "with_conflict": 0,
            "with_violation": 0,
            "with_both": 0,
            "conflict_rate": 0,
            "violation_rate": 0,
            "both_rate": 0,
            "top_agents": [],
        }).to_response()

    # Pre-fetch conflicts and violations in the window for these agents
    conflicts = (
        AgentConflict.query
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.created_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(AgentConflict.created_at, AgentConflict.agent_ids)
        .all()
    ) if agent_ids else []
    violations = (
        SandboxViolation.query
        .filter(
            SandboxViolation.agent_id.in_(agent_ids),
            SandboxViolation.blocked_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(SandboxViolation.agent_id, SandboxViolation.blocked_at)
        .all()
    ) if agent_ids else []

    def _near(times, target, agent_id, hours):
        lo = target - timedelta(hours=hours)
        hi = target + timedelta(hours=hours)
        return any(lo <= t <= hi for t in times)

    # Index violations by agent for speed
    violations_by_agent: dict = {}
    for aid, blocked_at in violations:
        violations_by_agent.setdefault(aid, []).append(blocked_at)

    per_agent = {}  # agent_id -> {failed, conflict, violation}
    with_conflict = 0
    with_violation = 0
    with_both = 0
    for _id, step_key, aid, finished_at, task_id, run_id in failed_steps:
        aid_int = aid
        v_times = violations_by_agent.get(aid_int, [])
        has_v = _near(v_times, finished_at, aid_int, window_hours) if v_times else False
        # conflicts store agent_ids list; check membership + time
        has_c = False
        for created_at, agent_ids_json in conflicts:
            if agent_ids_json and aid_int in (agent_ids_json or []):
                if abs((created_at - finished_at).total_seconds()) <= window_hours * 3600:
                    has_c = True
                    break
        if has_c:
            with_conflict += 1
        if has_v:
            with_violation += 1
        if has_c and has_v:
            with_both += 1
        bucket = per_agent.setdefault(aid_int, {"failed": 0, "conflict": 0, "violation": 0, "agent_id": aid_int})
        bucket["failed"] += 1
        if has_c:
            bucket["conflict"] += 1
        if has_v:
            bucket["violation"] += 1

    # Enrich top agents with name
    top_agent_ids = sorted(per_agent.keys(), key=lambda k: per_agent[k]["conflict"] + per_agent[k]["violation"], reverse=True)[:8]
    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(top_agent_ids)).with_entities(Agent.id, Agent.name).all()} if top_agent_ids else {}
    top_agents = []
    for aid in top_agent_ids:
        b = per_agent[aid]
        top_agents.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "failed_steps": b["failed"],
            "with_conflict": b["conflict"],
            "with_violation": b["violation"],
        })

    return ApiResponse.success({
        "days": days,
        "window_hours": window_hours,
        "total_failed_steps": total_failed,
        "with_conflict": with_conflict,
        "with_violation": with_violation,
        "with_both": with_both,
        "conflict_rate": round(with_conflict / total_failed * 100, 1),
        "violation_rate": round(with_violation / total_failed * 100, 1),
        "both_rate": round(with_both / total_failed * 100, 1),
        "top_agents": top_agents,
    }).to_response()


@agents_bp.route("/workflows/failure-correlation-by-step", methods=["GET"])
@unified_auth_required
def workflow_failure_correlation_by_step():
    """Per-step-key failure correlation with conflicts / sandbox violations.

    Like ``workflow_failure_correlation`` but aggregated by ``step_key``:
    for each step key, how many of its failures coincided (±window_hours,
    same Agent) with a conflict or sandbox violation. Reveals which steps
    are most prone to triggering coordination breakdowns or sandbox escapes.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        window_hours = max(0, min(168, int(request.args.get("window_hours", 2))))
    except (TypeError, ValueError):
        days = 30
        window_hours = 2

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]

    failed_steps = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.agent_id.in_(agent_ids) if agent_ids else sa_false(),
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(
            WorkflowStepRun.step_key, WorkflowStepRun.agent_id,
            WorkflowStepRun.finished_at,
        )
        .all()
    )

    if not failed_steps:
        return ApiResponse.success({
            "days": days,
            "window_hours": window_hours,
            "items": [],
            "step_conflict_type_matrix": {},
        }).to_response()

    conflicts = (
        AgentConflict.query
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.created_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(AgentConflict.created_at, AgentConflict.agent_ids, AgentConflict.conflict_type)
        .all()
    ) if agent_ids else []
    violations = (
        SandboxViolation.query
        .filter(
            SandboxViolation.agent_id.in_(agent_ids),
            SandboxViolation.blocked_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(SandboxViolation.agent_id, SandboxViolation.blocked_at)
        .all()
    ) if agent_ids else []

    violations_by_agent: dict = {}
    for aid, blocked_at in violations:
        violations_by_agent.setdefault(aid, []).append(blocked_at)

    per_step: dict = {}
    for step_key, aid, finished_at in failed_steps:
        aid_int = aid
        v_times = violations_by_agent.get(aid_int, [])
        has_v = any(abs((t - finished_at).total_seconds()) <= window_hours * 3600 for t in v_times) if v_times else False
        has_c = False
        matched_conflict_types: set = set()
        for created_at, agent_ids_json, ctype in conflicts:
            if agent_ids_json and aid_int in (agent_ids_json or []):
                if abs((created_at - finished_at).total_seconds()) <= window_hours * 3600:
                    has_c = True
                    if ctype is not None:
                        matched_conflict_types.add(ctype.value if hasattr(ctype, 'value') else str(ctype))
        bucket = per_step.setdefault(step_key, {"step_key": step_key, "failed": 0, "with_conflict": 0, "with_violation": 0, "conflict_types": {}})
        bucket["failed"] += 1
        if has_c:
            bucket["with_conflict"] += 1
            for ct in matched_conflict_types:
                bucket["conflict_types"][ct] = bucket["conflict_types"].get(ct, 0) + 1
        if has_v:
            bucket["with_violation"] += 1

    items = []
    for b in per_step.values():
        f = b["failed"]
        items.append({
            "step_key": b["step_key"],
            "failed": f,
            "with_conflict": b["with_conflict"],
            "with_violation": b["with_violation"],
            "conflict_rate": round(b["with_conflict"] / f * 100, 1) if f else 0,
            "violation_rate": round(b["with_violation"] / f * 100, 1) if f else 0,
            "conflict_types": b.get("conflict_types", {}),
        })
    items.sort(key=lambda x: (x["with_conflict"] + x["with_violation"], x["failed"]), reverse=True)

    # 步骤 × 冲突类型矩阵：{step_key: {conflict_type: count}}
    step_conflict_type_matrix: dict = {}
    for it in items:
        for ct, c in (it.get("conflict_types") or {}).items():
            step_conflict_type_matrix.setdefault(it["step_key"], {})[ct] = c

    return ApiResponse.success({
        "days": days,
        "window_hours": window_hours,
        "items": items[:30],
        "step_conflict_type_matrix": step_conflict_type_matrix,
    }).to_response()


@agents_bp.route("/conflicts/sandbox-correlation", methods=["GET"])
@unified_auth_required
def conflicts_sandbox_correlation():
    """Cross-dimension correlation between Agent conflicts and sandbox
    violations.

    For each conflict (AgentConflict, created_at within window), checks
    whether a sandbox violation (SandboxViolation, blocked_at within
    ±window_hours, same Agent among conflict parties) occurred. Reports
    co-occurrence rate, breakdown by conflict_type, and the top agents
    whose conflicts most often coincide with sandbox violations. Reveals
    whether coordination breakdowns cluster with sandbox escape attempts.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        window_hours = max(0, min(168, int(request.args.get("window_hours", 2))))
    except (TypeError, ValueError):
        days = 30
        window_hours = 2

    since = datetime.utcnow() - timedelta(days=days)
    conflicts = (
        AgentConflict.query
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.created_at >= since,
        )
        .with_entities(
            AgentConflict.id, AgentConflict.conflict_type,
            AgentConflict.created_at, AgentConflict.agent_ids,
        )
        .all()
    )

    total_conflicts = len(conflicts)
    if total_conflicts == 0:
        return ApiResponse.success({
            "days": days,
            "window_hours": window_hours,
            "total_conflicts": 0,
            "with_violation": 0,
            "violation_rate": 0,
            "by_conflict_type": {},
            "top_agents": [],
        }).to_response()

    # Collect all agent ids involved across conflicts for violation prefetch
    involved_ids = set()
    for _id, ctype, created_at, agent_ids_json in conflicts:
        if agent_ids_json:
            for aid in agent_ids_json:
                involved_ids.add(aid)

    violations = (
        SandboxViolation.query
        .filter(
            SandboxViolation.agent_id.in_(list(involved_ids)),
            SandboxViolation.blocked_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(SandboxViolation.agent_id, SandboxViolation.blocked_at)
        .all()
    ) if involved_ids else []
    violations_by_agent: dict = {}
    for aid, blocked_at in violations:
        violations_by_agent.setdefault(aid, []).append(blocked_at)

    with_violation = 0
    by_type_total: dict = {}
    by_type_with_violation: dict = {}
    per_agent: dict = {}
    for _id, ctype, created_at, agent_ids_json in conflicts:
        ct = ctype.value if ctype else "(未知)"
        by_type_total[ct] = by_type_total.get(ct, 0) + 1
        has_v = False
        for aid in (agent_ids_json or []):
            v_times = violations_by_agent.get(aid, [])
            if v_times and any(abs((t - created_at).total_seconds()) <= window_hours * 3600 for t in v_times):
                has_v = True
                break
        if has_v:
            with_violation += 1
            by_type_with_violation[ct] = by_type_with_violation.get(ct, 0) + 1
            for aid in (agent_ids_json or []):
                b = per_agent.setdefault(aid, {"agent_id": aid, "conflicts": 0, "with_violation": 0})
                b["conflicts"] += 1
                b["with_violation"] += 1
        else:
            for aid in (agent_ids_json or []):
                b = per_agent.setdefault(aid, {"agent_id": aid, "conflicts": 0, "with_violation": 0})
                b["conflicts"] += 1

    by_conflict_type = {
        ct: {
            "total": by_type_total.get(ct, 0),
            "with_violation": by_type_with_violation.get(ct, 0),
            "rate": round(by_type_with_violation.get(ct, 0) / by_type_total.get(ct, 0) * 100, 1) if by_type_total.get(ct, 0) else 0,
        }
        for ct in by_type_total
    }

    top_ids = sorted(per_agent.keys(), key=lambda k: per_agent[k]["with_violation"], reverse=True)[:8]
    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(top_ids)).with_entities(Agent.id, Agent.name).all()} if top_ids else {}
    top_agents = [
        {
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "conflicts": per_agent[aid]["conflicts"],
            "with_violation": per_agent[aid]["with_violation"],
        }
        for aid in top_ids
    ]

    return ApiResponse.success({
        "days": days,
        "window_hours": window_hours,
        "total_conflicts": total_conflicts,
        "with_violation": with_violation,
        "violation_rate": round(with_violation / total_conflicts * 100, 1),
        "by_conflict_type": by_conflict_type,
        "top_agents": top_agents,
    }).to_response()


_SUB_SCORE_LABELS = {
    "reputation": "声誉",
    "completion": "完成率",
    "conflict": "冲突控制",
    "violation": "沙盒合规",
}


def _compute_agent_health(user, days, weights=None, with_recommendations=False):
    """Shared computation for agent composite health (used by /health and
    /health/alerts). Returns (days, items) sorted by health_score desc.

    ``weights`` optionally overrides the default sub-score weights
    {reputation: 0.4, completion: 0.3, conflict: 0.15, violation: 0.15};
    they are normalised to sum to 1.0. When ``with_recommendations`` is
    True, each item carries a ``recommendations`` list of concrete
    improvement suggestions derived from its weakest sub-scores."""
    w = {"reputation": 0.4, "completion": 0.3, "conflict": 0.15, "violation": 0.15}
    if weights:
        for k in w:
            try:
                v = float(weights.get(k, w[k]))
            except (TypeError, ValueError):
                v = w[k]
            w[k] = max(0.0, v)
    total_w = sum(w.values())
    if total_w <= 0:
        w = {"reputation": 0.4, "completion": 0.3, "conflict": 0.15, "violation": 0.15}
        total_w = sum(w.values())
    w = {k: v / total_w for k, v in w.items()}

    since = datetime.utcnow() - timedelta(days=days)
    agents = Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id, Agent.name, Agent.status).all()
    if not agents:
        return days, []

    agent_ids = [a.id for a in agents]

    reps = {r.agent_id: r for r in AgentReputation.query.filter(AgentReputation.agent_id.in_(agent_ids)).all()}
    assign_rows = (
        TaskAssignment.query
        .filter(TaskAssignment.agent_id.in_(agent_ids), TaskAssignment.created_at >= since)
        .with_entities(TaskAssignment.agent_id, TaskAssignment.state)
        .all()
    )
    prod: dict = {}
    for aid, state in assign_rows:
        b = prod.setdefault(aid, {"total": 0, "done": 0})
        b["total"] += 1
        if state and state.value == "done":
            b["done"] += 1

    conflict_rows = (
        AgentConflict.query
        .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
        .with_entities(AgentConflict.agent_ids)
        .all()
    )
    conflict_counts: dict = {}
    for (agent_ids_json,) in conflict_rows:
        for aid in (agent_ids_json or []):
            conflict_counts[aid] = conflict_counts.get(aid, 0) + 1

    violation_rows = (
        SandboxViolation.query
        .filter(SandboxViolation.agent_id.in_(agent_ids), SandboxViolation.blocked_at >= since)
        .with_entities(SandboxViolation.agent_id, func.count(SandboxViolation.id))
        .group_by(SandboxViolation.agent_id)
        .all()
    )
    violation_counts = {aid: c for aid, c in violation_rows}

    max_conflicts = max(conflict_counts.values(), default=1)
    max_violations = max(violation_counts.values(), default=1)

    items = []
    for a in agents:
        rep = reps.get(a.id)
        rep_score = rep.score if rep and rep.score is not None else 50.0
        p = prod.get(a.id, {"total": 0, "done": 0})
        completion_rate = (p["done"] / p["total"] * 100) if p["total"] > 0 else None
        completion_score = completion_rate if completion_rate is not None else 50.0
        cc = conflict_counts.get(a.id, 0)
        vc = violation_counts.get(a.id, 0)
        conflict_score = 100 * (1 - cc / max_conflicts) if max_conflicts > 0 else 100.0
        violation_score = 100 * (1 - vc / max_violations) if max_violations > 0 else 100.0

        health = round(
            rep_score * w["reputation"] + completion_score * w["completion"]
            + conflict_score * w["conflict"] + violation_score * w["violation"],
            1,
        )
        sub_scores = {
            "reputation": round(rep_score, 1),
            "completion": round(completion_score, 1),
            "conflict": round(conflict_score, 1),
            "violation": round(violation_score, 1),
        }
        item = {
            "agent_id": a.id,
            "name": a.name,
            "status": a.status.value if a.status else None,
            "health_score": health,
            "reputation_score": round(rep_score, 1),
            "completion_rate": round(completion_rate, 1) if completion_rate is not None else None,
            "total_assignments": p["total"],
            "done_assignments": p["done"],
            "conflicts": cc,
            "sandbox_violations": vc,
            "sub_scores": sub_scores,
        }
        if with_recommendations:
            recs = []
            if rep_score < 50:
                recs.append("声誉分偏低，建议复盘近期失败任务并补充正向反馈以恢复信任")
            if completion_rate is not None and completion_rate < 50:
                recs.append("完成率偏低，建议核减负载或拆解复杂任务后再分配")
            elif p["total"] == 0:
                recs.append("近期无任务分配，建议主动领取任务以建立产出记录")
            if cc > 0:
                recs.append(f"近期发生 {cc} 次协作冲突，建议复核协作边界与消息协议")
            if vc > 0:
                recs.append(f"近期发生 {vc} 次沙盒违规，建议收紧工具权限并复查沙盒策略")
            # 按子分数升序追加最弱维度提示
            weakest = sorted(sub_scores.items(), key=lambda x: x[1])[:1]
            for name, score in weakest:
                if not recs:
                    recs.append(f"当前最弱维度为「{_SUB_SCORE_LABELS.get(name, name)}」({score})，建议针对性改进")
            item["recommendations"] = recs
        items.append(item)
    items.sort(key=lambda x: x["health_score"], reverse=True)
    return days, items


@agents_bp.route("/health", methods=["GET"])
@unified_auth_required
def agent_health():
    """Per-Agent composite health score for the current user.

    Combines multiple dimensions into a single 0-100 health score per Agent:
      - reputation score (weight 0.4)
      - assignment completion rate (weight 0.3)
      - conflict penalty (weight 0.15): fewer recent conflicts is better
      - sandbox violation penalty (weight 0.15): fewer recent violations is better

    Also returns the raw sub-scores so callers can see what drags health down.
    Optional ``w_reputation`` / ``w_completion`` / ``w_conflict`` /
    ``w_violation`` query params override the default sub-score weights
    (normalised to sum to 1). Reveals a single comparable metric across all
    of a user's Agents.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    weights = {
        "reputation": request.args.get("w_reputation"),
        "completion": request.args.get("w_completion"),
        "conflict": request.args.get("w_conflict"),
        "violation": request.args.get("w_violation"),
    }
    days, items = _compute_agent_health(user, days, weights=weights)
    return ApiResponse.success({"days": days, "items": items}).to_response()


@agents_bp.route("/health/alerts", methods=["GET"])
@unified_auth_required
def agent_health_alerts():
    """Low-health Agent alert list for the current user.

    Returns Agents whose composite health_score falls below
    ``min_health_score`` (default 60), with triggering reasons (low
    reputation / low completion / conflicts / violations) and concrete
    ``recommendations``. Optional weight overrides (``w_reputation`` /
    ``w_completion`` / ``w_conflict`` / ``w_violation``) re-weight the
    composite score. Each entry includes the full health fields. Surfaces
    Agents needing attention.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        min_health_score = max(0, min(100, float(request.args.get("min_health_score", 60))))
    except (TypeError, ValueError):
        days = 30
        min_health_score = 60
    weights = {
        "reputation": request.args.get("w_reputation"),
        "completion": request.args.get("w_completion"),
        "conflict": request.args.get("w_conflict"),
        "violation": request.args.get("w_violation"),
    }
    _, items = _compute_agent_health(user, days, weights=weights, with_recommendations=True)
    alerts = []
    for a in items:
        if a["health_score"] >= min_health_score:
            continue
        reasons = []
        if a["sub_scores"]["reputation"] < 50:
            reasons.append(f"声誉 {a['sub_scores']['reputation']} 偏低")
        if a["completion_rate"] is not None and a["completion_rate"] < 50:
            reasons.append(f"完成率 {a['completion_rate']}% 偏低")
        if a["conflicts"] > 0:
            reasons.append(f"冲突 {a['conflicts']} 次")
        if a["sandbox_violations"] > 0:
            reasons.append(f"违规 {a['sandbox_violations']} 次")
        a_copy = dict(a)
        a_copy["reasons"] = reasons
        alerts.append(a_copy)

    return ApiResponse.success({
        "days": days,
        "min_health_score": min_health_score,
        "items": alerts,
    }).to_response()


@agents_bp.route("/health/trend", methods=["GET"])
@unified_auth_required
def agent_health_trend():
    """Daily reputation-derived health trend for the current user's Agents.

    Aggregates ``reputation.update`` audit entries (which carry ``new_score``
    and ``score_delta`` in detail) by day across all of the user's Agents.
    Per-day: average new_score (last-seen per agent that day), count of
    positive deltas, count of negative deltas. A proxy for whether the
    fleet's health is rising or falling over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        agent_id = int(request.args.get("agent_id")) if request.args.get("agent_id") else None
    except (TypeError, ValueError):
        days = 30
        agent_id = None

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if agent_id is not None and agent_id not in agent_ids:
        return ApiResponse.success({"days": days, "trend": [], "total_positive": 0, "total_negative": 0, "agent_id": agent_id, "agent_name": None}).to_response()
    if agent_id is not None:
        agent_ids = [agent_id]
    if not agent_ids:
        return ApiResponse.success({"days": days, "trend": [], "total_positive": 0, "total_negative": 0}).to_response()

    selected_name = None
    if agent_id is not None:
        selected_name = Agent.query.filter_by(id=agent_id).with_entities(Agent.name).first()
        selected_name = selected_name[0] if selected_name else None

    rows = (
        AuditLog.query
        .filter(
            AuditLog.action == "reputation.update",
            AuditLog.resource_type == "agent",
            AuditLog.resource_id.in_(agent_ids),
            AuditLog.created_at >= since,
        )
        .with_entities(
            func.date(AuditLog.created_at).label("d"),
            AuditLog.resource_id,
            AuditLog.detail,
        )
        .all()
    )

    # per (day, agent) keep last new_score; track pos/neg deltas
    last_score_by_day_agent: dict = {}
    pos_by_day: dict = {}
    neg_by_day: dict = {}
    for d, aid, detail in rows:
        if not d:
            continue
        key = (str(d), aid)
        det = detail or {}
        new_score = det.get("new_score")
        delta = det.get("score_delta")
        if new_score is not None:
            last_score_by_day_agent[key] = new_score
        if delta is not None:
            try:
                dval = float(delta)
                if dval > 0:
                    pos_by_day[str(d)] = pos_by_day.get(str(d), 0) + 1
                elif dval < 0:
                    neg_by_day[str(d)] = neg_by_day.get(str(d), 0) + 1
            except (TypeError, ValueError):
                pass

    # 按日聚合平均 new_score
    day_scores: dict = {}
    for (day, _aid), score in last_score_by_day_agent.items():
        day_scores.setdefault(day, []).append(score)

    # 按日附加冲突事件计数（owner_id 命中当前用户；单 Agent 时进一步按参与方过滤）
    if agent_id is not None:
        conflict_rows_raw = (
            AgentConflict.query
            .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
            .with_entities(func.date(AgentConflict.created_at).label("d"), AgentConflict.agent_ids)
            .all()
        )
        conflict_by_day: dict = {}
        for d, agent_ids_json in conflict_rows_raw:
            if not d:
                continue
            if agent_ids_json and agent_id in (agent_ids_json or []):
                conflict_by_day[str(d)] = conflict_by_day.get(str(d), 0) + 1
    else:
        conflict_rows = (
            AgentConflict.query
            .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
            .with_entities(func.date(AgentConflict.created_at).label("d"), func.count(AgentConflict.id))
            .group_by("d")
            .all()
        )
        conflict_by_day = {str(d): c for d, c in conflict_rows if d}

    # 按日附加沙盒违规事件计数
    violation_rows = (
        SandboxViolation.query
        .filter(SandboxViolation.agent_id.in_(agent_ids), SandboxViolation.blocked_at >= since)
        .with_entities(func.date(SandboxViolation.blocked_at).label("d"), func.count(SandboxViolation.id))
        .group_by("d")
        .all()
    )
    violation_by_day = {str(d): c for d, c in violation_rows if d}

    trend = []
    for day in sorted(day_scores.keys()):
        scores = day_scores[day]
        avg = round(sum(scores) / len(scores), 2) if scores else None
        trend.append({
            "date": day,
            "avg_reputation": avg,
            "positive": pos_by_day.get(day, 0),
            "negative": neg_by_day.get(day, 0),
            "conflicts": conflict_by_day.get(day, 0),
            "sandbox_violations": violation_by_day.get(day, 0),
        })

    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_positive": sum(pos_by_day.values()),
        "total_negative": sum(neg_by_day.values()),
        "total_conflicts": sum(conflict_by_day.values()),
        "total_violations": sum(violation_by_day.values()),
        "agent_id": agent_id,
        "agent_name": selected_name,
    }).to_response()


@agents_bp.route("/workflows/run-trend", methods=["GET"])
@unified_auth_required
def workflow_run_trend():
    """Daily workflow run outcome trend for the current user.

    Buckets by calendar day (UTC) using ``finished_at`` (when the run reached
    a terminal state). Each bucket has ``succeeded`` and ``failed`` counts.
    Runs still pending/running/paused/cancelled are excluded (no finish time
    or non-terminal). Useful for charting workflow reliability over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    from sqlalchemy import func as sa_func
    daily = (
        db.session.query(
            sa_func.date(WorkflowRun.finished_at).label("date"),
            WorkflowRun.status,
            sa_func.count(WorkflowRun.id).label("count"),
        )
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowRun.finished_at.isnot(None),
            WorkflowRun.finished_at >= since,
        )
        .group_by(sa_func.date(WorkflowRun.finished_at), WorkflowRun.status)
        .all()
    )
    trend_map: dict = {}
    for d, status, c in daily:
        key = str(d)
        bucket = trend_map.setdefault(key, {"date": key, "succeeded": 0, "failed": 0})
        if status == WorkflowStatus.SUCCEEDED:
            bucket["succeeded"] = c
        elif status == WorkflowStatus.FAILED:
            bucket["failed"] = c
    trend = sorted(trend_map.values(), key=lambda x: x["date"])
    total_succeeded = sum(b["succeeded"] for b in trend)
    total_failed = sum(b["failed"] for b in trend)

    # 按日失败步骤数（WorkflowStepRun status=FAILED，按 finished_at 分桶）
    step_daily = (
        db.session.query(
            sa_func.date(WorkflowStepRun.finished_at).label("date"),
            sa_func.count(WorkflowStepRun.id).label("count"),
        )
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .group_by(sa_func.date(WorkflowStepRun.finished_at))
        .all()
    )
    step_failed_by_day = {str(d): c for d, c in step_daily if d}
    for b in trend:
        b["failed_steps"] = step_failed_by_day.get(b["date"], 0)
    total_failed_steps = sum(step_failed_by_day.values())

    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_succeeded": total_succeeded,
        "total_failed": total_failed,
        "total_failed_steps": total_failed_steps,
    }).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>", methods=["GET"])
@unified_auth_required
def get_workflow_run(run_id):
    """Get a single workflow run with step details."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True)).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/console", methods=["GET"])
@unified_auth_required
def get_workflow_run_console(run_id):
    """Step-level real-time console: aggregates step runs with their sandbox
    executions, effective params, recent run logs, and any conflicts tied to
    the run — a single payload for monitoring/intervening on a running workflow.

    Query params:
      log_limit (default 5): max recent RunLog entries per step
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    try:
        log_limit = max(1, min(50, int(request.args.get("log_limit", 5))))
    except (TypeError, ValueError):
        log_limit = 5

    now = datetime.utcnow()
    steps_payload = []
    for sr in wf_run.step_runs:
        # Effective params (overrides merged with definition)
        effective = {}
        for k in _RUNTIME_OVERRIDABLE_KEYS:
            effective[k] = sr.get_effective_param(k)

        # Sandbox execution bound to this step (most recent)
        sandbox_exec = SandboxExecution.query.filter_by(step_run_id=sr.id).order_by(
            SandboxExecution.created_at.desc()
        ).first()
        sandbox_exec_dict = None
        sandbox_policy = None
        if sandbox_exec:
            sandbox_exec_dict = sandbox_exec.to_dict(include_violations=True)
            sb = AgentSandbox.query.get(sandbox_exec.sandbox_id)
            sandbox_policy = sb.to_dict() if sb else None

        # Recent run logs for the AgentRun bound to this step
        logs = []
        if sr.assignment_id:
            bound_run = AgentRun.query.filter_by(assignment_id=sr.assignment_id).order_by(
                AgentRun.started_at.desc()
            ).first()
            if bound_run:
                logs = [l.to_dict() for l in RunLog.query.filter_by(run_id=bound_run.id).order_by(
                    RunLog.created_at.desc()
                ).limit(log_limit).all()]
                logs.reverse()  # chronological order for display

        # Timing
        duration_seconds = None
        if sr.started_at:
            end = sr.finished_at or now
            duration_seconds = (end - sr.started_at).total_seconds()

        steps_payload.append({
            "step_run": sr.to_dict(),
            "effective_params": effective,
            "sandbox_execution": sandbox_exec_dict,
            "sandbox_policy": sandbox_policy,
            "recent_logs": logs,
            "duration_seconds": duration_seconds,
        })

    # Conflicts tied to this run
    run_conflicts = AgentConflict.query.filter_by(
        owner_id=user.id, workflow_run_id=run_id
    ).order_by(AgentConflict.created_at.desc()).all()

    # Overall progress summary
    status_counts = {}
    for sr in wf_run.step_runs:
        s = sr.status.value if sr.status else "unknown"
        status_counts[s] = status_counts.get(s, 0) + 1
    total_steps = len(wf_run.step_runs)
    done = status_counts.get("succeeded", 0) + status_counts.get("skipped", 0) + status_counts.get("cancelled", 0)
    progress_pct = round((done / total_steps) * 100, 1) if total_steps else 0.0

    return ApiResponse.success({
        "workflow_run": wf_run.to_dict(include_step_runs=False),
        "steps": steps_payload,
        "conflicts": [c.to_dict() for c in run_conflicts],
        "summary": {
            "total_steps": total_steps,
            "status_counts": status_counts,
            "progress_percent": progress_pct,
            "running_count": status_counts.get("running", 0),
            "failed_count": status_counts.get("failed", 0),
            "pending_count": status_counts.get("pending", 0) + status_counts.get("waiting", 0),
        },
    }).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/cancel", methods=["POST"])
@unified_auth_required
def cancel_workflow_run(run_id):
    """Cancel a running workflow."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
        return ApiResponse.error("Workflow is not cancellable", 400).to_response()

    now = datetime.utcnow()
    wf_run.status = WorkflowStatus.CANCELLED
    wf_run.finished_at = now
    # Cancel all pending/waiting/running step runs
    for sr in wf_run.step_runs:
        if sr.status in (StepStatus.PENDING, StepStatus.WAITING, StepStatus.RUNNING):
            sr.status = StepStatus.CANCELLED
            sr.finished_at = now

    db.session.commit()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow cancelled").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/pause", methods=["POST"])
@unified_auth_required
def pause_workflow_run(run_id):
    """Pause a running workflow. Running steps will continue but no new steps will be started."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status != WorkflowStatus.RUNNING:
        return ApiResponse.error("Only running workflows can be paused", 400).to_response()

    now = datetime.utcnow()
    wf_run.status = WorkflowStatus.PAUSED
    # Mark any WAITING steps as PAUSED too so they don't get picked up on resume
    for sr in wf_run.step_runs:
        if sr.status == StepStatus.WAITING:
            sr.status = StepStatus.PENDING
    db.session.commit()
    AuditLog.record("workflow_pause", target_type="workflow_run", target_id=run_id, user_id=user.id,
                     details={"workflow_name": wf_run.workflow.name if wf_run.workflow else None})
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow paused").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/resume", methods=["POST"])
@unified_auth_required
def resume_workflow_run(run_id):
    """Resume a paused workflow. The DAG engine will re-evaluate which steps can start."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status != WorkflowStatus.PAUSED:
        return ApiResponse.error("Only paused workflows can be resumed", 400).to_response()

    wf_run.status = WorkflowStatus.RUNNING
    db.session.commit()
    AuditLog.record("workflow_resume", target_type="workflow_run", target_id=run_id, user_id=user.id,
                     details={"workflow_name": wf_run.workflow.name if wf_run.workflow else None})
    # Re-evaluate which steps can start now
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow resumed").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/retry", methods=["POST"])
@unified_auth_required
def retry_workflow_run(run_id):
    """Retry a failed workflow by resetting failed steps and re-advancing the DAG."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status != WorkflowStatus.FAILED:
        return ApiResponse.error("Only failed workflows can be retried", 400).to_response()

    now = datetime.utcnow()
    # Reset failed steps back to PENDING so the DAG engine can re-evaluate
    retried_steps = []
    for sr in wf_run.step_runs:
        if sr.status == StepStatus.FAILED:
            sr.status = StepStatus.PENDING
            sr.error = None
            sr.finished_at = None
            sr.attempt = (sr.attempt or 1) + 1
            retried_steps.append(sr.step_key)
        elif sr.status == StepStatus.SKIPPED:
            # Also retry skipped steps — they may have been skipped due to a prior failure
            sr.status = StepStatus.PENDING
            sr.finished_at = None
            sr.attempt = (sr.attempt or 1) + 1
            retried_steps.append(sr.step_key)

    wf_run.status = WorkflowStatus.RUNNING
    wf_run.error = None
    wf_run.finished_at = None
    db.session.commit()

    AuditLog.record("workflow_retry", target_type="workflow_run", target_id=run_id, user_id=user.id,
                     details={"retried_steps": retried_steps})
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow retry started").to_response()


# --- Workflow step callback (called by Agent system when a step completes) ---


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/complete", methods=["POST"])
@unified_auth_required
def complete_workflow_step(run_id, step_key):
    """Mark a workflow step as completed (or failed) and advance the DAG.

    This is the callback that the Agent system calls when a step finishes.
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()

    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()

    data = validate_json_request()
    success = data.get("success", True)
    now = datetime.utcnow()

    if success:
        sr.status = StepStatus.SUCCEEDED
        # Auto-save step result to SharedContext for downstream steps
        if sr.task_id:
            result_summary = data.get("result_summary", "")
            if result_summary:
                existing = SharedContext.query.filter_by(task_id=sr.task_id, key=f"step_result_{step_key}").first()
                if existing:
                    existing.value = result_summary
                    if sr.agent_id:
                        existing.author_agent_id = sr.agent_id
                else:
                    SharedContext.create(
                        task_id=sr.task_id,
                        key=f"step_result_{step_key}",
                        value=result_summary,
                        author_agent_id=sr.agent_id,
                    )
    else:
        sr.status = StepStatus.FAILED
        sr.error = data.get("error", "")

        # Auto-retry: if the step definition has retry_count and we haven't exhausted attempts
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=step_key
        ).first()
        # Apply runtime overrides so a dynamically-reconfigured retry_count takes effect
        step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
        if step_def and step_def.retry_count > 0:
            current_attempt = sr.attempt or 1
            if current_attempt <= step_def.retry_count:
                # Reset step for retry
                sr.status = StepStatus.PENDING
                sr.error = None
                sr.finished_at = None
                sr.attempt = current_attempt + 1
                # Cancel the old assignment/run
                if sr.assignment_id:
                    old_assignment = TaskAssignment.query.get(sr.assignment_id)
                    if old_assignment and old_assignment.state in LEASED_EXECUTION_STATES:
                        old_assignment.state = TaskAssignmentState.CANCELLED
                        old_assignment.completed_at = now
                if sr.task_id:
                    old_runs = AgentRun.query.filter_by(
                        assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                    ).all()
                    for r in old_runs:
                        r.status = AgentRunStatus.CANCELLED
                        r.ended_at = now
                sr.assignment_id = None
                sr.agent_id = None
                sr.task_id = None
                # Record retry event
                if sr.task_id:
                    record_task_event(
                        task_id=sr.task_id,
                        event_type="workflow_step_auto_retry",
                        actor_type="system",
                        payload={"step_key": step_key, "attempt": current_attempt + 1, "max_retries": step_def.retry_count},
                    )

    sr.finished_at = now

    # Update Agent reputation based on step outcome
    if sr.agent_id and sr.status != StepStatus.PENDING:  # Don't update on auto-retry
        completion_time = None
        if sr.started_at and sr.finished_at:
            completion_time = (sr.finished_at - sr.started_at).total_seconds()
        AgentReputation.record_outcome(
            agent_id=sr.agent_id,
            success=(sr.status == StepStatus.SUCCEEDED),
            completion_time=completion_time,
            context={
                "task_id": sr.task_id,
                "step_key": sr.step_key,
                "workflow_run_id": sr.run_id,
                "duration_sec": round(completion_time, 1) if completion_time else None,
            },
        )

        # Auto-extract experience from step outcome
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=step_key
        ).first()
        task = Task.query.get(sr.task_id) if sr.task_id else None
        try:
            AgentExperience.extract_from_step_outcome(
                agent_id=sr.agent_id,
                step_run=sr,
                step_def=step_def,
                task=task,
            )
        except Exception:
            pass  # Don't fail the step completion if experience extraction fails

        # Complete any sandboxed execution bound to this step's AgentRun
        try:
            if sr.assignment_id:
                bound_run = AgentRun.query.filter_by(
                    assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                ).first()
                if bound_run:
                    sandbox_status = (
                        SandboxExecutionStatus.COMPLETED
                        if sr.status == StepStatus.SUCCEEDED
                        else SandboxExecutionStatus.FAILED
                    )
                    _maybe_finish_sandboxed_execution(
                        bound_run,
                        sandbox_status,
                        summary=data.get("result_summary"),
                        error=data.get("error"),
                    )
        except Exception:
            pass  # Don't fail step completion if sandbox finalization fails

    db.session.commit()

    # Notify clients that a step reached a terminal/intermediate state so the
    # real-time console can refresh without polling.
    _queue_sse(user.id, "workflow_step_finished", {
        "run_id": run_id,
        "step_key": step_key,
        "status": sr.status.value if sr.status else None,
        "agent_id": sr.agent_id,
        "attempt": sr.attempt,
    })

    # Advance the workflow
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()

    return ApiResponse.success(
        wf_run.to_dict(include_step_runs=True),
        f"Step {step_key} {'succeeded' if success else 'failed'}",
    ).to_response()


# --- Internal: DAG advancement logic -------------------------------------


def _evaluate_step_condition(condition, step_runs):
    """Evaluate a step's condition against the current step run states.

    Condition format:
      - Simple: {"step_key": "review", "operator": "succeeded", "value": true}
      - Negation: {"step_key": "review", "operator": "failed"}
      - Output match: {"step_key": "review", "operator": "output_contains", "value": "approved"}
      - Composite (AND): {"all": [cond1, cond2]}
      - Composite (OR): {"any": [cond1, cond2]}

    Returns True if the step should execute, False to skip.
    """
    if not condition:
        return True

    # Composite conditions
    if "all" in condition:
        return all(_evaluate_step_condition(c, step_runs) for c in condition["all"])
    if "any" in condition:
        return any(_evaluate_step_condition(c, step_runs) for c in condition["any"])

    # Simple condition
    step_key = condition.get("step_key")
    operator = condition.get("operator", "succeeded")
    value = condition.get("value")

    if not step_key:
        return True  # No step_key means no condition

    sr = step_runs.get(step_key)
    if not sr:
        return False  # Dependency step hasn't started yet

    if operator == "succeeded":
        return sr.status == StepStatus.SUCCEEDED
    elif operator == "failed":
        return sr.status == StepStatus.FAILED
    elif operator == "skipped":
        return sr.status == StepStatus.SKIPPED
    elif operator == "completed":
        return sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED)
    elif operator == "output_equals":
        return (sr.result_summary or "") == str(value)
    elif operator == "output_contains":
        return str(value) in (sr.result_summary or "")
    elif operator == "output_not_contains":
        return str(value) not in (sr.result_summary or "")
    elif operator == "status_equals":
        return sr.status.value == str(value) if hasattr(sr.status, 'value') else str(sr.status) == str(value)
    else:
        # Unknown operator — default to True (don't block execution)
        return True


def _propagate_sub_workflow_completion(sub_wf_run):
    """When a sub-workflow completes, find and advance the parent step that launched it.

    Looks for step runs whose result_summary contains "sub_workflow_run:{id}".
    When found, marks the parent step as succeeded/failed and advances the parent workflow.
    """
    if not sub_wf_run or sub_wf_run.status not in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED):
        return

    # Search for step runs that reference this sub-workflow
    parent_step_runs = WorkflowStepRun.query.filter(
        WorkflowStepRun.result_summary.like(f"%sub_workflow_run:{sub_wf_run.id}%"),
        WorkflowStepRun.status == StepStatus.RUNNING,
    ).all()

    now = datetime.utcnow()
    for psr in parent_step_runs:
        if sub_wf_run.status == WorkflowStatus.SUCCEEDED:
            psr.status = StepStatus.SUCCEEDED
            psr.result_summary = f"Sub-workflow #{sub_wf_run.id} completed successfully"
        else:
            psr.status = StepStatus.FAILED
            psr.error = f"Sub-workflow #{sub_wf_run.id} failed"
            psr.result_summary = f"Sub-workflow #{sub_wf_run.id} failed"
        psr.finished_at = now

        # Update reputation for the agent that managed the sub-workflow
        if psr.agent_id:
            AgentReputation.record_outcome(
                agent_id=psr.agent_id,
                success=(sub_wf_run.status == WorkflowStatus.SUCCEEDED),
                context={
                    "parent_workflow_run_id": psr.run_id,
                    "sub_workflow_run_id": sub_wf_run.id,
                    "step_key": psr.step_key,
                },
            )

        # Advance the parent workflow
        parent_run = WorkflowRun.query.get(psr.run_id)
        if parent_run:
            _advance_workflow(parent_run)


# Keys that may be dynamically overridden on a step run without touching the
# workflow definition. Validated against this allowlist when an override is set.
_RUNTIME_OVERRIDABLE_KEYS = {
    "agent_id",
    "required_capabilities",
    "timeout_seconds",
    "retry_count",
    "on_failure",
    "condition",
    "task_template_id",
    "sub_workflow_id",
}


def _apply_runtime_overrides(step_def, step_run):
    """Return a view of step_def with any runtime overrides from step_run applied.

    Uses a SimpleNamespace so downstream code (which reads attributes like
    step_def.agent_id, step_def.required_capabilities, etc.) works unchanged.
    The original WorkflowStep definition is never mutated.
    """
    overrides = (step_run.runtime_overrides if step_run else None) or {}
    if not overrides:
        return step_def
    from types import SimpleNamespace
    merged = SimpleNamespace(
        step_key=step_def.step_key,
        name=step_def.name,
        description=step_def.description,
        order=step_def.order,
        required_capabilities=step_def.required_capabilities,
        agent_id=step_def.agent_id,
        task_template_id=step_def.task_template_id,
        depends_on=step_def.depends_on,
        condition=step_def.condition,
        sub_workflow_id=step_def.sub_workflow_id,
        timeout_seconds=step_def.timeout_seconds,
        retry_count=step_def.retry_count,
        on_failure=step_def.on_failure,
    )
    for k, v in overrides.items():
        if k in _RUNTIME_OVERRIDABLE_KEYS and v is not None:
            setattr(merged, k, v)
    return merged


def _advance_workflow(wf_run):
    """Examine step runs and start any whose dependencies are all satisfied.

    If all steps are terminal, mark the workflow as finished.
    """
    now = datetime.utcnow()
    step_runs = {sr.step_key: sr for sr in wf_run.step_runs}
    steps = {
        s.step_key: s
        for s in WorkflowStep.query.filter_by(workflow_id=wf_run.workflow_id).all()
    }

    # Check if the overall workflow is already terminal
    if wf_run.status in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
        return

    any_running = False
    all_terminal = True

    # If workflow is paused, don't start new steps (already-running steps continue)
    is_paused = wf_run.status == WorkflowStatus.PAUSED

    # Check max parallelism from workflow definition
    max_parallel = 0
    if wf_run.workflow:
        max_parallel = wf_run.workflow.max_parallel_steps or 0
    # Also support per-run override from definition
    if not max_parallel and wf_run.workflow and wf_run.workflow.definition:
        max_parallel = wf_run.workflow.definition.get("max_parallel_steps", 0)

    # Count currently running steps
    running_count = sum(1 for sr in step_runs.values() if sr.status == StepStatus.RUNNING)

    for step_key, sr in step_runs.items():
        if sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
            continue  # already terminal
        all_terminal = False

        if sr.status == StepStatus.RUNNING:
            any_running = True
            continue

        # PENDING or WAITING — check dependencies
        step_def = steps.get(step_key)
        if not step_def:
            continue
        # Apply runtime overrides (dynamic reconfiguration) on top of the
        # step definition. Only affects this run; the definition is unchanged.
        step_def = _apply_runtime_overrides(step_def, sr)

        # Don't start new steps if paused
        if is_paused:
            continue

        # Check max parallelism: skip starting if we've hit the limit
        if max_parallel > 0 and running_count >= max_parallel:
            continue

        deps = step_def.depends_on or []
        deps_met = all(
            step_runs.get(dep_key) and step_runs[dep_key].status == StepStatus.SUCCEEDED
            for dep_key in deps
        )

        # Check if any dependency failed
        any_dep_failed = any(
            step_runs.get(dep_key) and step_runs[dep_key].status in (StepStatus.FAILED, StepStatus.CANCELLED)
            for dep_key in deps
        )

        if any_dep_failed:
            # Handle based on on_failure policy
            if step_def.on_failure == "skip":
                sr.status = StepStatus.SKIPPED
                sr.finished_at = now
            elif step_def.on_failure == "continue":
                # Treat as if deps are met — start the step anyway
                _start_step(wf_run, sr, step_def, now)
                any_running = True
                running_count += 1
            else:
                # abort — mark step as waiting (will never start) and fail the workflow
                sr.status = StepStatus.WAITING
            continue

        if deps_met:
            # Check conditional execution
            if step_def.condition:
                if not _evaluate_step_condition(step_def.condition, step_runs):
                    # Condition not met — skip this step
                    sr.status = StepStatus.SKIPPED
                    sr.finished_at = now
                    continue

            _start_step(wf_run, sr, step_def, now)
            any_running = True
            running_count += 1

    # If nothing is running and all are terminal, the workflow is done
    if all_terminal:
        # Determine overall status
        has_failure = any(
            sr.status in (StepStatus.FAILED, StepStatus.CANCELLED)
            for sr in step_runs.values()
        )
        wf_run.status = WorkflowStatus.FAILED if has_failure else WorkflowStatus.SUCCEEDED
        wf_run.finished_at = now

        # Check if this is a sub-workflow — advance the parent step
        _propagate_sub_workflow_completion(wf_run)
    elif any_running and wf_run.status == WorkflowStatus.PENDING:
        wf_run.status = WorkflowStatus.RUNNING
        wf_run.started_at = now


def _pick_agent_for_step(wf_run, step_def):
    """Pick the best Agent for a workflow step based on capabilities, role, workload, and reputation.

    Searches the workflow owner's own agents first, then extends to cross-project
    authorized agents if no suitable match is found.
    """
    agent = None
    if step_def.agent_id:
        agent = Agent.query.get(step_def.agent_id)
    if not agent and step_def.required_capabilities:
        required = set(step_def.required_capabilities)
        is_coordination_step = "coordination" in required or "management" in required

        # Phase 1: Search own agents
        candidates = Agent.query.filter(
            Agent.status == AgentStatus.ACTIVE,
            Agent.owner_id == wf_run.owner_id,
        ).all()

        scored = []
        for c in candidates:
            expanded = _expand_capabilities(set(c.capabilities or []))
            match_count = len(required.intersection(expanded))
            if match_count == 0:
                continue
            role = c.collaboration_role or "standalone"
            role_bonus = 0
            if is_coordination_step and role == "leader":
                role_bonus = 100
            elif not is_coordination_step and role == "follower":
                role_bonus = 50
            active_count = TaskAssignment.query.filter(
                TaskAssignment.agent_id == c.id,
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
            ).count()
            workload_penalty = active_count * 15
            # Reputation bonus
            rep = AgentReputation.query.filter_by(agent_id=c.id).first()
            rep_bonus = int((rep.score - 50) * 0.3) if rep and rep.score > 50 else 0
            scored.append((c, match_count * 10 + role_bonus - workload_penalty + rep_bonus, active_count))

        # Phase 2: If no good match, search cross-project agents
        if not scored or scored[0][1] < 20:
            # Get the workflow's project
            wf = Workflow.query.get(wf_run.workflow_id)
            if wf and wf.project_id:
                cross_auths = CrossProjectAgent.get_active_for_project(wf.project_id)
                for auth in cross_auths:
                    c = Agent.query.get(auth.agent_id)
                    if not c or c.status != AgentStatus.ACTIVE:
                        continue
                    # Use effective capabilities (may be overridden for this project)
                    caps = CrossProjectAgent.get_effective_capabilities(c.id, wf.project_id)
                    expanded = _expand_capabilities(set(caps or []))
                    match_count = len(required.intersection(expanded))
                    if match_count == 0:
                        continue
                    # Check concurrent task limit
                    active_count = TaskAssignment.query.filter(
                        TaskAssignment.agent_id == c.id,
                        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                    ).count()
                    if active_count >= (auth.max_concurrent_tasks or 3):
                        continue
                    # Cross-project agents get a small penalty (prefer own agents)
                    cross_penalty = -5
                    role = c.collaboration_role or "standalone"
                    role_bonus = 0
                    if is_coordination_step and role == "leader":
                        role_bonus = 100
                    elif not is_coordination_step and role == "follower":
                        role_bonus = 50
                    rep = AgentReputation.query.filter_by(agent_id=c.id).first()
                    rep_bonus = int((rep.score - 50) * 0.3) if rep and rep.score > 50 else 0
                    scored.append((c, match_count * 10 + role_bonus - active_count * 15 + rep_bonus + cross_penalty, active_count))

        if scored:
            scored.sort(key=lambda x: -x[1])
            agent = scored[0][0]

    if not agent:
        leader = Agent.query.filter_by(
            status=AgentStatus.ACTIVE, owner_id=wf_run.owner_id,
            collaboration_role="leader",
        ).first()
        if leader:
            agent = leader
        else:
            agent = Agent.query.filter_by(
                status=AgentStatus.ACTIVE, owner_id=wf_run.owner_id,
            ).first()
    return agent


def _start_step(wf_run, step_run, step_def, now):
    """Start a single step: find a matching Agent, create a task, and claim it.

    If the step has a sub_workflow_id, launch that workflow instead of creating
    a single task. The sub-workflow's completion will be tracked as this step's
    outcome.
    """
    step_run.status = StepStatus.RUNNING
    step_run.started_at = now

    # --- Sub-workflow handling ---
    if step_def.sub_workflow_id:
        sub_wf = Workflow.query.filter_by(id=step_def.sub_workflow_id, owner_id=wf_run.owner_id).first()
        if not sub_wf:
            step_run.status = StepStatus.FAILED
            step_run.error = f"Sub-workflow {step_def.sub_workflow_id} not found"
            step_run.finished_at = now
            return

        # Find an agent to own the sub-workflow (prefer coordinator/leader)
        agent = _pick_agent_for_step(wf_run, step_def)

        # Launch the sub-workflow
        sub_run = WorkflowRun.create(
            workflow_id=sub_wf.id,
            root_task_id=wf_run.root_task_id,
            project_id=wf_run.project_id,
            owner_id=wf_run.owner_id,
            status=WorkflowStatus.PENDING,
        )
        # Create step runs for sub-workflow
        for sub_step in sub_wf.steps:
            WorkflowStepRun.create(
                run_id=sub_run.id,
                step_key=sub_step.step_key,
                status=StepStatus.PENDING,
            )
        db.session.commit()

        # Store the sub-run reference on the step_run
        step_run.result_summary = f"sub_workflow_run:{sub_run.id}"
        if agent:
            step_run.agent_id = agent.id

        # Advance the sub-workflow
        _advance_workflow(sub_run)
        db.session.commit()

        record_task_event(
            task_id=wf_run.root_task_id,
            event_type="sub_workflow_launched",
            actor_type="system",
            payload={
                "parent_run_id": wf_run.id,
                "parent_step_key": step_def.step_key,
                "sub_workflow_id": sub_wf.id,
                "sub_run_id": sub_run.id,
            },
        )
        return

    # --- Normal step handling ---
    # Find a matching Agent
    agent = _pick_agent_for_step(wf_run, step_def)

    if not agent:
        step_run.status = StepStatus.FAILED
        step_run.error = "No available Agent"
        step_run.finished_at = now
        return

    step_run.agent_id = agent.id

    # Create a task for this step
    task_title = f"[Workflow:{wf_run.id}] {step_def.name}"
    task_content = step_def.description or ""

    # Inject predecessor step outputs as context
    deps = step_def.depends_on or []
    if deps:
        predecessor_context_parts = []
        for dep_key in deps:
            dep_sr = WorkflowStepRun.query.filter_by(run_id=wf_run.id, step_key=dep_key).first()
            if dep_sr and dep_sr.task_id:
                ctx_entries = SharedContext.query.filter_by(task_id=dep_sr.task_id).order_by(SharedContext.key.asc()).all()
                if ctx_entries:
                    parts = [f"--- 前置步骤 [{dep_key}] 上下文 ---"]
                    for entry in ctx_entries:
                        parts.append(f"**{entry.key}** (by {entry.author_agent.name if entry.author_agent else 'user'}):\n{entry.value}")
                    predecessor_context_parts.append("\n".join(parts))
        if predecessor_context_parts:
            task_content = (task_content + "\n\n" if task_content else "") + "\n\n".join(predecessor_context_parts)
    task_kwargs = dict(
        project_id=wf_run.project_id,
        title=task_title,
        content=task_content,
        status=TaskStatus.TODO,
        is_ai_task=True,
        creator_id=wf_run.owner_id,
        parent_task_id=wf_run.root_task_id,
    )
    # If there's a task template, use its defaults
    if step_def.task_template_id:
        tmpl = TaskTemplate.query.get(step_def.task_template_id)
        if tmpl:
            task_kwargs["title"] = task_title + f" (from: {tmpl.name})"
            if tmpl.content_template:
                task_kwargs["content"] = tmpl.content_template
            if tmpl.priority:
                try:
                    from models.task import TaskPriority
                    task_kwargs["priority"] = TaskPriority(tmpl.priority)
                except ValueError:
                    pass
            if tmpl.tags:
                task_kwargs["tags"] = tmpl.tags
            if tmpl.is_ai_task is not None:
                task_kwargs["is_ai_task"] = tmpl.is_ai_task

    task = Task.create(**task_kwargs)
    step_run.task_id = task.id

    # Claim the task for the agent
    assignment = TaskAssignment.create(
        task_id=task.id,
        agent_id=agent.id,
        assigned_by_user_id=wf_run.owner_id,
        state=TaskAssignmentState.ASSIGNED,
    )
    run = AgentRun.create(
        task_id=task.id,
        agent_id=agent.id,
        assignment_id=assignment.id,
        status=AgentRunStatus.RUNNING,
        started_at=now,
    )
    step_run.assignment_id = assignment.id

    # --- Sandbox integration: auto-start a sandboxed execution if the agent
    # has an active sandbox policy bound to it. The policy snapshot is frozen
    # so audits remain valid even if the policy changes later. ---
    _maybe_start_sandboxed_execution(agent, run, step_run)

    # Record event
    record_task_event(
        task_id=task.id,
        event_type="workflow_step_started",
        actor_type="system",
        payload={
            "workflow_run_id": wf_run.id,
            "step_key": step_def.step_key,
            "agent_id": agent.id,
            "agent_name": agent.name,
        },
    )
    # SSE so the real-time console reflects step start/assignment immediately
    _queue_sse(wf_run.owner_id, "workflow_step_started", {
        "run_id": wf_run.id,
        "step_key": step_def.step_key,
        "agent_id": agent.id,
        "agent_name": agent.name,
    })


def _maybe_start_sandboxed_execution(agent, run, step_run):
    """If the agent has an active sandbox, create a RUNNING SandboxExecution
    bound to this AgentRun + WorkflowStepRun, freezing the policy snapshot.

    Returns the created SandboxExecution or None.
    """
    sandbox = AgentSandbox.get_for_agent(agent.id)
    if not sandbox:
        return None
    execution = SandboxExecution(
        sandbox_id=sandbox.id,
        agent_id=agent.id,
        run_id=run.id,
        step_run_id=step_run.id if step_run else None,
        status=SandboxExecutionStatus.RUNNING,
        policy_snapshot=sandbox.to_policy_dict(),
        started_at=datetime.utcnow(),
        tool_calls=0,
        network_calls=0,
    )
    db.session.add(execution)
    db.session.flush()
    return execution


def _maybe_finish_sandboxed_execution(run, status, summary=None, error=None):
    """Complete any RUNNING SandboxExecution bound to an AgentRun.

    Called when a workflow step / agent run completes (success or failure).
    """
    if not run:
        return None
    execution = SandboxExecution.query.filter_by(
        run_id=run.id, status=SandboxExecutionStatus.RUNNING
    ).first()
    if not execution:
        return None
    execution.finish(status, summary=summary, error=error)
    return execution


# =========================================================================
# Priority auto-escalation
# =========================================================================

_PRIORITY_LADDER = {
    "low": "medium",
    "medium": "high",
    "high": "urgent",
}


def _escalate_overdue_tasks(owner_id=None, overdue_after_days=1):
    """Auto-escalate the priority of overdue tasks that are not yet urgent.

    Tasks whose due_date is in the past and whose status is not in a terminal
    state (done / cancelled) will have their priority bumped one level.
    Returns the list of escalated task IDs.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=overdue_after_days)

    query = Task.query.filter(
        Task.due_date.isnot(None),
        Task.due_date < cutoff,
        Task.status.notin_([TaskStatus.DONE, TaskStatus.CANCELLED]),
        Task.priority != TaskPriority.URGENT,
    )
    if owner_id:
        project_ids = [p.id for p in Project.query.filter_by(owner_id=owner_id).all()]
        query = query.filter(Task.project_id.in_(project_ids))

    escalated = []
    for task in query.all():
        current = task.priority.value if task.priority else "medium"
        next_level = _PRIORITY_LADDER.get(current)
        if next_level:
            try:
                task.priority = TaskPriority(next_level)
                db.session.add(task)
                escalated.append(task.id)
                Notification.create_notification(
                    user_id=task.project.owner_id if task.project and task.project.owner_id else None,
                    event_type="task_priority_escalated",
                    task_id=task.id,
                    payload={
                        "old_priority": current,
                        "new_priority": next_level,
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                    },
                )
            except (ValueError, AttributeError):
                pass

    if escalated:
        db.session.commit()

    return escalated


@agents_bp.route("/maintenance/escalate-overdue", methods=["POST"])
@unified_auth_required
def escalate_overdue():
    """Manually trigger priority escalation for overdue tasks.

    Can be called by a cron job or manually. Only escalates tasks owned by the
    current user unless the user is an admin.
    """
    try:
        current_user = get_current_user()
        data = request.get_json(silent=True) or {}
        overdue_after_days = data.get("overdue_after_days", 1)
        escalated = _escalate_overdue_tasks(
            owner_id=current_user.id,
            overdue_after_days=overdue_after_days,
        )
        return ApiResponse.success(
            {"escalated_count": len(escalated), "task_ids": escalated},
            f"Escalated {len(escalated)} overdue task(s)",
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to escalate: {str(e)}", 500).to_response()


# =========================================================================
# Audit Log API
# =========================================================================


@agents_bp.route("/audit-logs", methods=["GET"])
@unified_auth_required
def list_audit_logs():
    """Query the immutable audit trail for platform operations."""
    user = get_current_user()
    args = get_request_args()

    query = AuditLog.query.filter(
        or_(
            AuditLog.actor_user_id == user.id,
            AuditLog.project_id.in_([p.id for p in Project.query.filter_by(owner_id=user.id).all()]),
        )
    )

    # Filters
    if args.get("action"):
        query = query.filter(AuditLog.action == args.get("action"))
    if args.get("resource_type"):
        query = query.filter(AuditLog.resource_type == args.get("resource_type"))
    if args.get("resource_id", type=int):
        query = query.filter(AuditLog.resource_id == args.get("resource_id", type=int))
    if args.get("actor_type"):
        query = query.filter(AuditLog.actor_type == args.get("actor_type"))
    if args.get("actor_agent_id", type=int):
        query = query.filter(AuditLog.actor_agent_id == args.get("actor_agent_id", type=int))
    if args.get("project_id", type=int):
        query = query.filter(AuditLog.project_id == args.get("project_id", type=int))

    query = query.order_by(AuditLog.created_at.desc())
    result = paginate_query(query, args)
    items = [log.to_dict() for log in result["items"]]
    return ApiResponse.paginated(items, result["pagination"]).to_response()


@agents_bp.route("/security/events", methods=["GET"])
@unified_auth_required
def list_security_events():
    """Unified security event log: aggregates sandbox violations, agent
    conflicts, and security-relevant audit entries (sandbox./conflict./
    reputation./workflow_step_overridden) into a single time-ordered feed.

    Each event is normalized to:
      {event_type, occurred_at, severity, agent_id, title, detail,
       source, source_id, workflow_run_id}

    Filters: agent_id, workflow_run_id, event_type, severity, since (ISO),
    until (ISO), search (keyword on title/detail), plus standard pagination.
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    # Pagination (in-memory since merged from multiple sources)
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 20, type=int) or 20
    per_page = max(1, min(100, per_page))
    total = len(events)
    start = (page - 1) * per_page
    page_items = events[start:start + per_page]
    pagination = {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        "has_prev": page > 1,
        "has_next": (start + per_page) < total,
    }
    return ApiResponse.success(
        data={"items": page_items, "pagination": pagination},
        message="Security events",
    ).to_response()


def _collect_security_events(user, args):
    """Collect normalized security events across sandbox violations, agent
    conflicts, and security-relevant audit entries. Returns (events, error_response).

    Shared by list_security_events and the CSV export endpoint so the two stay
    consistent. Filters are read from `args` (a MultiDict-like): agent_id,
    workflow_run_id, event_type, severity, since, until, search.
    """
    agent_filter = args.get("agent_id", type=int)
    run_filter = args.get("workflow_run_id", type=int)
    event_type_filter = args.get("event_type")
    severity_filter = args.get("severity")
    search = (args.get("search") or "").strip().lower()
    since_str = args.get("since")
    until_str = args.get("until")
    since = None
    until = None
    if since_str:
        try:
            since = datetime.fromisoformat(since_str)
        except (ValueError, TypeError):
            return None, ApiResponse.error("Invalid 'since' datetime (use ISO 8601)", 400).to_response()
    if until_str:
        try:
            until = datetime.fromisoformat(until_str)
        except (ValueError, TypeError):
            return None, ApiResponse.error("Invalid 'until' datetime (use ISO 8601)", 400).to_response()

    events = []

    # 1. Sandbox violations (scoped to this owner via sandbox ownership)
    vq = SandboxViolation.query.join(
        AgentSandbox, SandboxViolation.sandbox_id == AgentSandbox.id
    ).filter(AgentSandbox.owner_id == user.id)
    if agent_filter:
        vq = vq.filter(SandboxViolation.agent_id == agent_filter)
    if severity_filter:
        # Only CRITICAL severity maps to sandbox violations
        if severity_filter == "CRITICAL":
            pass
        else:
            vq = vq.filter(False)
    if since:
        vq = vq.filter(SandboxViolation.blocked_at >= since)
    if until:
        vq = vq.filter(SandboxViolation.blocked_at <= until)
    if event_type_filter and event_type_filter != "sandbox_violation":
        vq = vq.filter(False)
    for v in vq.order_by(SandboxViolation.blocked_at.desc()).limit(200).all():
        events.append({
            "event_type": "sandbox_violation",
            "occurred_at": v.blocked_at.isoformat() if v.blocked_at else None,
            "severity": "CRITICAL",
            "agent_id": v.agent_id,
            "title": f"Sandbox violation: {v.violation_type.value if v.violation_type else 'unknown'}",
            "detail": v.detail or v.attempted_action or "",
            "source": "sandbox_violation",
            "source_id": v.id,
            "workflow_run_id": None,
            "extra": {"violation_type": v.violation_type.value if v.violation_type else None,
                      "execution_id": v.execution_id, "sandbox_id": v.sandbox_id},
        })

    # 2. Agent conflicts
    cq = AgentConflict.query.filter_by(owner_id=user.id)
    if agent_filter:
        # agent_ids is a JSON list; filter in Python after fetch for portability
        pass
    if run_filter:
        cq = cq.filter(AgentConflict.workflow_run_id == run_filter)
    if severity_filter:
        cq = cq.filter(AgentConflict.severity == severity_filter)
    if since:
        cq = cq.filter(AgentConflict.created_at >= since)
    if until:
        cq = cq.filter(AgentConflict.created_at <= until)
    for c in cq.order_by(AgentConflict.created_at.desc()).limit(200).all():
        if agent_filter and (not c.agent_ids or agent_filter not in (c.agent_ids or [])):
            continue
        if event_type_filter and event_type_filter != "conflict":
            continue
        events.append({
            "event_type": "conflict",
            "occurred_at": c.created_at.isoformat() if c.created_at else None,
            "severity": c.severity.value if c.severity else "INFO",
            "agent_id": (c.agent_ids or [None])[0] if c.agent_ids else None,
            "title": c.title or c.conflict_type.value if c.conflict_type else "Conflict",
            "detail": c.description or "",
            "source": "agent_conflict",
            "source_id": c.id,
            "workflow_run_id": c.workflow_run_id,
            "extra": {"conflict_type": c.conflict_type.value if c.conflict_type else None,
                      "status": c.status.value if c.status else None,
                      "suggested_strategy": c.suggested_strategy.value if c.suggested_strategy else None},
        })

    # 3. Security-relevant audit log entries
    SECURITY_AUDIT_PREFIXES = ("sandbox.", "conflict.", "reputation.", "workflow_step_overridden", "workflow_step_override_cleared")
    aq = AuditLog.query.filter(
        or_(
            AuditLog.actor_user_id == user.id,
            AuditLog.project_id.in_([p.id for p in Project.query.filter_by(owner_id=user.id).all()]),
        )
    )
    # Prefix filtering (SQLAlchemy .op or startswith depending on dialect; use Python-side for portability)
    audit_rows = aq.order_by(AuditLog.created_at.desc()).limit(500).all()
    for a in audit_rows:
        if not a.action or not any(a.action.startswith(p) for p in SECURITY_AUDIT_PREFIXES):
            continue
        if event_type_filter and event_type_filter != "audit":
            continue
        if since and a.created_at and a.created_at < since:
            continue
        if until and a.created_at and a.created_at > until:
            continue
        events.append({
            "event_type": "audit",
            "occurred_at": a.created_at.isoformat() if a.created_at else None,
            "severity": "CRITICAL" if "revoke" in (a.action or "").lower() or "violation" in (a.action or "").lower()
                        else ("WARNING" if "auto_resolve" in (a.action or "") or "override" in (a.action or "") else "INFO"),
            "agent_id": a.actor_agent_id,
            "title": a.action or "audit",
            "detail": (a.detail or "")[:500] if isinstance(a.detail, str) else str(a.detail or "")[:500],
            "source": "audit_log",
            "source_id": a.id,
            "workflow_run_id": None,
            "extra": {"resource_type": a.resource_type, "resource_id": a.resource_id,
                      "actor_type": a.actor_type, "actor_user_id": a.actor_user_id},
        })

    # Merge and sort by occurred_at desc
    events.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)

    # Keyword search across title/detail (case-insensitive, Python-side since
    # the feed is merged from heterogeneous sources)
    if search:
        events = [
            e for e in events
            if search in (e.get("title") or "").lower()
            or search in (e.get("detail") or "").lower()
        ]
    return events, None


@agents_bp.route("/security/events/export", methods=["GET"])
@unified_auth_required
def export_security_events():
    """Export the unified security event log as CSV or JSON.

    Accepts the same filters as GET /security/events (agent_id,
    workflow_run_id, event_type, severity, since, until, search) plus
    `format` (csv | json, default csv). Up to 1000 rows.
    CSV returns a text/csv attachment; JSON returns a JSON array attachment
    (each item is the full normalized event object, including `extra`).
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    # Cap export volume
    export_rows = events[:1000]
    fmt = (request.args.get("format") or "csv").lower()

    if fmt == "json":
        # Return the full normalized event objects for programmatic consumers.
        payload = io.StringIO()
        import json as _json
        _json.dump(export_rows, payload, ensure_ascii=False, default=str)
        resp = make_response(payload.getvalue())
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            'attachment; filename="security_events.json"'
        )
        return resp

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "occurred_at", "event_type", "severity", "agent_id",
        "workflow_run_id", "source", "source_id", "title", "detail",
    ])
    for e in export_rows:
        detail = e.get("detail") or ""
        if not isinstance(detail, str):
            detail = str(detail)
        writer.writerow([
            e.get("occurred_at") or "",
            e.get("event_type") or "",
            e.get("severity") or "",
            e.get("agent_id") if e.get("agent_id") is not None else "",
            e.get("workflow_run_id") if e.get("workflow_run_id") is not None else "",
            e.get("source") or "",
            e.get("source_id") if e.get("source_id") is not None else "",
            (e.get("title") or "").replace("\n", " ").replace("\r", " "),
            detail.replace("\n", " ").replace("\r", " "),
        ])
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        'attachment; filename="security_events.csv"'
    )
    return resp


@agents_bp.route("/security/events/daily-trend", methods=["GET"])
@unified_auth_required
def security_events_daily_trend():
    """Daily aggregation of security events for trend visualization.

    Reuses _collect_security_events with the same filters (agent_id,
    workflow_run_id, event_type, severity, since, until, search), then
    buckets events by the date portion of occurred_at. Returns:
      {
        days: [{date, sandbox_violation, conflict, audit, total}],
        totals: {sandbox_violation, conflict, audit, total}
      }
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    buckets = {}  # date -> {sandbox_violation, conflict, audit}
    for e in events[:1000]:
        ts = e.get("occurred_at") or ""
        # occurred_at is ISO; date is the first 10 chars (YYYY-MM-DD)
        day = ts[:10] if len(ts) >= 10 else None
        if not day:
            continue
        etype = e.get("event_type") or "audit"
        b = buckets.setdefault(day, {"sandbox_violation": 0, "conflict": 0, "audit": 0})
        if etype in b:
            b[etype] += 1
        else:
            b["audit"] += 1

    # Sort by date ascending
    sorted_days = sorted(buckets.items(), key=lambda kv: kv[0])
    days = [{"date": d, **counts, "total": sum(counts.values())} for d, counts in sorted_days]
    totals = {
        "sandbox_violation": sum(d["sandbox_violation"] for d in days),
        "conflict": sum(d["conflict"] for d in days),
        "audit": sum(d["audit"] for d in days),
        "total": sum(d["total"] for d in days),
    }
    return ApiResponse.success(
        data={"days": days, "totals": totals},
        message="Security events daily trend",
    ).to_response()


@agents_bp.route("/security/events/by-agent", methods=["GET"])
@unified_auth_required
def security_events_by_agent():
    """Per-agent aggregation of security events for ranking.

    Reuses _collect_security_events with the same filters. Buckets events
    by agent_id (events without an agent_id fall under agent_id=null).
    Returns agents: [{agent_id, name, total, sandbox_violation, conflict,
    audit, critical, warning, info}] sorted by total desc (top 50).
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    buckets = {}  # agent_id -> counts
    for e in events[:1000]:
        aid = e.get("agent_id")
        key = aid if aid is not None else 0  # 0 = "no agent"
        b = buckets.setdefault(key, {
            "agent_id": aid,
            "total": 0,
            "sandbox_violation": 0, "conflict": 0, "audit": 0,
            "CRITICAL": 0, "WARNING": 0, "INFO": 0,
        })
        b["total"] += 1
        etype = e.get("event_type") or "audit"
        if etype in ("sandbox_violation", "conflict", "audit"):
            b[etype] += 1
        else:
            b["audit"] += 1
        sev = e.get("severity") or "INFO"
        if sev in ("CRITICAL", "WARNING", "INFO"):
            b[sev] += 1
        else:
            b["INFO"] += 1

    # Resolve agent names (best-effort, single query for known ids)
    known_ids = [k for k in buckets.keys() if k != 0]
    name_map = {}
    if known_ids:
        for a in Agent.query.filter(Agent.id.in_(known_ids)).all():
            name_map[a.id] = a.name

    ranked = sorted(buckets.values(), key=lambda b: b["total"], reverse=True)[:50]
    for b in ranked:
        b["name"] = name_map.get(b["agent_id"]) if b["agent_id"] else "(无 Agent)"

    return ApiResponse.success(
        data={"agents": ranked},
        message="Security events by agent",
    ).to_response()


# =========================================================================
# Health check & auto-recovery
# =========================================================================


@agents_bp.route("/maintenance/health-check", methods=["POST"])
@unified_auth_required
def health_check():
    """Run a full health check: expire stale agents, expire stale leases,
    and escalate overdue tasks. Returns a summary of what was done.

    Designed to be called by a cron job every few minutes.
    """
    try:
        current_user = get_current_user()
        now = datetime.utcnow()

        # 1. Mark stale agents offline (expires their assignments & runs)
        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        stale_agent_ids = [a.id for a in stale_agents]

        # 2. Expire stale leases
        expired_count = 0
        expired_assignments = TaskAssignment.query.filter(
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            TaskAssignment.lease_expires_at.isnot(None),
            TaskAssignment.lease_expires_at < now,
        ).join(Task).join(Project).filter(Project.owner_id == current_user.id).all()

        for assignment in expired_assignments:
            assignment.state = TaskAssignmentState.EXPIRED
            assignment.completed_at = now
            for run in AgentRun.query.filter_by(
                assignment_id=assignment.id, status=AgentRunStatus.RUNNING
            ).all():
                run.status = AgentRunStatus.EXPIRED
                run.ended_at = now
            expired_count += 1

        # 3. Escalate overdue tasks
        escalated_ids = _escalate_overdue_tasks(owner_id=current_user.id)

        db.session.commit()

        # 4. Audit log
        if stale_agents or expired_assignments or escalated_ids:
            AuditLog.record(
                action="maintenance.health_check",
                resource_type="system",
                resource_id=0,
                actor_type="human",
                actor_user_id=current_user.id,
                detail={
                    "stale_agents": stale_agent_ids,
                    "expired_leases": expired_count,
                    "escalated_tasks": escalated_ids,
                },
                ip_address=_client_ip(),
            )
            db.session.commit()

        return ApiResponse.success(
            {
                "stale_agents": len(stale_agent_ids),
                "stale_agent_ids": stale_agent_ids,
                "expired_leases": expired_count,
                "escalated_tasks": len(escalated_ids),
                "escalated_task_ids": escalated_ids,
            },
            f"Health check complete: {len(stale_agent_ids)} stale agent(s), {expired_count} expired lease(s), {len(escalated_ids)} escalated task(s)",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Health check failed: {str(e)}", 500).to_response()


@agents_bp.route("/maintenance/mark-offline-agents", methods=["POST"])
@unified_auth_required
def mark_offline_agents():
    """Scan all active/paused agents and mark those whose last_seen_at exceeds
    AGENT_OFFLINE_AFTER_SECONDS as OFFLINE. Also cancels their running assignments.

    Can be called by a cron scheduler or manually.
    """
    user = get_current_user()
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=AGENT_OFFLINE_AFTER_SECONDS)

    stale = Agent.query.filter(
        Agent.owner_id == user.id,
        Agent.status.in_((AgentStatus.ACTIVE, AgentStatus.PAUSED)),
        db.or_(
            Agent.last_seen_at.is_(None),
            Agent.last_seen_at < cutoff,
        ),
    ).all()

    stale_ids = []
    for agent in stale:
        agent.status = AgentStatus.OFFLINE
        # Cancel running assignments for this agent
        for assignment in TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent.id,
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
        ).all():
            assignment.state = TaskAssignmentState.CANCELLED
            assignment.completed_at = now
        stale_ids.append(agent.id)

    if stale_ids:
        AuditLog.record(
            action="maintenance.mark_offline_agents",
            resource_type="system",
            resource_id=0,
            actor_type="human",
            actor_user_id=user.id,
            detail={"offline_agent_ids": stale_ids, "threshold_seconds": AGENT_OFFLINE_AFTER_SECONDS},
            ip_address=_client_ip(),
        )
    db.session.commit()

    return ApiResponse.success({
        "marked_offline": len(stale_ids),
        "agent_ids": stale_ids,
    }, f"{len(stale_ids)} agent(s) marked offline").to_response()


@agents_bp.route("/maintenance/timeout-workflow-steps", methods=["POST"])
@unified_auth_required
def timeout_workflow_steps():
    """Scan all running workflow steps and mark those that have exceeded their
    timeout_seconds as FAILED, then advance the affected workflows.

    Designed to be called by a cron scheduler periodically.
    """
    user = get_current_user()
    now = datetime.utcnow()
    timed_out = []

    # Find all running step runs owned by this user
    running_steps = WorkflowStepRun.query.filter(
        WorkflowStepRun.status == StepStatus.RUNNING,
    ).join(WorkflowRun).filter(
        WorkflowRun.owner_id == user.id,
        WorkflowRun.status == WorkflowStatus.RUNNING,
    ).all()

    for sr in running_steps:
        # Get the step definition for timeout_seconds
        wf_run = sr.run
        if not wf_run or not wf_run.workflow:
            continue
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=sr.step_key
        ).first()
        # Apply runtime overrides so a dynamically-adjusted timeout takes effect
        step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
        if not step_def or not step_def.timeout_seconds or step_def.timeout_seconds <= 0:
            continue  # no timeout configured

        if sr.started_at:
            elapsed = (now - sr.started_at).total_seconds()
            if elapsed > step_def.timeout_seconds:
                sr.status = StepStatus.FAILED
                sr.error = f"Step timed out after {int(elapsed)}s (limit: {step_def.timeout_seconds}s)"
                sr.finished_at = now
                # Finalize any sandboxed execution as TIMEOUT + record violation
                try:
                    if sr.assignment_id:
                        bound_run = AgentRun.query.filter_by(
                            assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                        ).first()
                        if bound_run:
                            execution = _maybe_finish_sandboxed_execution(
                                bound_run, SandboxExecutionStatus.TIMEOUT,
                                error=sr.error, reason="Step timeout",
                            )
                            if execution:
                                execution.record_violation(
                                    SandboxViolationType.TIMEOUT,
                                    detail=sr.error,
                                    attempted_action=f"step {sr.step_key} exceeded timeout",
                                )
                except Exception:
                    pass
                timed_out.append({
                    "step_key": sr.step_key,
                    "run_id": wf_run.id,
                    "elapsed_seconds": int(elapsed),
                    "timeout_seconds": step_def.timeout_seconds,
                })

    if timed_out:
        AuditLog.record(
            action="maintenance.timeout_workflow_steps",
            resource_type="system",
            resource_id=0,
            actor_type="human",
            actor_user_id=user.id,
            detail={"timed_out_steps": timed_out},
            ip_address=_client_ip(),
        )
    db.session.commit()

    # Re-advance affected workflows
    affected_run_ids = set(t["run_id"] for t in timed_out)
    for run_id in affected_run_ids:
        wf_run = WorkflowRun.query.get(run_id)
        if wf_run:
            _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()

    return ApiResponse.success({
        "timed_out": len(timed_out),
        "steps": timed_out,
    }, f"{len(timed_out)} step(s) timed out").to_response()


@agents_bp.route("/projects/<int:project_id>/members", methods=["GET"])
@unified_auth_required
def list_project_members(project_id):
    """List members of a project with their roles."""
    user = get_current_user()
    project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
    if not project:
        # Also allow if user is a project member
        membership = ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first()
        if not membership:
            return ApiResponse.not_found("Project not found").to_response()

    members = ProjectMember.query.filter_by(project_id=project_id).all()
    items = [m.to_dict() for m in members]
    return ApiResponse.success(items).to_response()


@agents_bp.route("/projects/<int:project_id>/members", methods=["POST"])
@unified_auth_required
def add_project_member(project_id):
    """Add a member to a project with a specified role. Only admins/owners can do this."""
    user = get_current_user()

    # Check if current user can manage this project
    if not ProjectMember.can(project_id, user.id, "manage"):
        # Also allow project owner (legacy)
        project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
        if not project:
            return ApiResponse.error("Insufficient permissions", 403).to_response()

    data = validate_json_request()
    target_user_id = data.get("user_id")
    role_str = data.get("role", "member")

    if not target_user_id:
        return ApiResponse.error("user_id is required", 400).to_response()

    try:
        role = ProjectRole(role_str)
    except ValueError:
        return ApiResponse.error(f"Invalid role: {role_str}. Must be owner/admin/member/viewer", 400).to_response()

    # Can't assign OWNER role via API
    if role == ProjectRole.OWNER:
        return ApiResponse.error("Cannot assign owner role via API", 400).to_response()

    # Check if already a member
    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=target_user_id).first()
    if existing:
        return ApiResponse.error("User is already a member", 409).to_response()

    from models import User
    target_user = User.query.get(target_user_id)
    if not target_user:
        return ApiResponse.error("User not found", 404).to_response()

    member = ProjectMember.create(
        project_id=project_id,
        user_id=target_user_id,
        role=role,
        invited_by=user.id,
        accepted_at=datetime.utcnow(),
    )
    db.session.commit()

    AuditLog.record(
        action="project.member_added", resource_type="project", resource_id=project_id,
        actor_type="human", actor_user_id=user.id,
        project_id=project_id,
        detail={"target_user_id": target_user_id, "role": role.value},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.created(member.to_dict(), "Member added").to_response()


@agents_bp.route("/projects/<int:project_id>/members/<int:member_id>", methods=["PUT"])
@unified_auth_required
def update_project_member(project_id, member_id):
    """Update a member's role. Only admins/owners can do this."""
    user = get_current_user()

    if not ProjectMember.can(project_id, user.id, "manage"):
        project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
        if not project:
            return ApiResponse.error("Insufficient permissions", 403).to_response()

    member = ProjectMember.query.filter_by(id=member_id, project_id=project_id).first()
    if not member:
        return ApiResponse.not_found("Member not found").to_response()

    data = validate_json_request()
    role_str = data.get("role")
    if not role_str:
        return ApiResponse.error("role is required", 400).to_response()

    try:
        new_role = ProjectRole(role_str)
    except ValueError:
        return ApiResponse.error(f"Invalid role: {role_str}", 400).to_response()

    if new_role == ProjectRole.OWNER:
        return ApiResponse.error("Cannot assign owner role via API", 400).to_response()

    old_role = member.role.value if member.role else None
    member.role = new_role
    db.session.commit()

    AuditLog.record(
        action="project.member_updated", resource_type="project", resource_id=project_id,
        actor_type="human", actor_user_id=user.id,
        project_id=project_id,
        detail={"member_id": member_id, "old_role": old_role, "new_role": new_role.value},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.success(member.to_dict(), "Member role updated").to_response()


@agents_bp.route("/projects/<int:project_id>/members/<int:member_id>", methods=["DELETE"])
@unified_auth_required
def remove_project_member(project_id, member_id):
    """Remove a member from a project. Only admins/owners can do this."""
    user = get_current_user()

    if not ProjectMember.can(project_id, user.id, "manage"):
        project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
        if not project:
            return ApiResponse.error("Insufficient permissions", 403).to_response()

    member = ProjectMember.query.filter_by(id=member_id, project_id=project_id).first()
    if not member:
        return ApiResponse.not_found("Member not found").to_response()

    if member.role == ProjectRole.OWNER:
        return ApiResponse.error("Cannot remove the project owner", 400).to_response()

    db.session.delete(member)
    db.session.commit()

    AuditLog.record(
        action="project.member_removed", resource_type="project", resource_id=project_id,
        actor_type="human", actor_user_id=user.id,
        project_id=project_id,
        detail={"removed_user_id": member.user_id},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.success(None, "Member removed").to_response()


# =========================================================================
# Agent broadcast messaging
# =========================================================================


@agents_bp.route("/agents/<int:agent_id>/broadcast", methods=["POST"])
@unified_auth_required
def broadcast_message(agent_id):
    """Send a broadcast message from one Agent to all other active Agents.

    The message is posted as a TaskEvent on the specified task (if any) and
    also creates a Notification for each active Agent's owner.
    """
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        data = validate_json_request(
            optional_fields=["content", "task_id", "event_type", "payload"],
        )
        if isinstance(data, tuple):
            return data

        content = (data.get("content") or "").strip()
        if not content:
            return ApiResponse.error("content is required", 400).to_response()

        task_id = data.get("task_id")
        event_type = (data.get("event_type") or "broadcast").strip().lower()
        payload = data.get("payload") or {}

        # Find all other active agents owned by the same user
        recipients = Agent.query.filter(
            Agent.owner_id == current_user.id,
            Agent.status == AgentStatus.ACTIVE,
            Agent.id != agent_id,
        ).all()

        # If a task_id is provided, post the event on that task
        if task_id:
            task = Task.query.filter_by(id=task_id).join(Project).filter(Project.owner_id == current_user.id).first()
            if task:
                record_task_event(
                    task_id=task.id,
                    event_type=event_type,
                    actor_type="agent",
                    agent=agent,
                    payload={
                        "content": content,
                        "broadcast": True,
                        "recipient_count": len(recipients),
                        **payload,
                    },
                )

        # Create a notification for each recipient agent's owner
        for recipient in recipients:
            Notification.create_notification(
                user_id=current_user.id,
                event_type=f"agent_broadcast_{event_type}",
                agent_id=recipient.id,
                task_id=task_id,
                payload={
                    "from_agent_id": agent.id,
                    "from_agent_name": agent.name,
                    "content": content,
                    **payload,
                },
            )

        db.session.commit()

        AuditLog.record(
            action="agent.broadcast", resource_type="agent", resource_id=agent.id,
            actor_type="agent", actor_agent_id=agent.id,
            actor_user_id=current_user.id,
            detail={"recipient_count": len(recipients), "event_type": event_type, "task_id": task_id},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.success(
            {"recipient_count": len(recipients), "recipient_agent_ids": [r.id for r in recipients]},
            f"Broadcast sent to {len(recipients)} active Agent(s)",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Broadcast failed: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Collaboration Metrics Dashboard
# ---------------------------------------------------------------------------


@agents_bp.route("/dashboard/metrics", methods=["GET"])
@unified_auth_required
def collaboration_metrics():
    """Aggregate collaboration metrics for the dashboard.

    Query params:
      project_id  – scope to a single project (optional)
      days        – look-back window in days (default 7)
    """
    try:
        user = get_current_user()
        project_id = request.args.get("project_id", type=int)
        days = request.args.get("days", 7, type=int)
        since = datetime.utcnow() - timedelta(days=max(1, min(days, 90)))

        # --- Base filters ---
        task_q = Task.query.filter(Task.created_at >= since)
        if project_id:
            task_q = task_q.filter_by(project_id=project_id)

        # --- Task metrics ---
        total_tasks = task_q.count()
        done_tasks = task_q.filter(Task.status == TaskStatus.DONE).count()
        failed_tasks = task_q.filter(
            Task.status.in_([TaskStatus.CANCELLED])
        ).count()
        in_progress = task_q.filter(Task.status == TaskStatus.IN_PROGRESS).count()
        blocked = task_q.filter(Task.status == TaskStatus.BLOCKED).count()
        review = task_q.filter(Task.status == TaskStatus.REVIEW).count()

        # Average completion time for tasks finished in window
        completed_in_window = task_q.filter(
            Task.status == TaskStatus.DONE,
            Task.updated_at >= since,
        ).all()
        completion_times = []
        for t in completed_in_window:
            if t.created_at and t.updated_at:
                delta = (t.updated_at - t.created_at).total_seconds()
                if delta > 0:
                    completion_times.append(delta)
        avg_completion_seconds = (
            sum(completion_times) / len(completion_times)
            if completion_times
            else 0
        )

        # --- Agent metrics ---
        agent_q = Agent.query
        total_agents = agent_q.count()
        active_agents = agent_q.filter_by(status=AgentStatus.ACTIVE).count()
        paused_agents = agent_q.filter_by(status=AgentStatus.PAUSED).count()
        offline_agents = agent_q.filter_by(status=AgentStatus.OFFLINE).count()

        # Agent utilization: agents with at least one running assignment
        agents_with_running = (
            db.session.query(TaskAssignment.agent_id)
            .filter(
                TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                TaskAssignment.agent_id.isnot(None),
            )
            .distinct()
            .count()
        )
        agent_utilization = (
            round(agents_with_running / active_agents * 100, 1) if active_agents else 0
        )

        # --- Assignment metrics ---
        assignment_q = TaskAssignment.query.filter(TaskAssignment.created_at >= since)
        total_assignments = assignment_q.count()
        done_assignments = assignment_q.filter(
            TaskAssignment.state == TaskAssignmentState.DONE
        ).count()
        failed_assignments = assignment_q.filter(
            TaskAssignment.state == TaskAssignmentState.FAILED
        ).count()

        # --- Workflow metrics ---
        wf_run_q = WorkflowRun.query.filter(WorkflowRun.created_at >= since)
        if project_id:
            wf_run_q = wf_run_q.filter_by(project_id=project_id)
        total_wf_runs = wf_run_q.count()
        succeeded_wf_runs = wf_run_q.filter(
            WorkflowRun.status == WorkflowStatus.SUCCEEDED
        ).count()
        failed_wf_runs = wf_run_q.filter(
            WorkflowRun.status == WorkflowStatus.FAILED
        ).count()
        running_wf_runs = wf_run_q.filter(
            WorkflowRun.status == WorkflowStatus.RUNNING
        ).count()
        wf_success_rate = (
            round(succeeded_wf_runs / total_wf_runs * 100, 1) if total_wf_runs else 0
        )

        # --- Handoff metrics ---
        handoff_count = (
            AuditLog.query.filter(
                AuditLog.action == "task.handoff",
                AuditLog.created_at >= since,
            )
            .count()
        )

        # --- Task creation trend (daily buckets) ---
        from sqlalchemy import func as sa_func
        daily_tasks = (
            db.session.query(
                sa_func.date(Task.created_at).label("date"),
                sa_func.count(Task.id).label("created"),
            )
            .filter(Task.created_at >= since)
            .group_by(sa_func.date(Task.created_at))
            .order_by(sa_func.date(Task.created_at))
            .all()
        )
        daily_done = (
            db.session.query(
                sa_func.date(Task.updated_at).label("date"),
                sa_func.count(Task.id).label("completed"),
            )
            .filter(
                Task.status == TaskStatus.DONE,
                Task.updated_at >= since,
            )
            .group_by(sa_func.date(Task.updated_at))
            .order_by(sa_func.date(Task.updated_at))
            .all()
        )
        # Merge into a single dict
        trend_map: dict = {}
        for d, c in daily_tasks:
            trend_map[str(d)] = {"date": str(d), "created": c, "completed": 0}
        for d, c in daily_done:
            key = str(d)
            if key in trend_map:
                trend_map[key]["completed"] = c
            else:
                trend_map[key] = {"date": key, "created": 0, "completed": c}
        trend = sorted(trend_map.values(), key=lambda x: x["date"])

        # --- Agent kind distribution ---
        kind_dist = (
            db.session.query(Agent.kind, sa_func.count(Agent.id))
            .group_by(Agent.kind)
            .all()
        )
        agent_kind_distribution = {str(k): c for k, c in kind_dist}

        # --- Top agents by completed tasks ---
        top_agents_q = (
            db.session.query(
                Agent.id, Agent.name, Agent.kind,
                sa_func.count(TaskAssignment.id).label("completed_count"),
            )
            .join(TaskAssignment, TaskAssignment.agent_id == Agent.id)
            .filter(
                TaskAssignment.state == TaskAssignmentState.DONE,
                TaskAssignment.created_at >= since,
            )
            .group_by(Agent.id, Agent.name, Agent.kind)
            .order_by(sa_func.count(TaskAssignment.id).desc())
            .limit(10)
            .all()
        )
        top_agents = [
            {"id": a_id, "name": name, "kind": str(kind), "completed_count": cnt}
            for a_id, name, kind, cnt in top_agents_q
        ]

        # --- Agent performance details ---
        agent_perf_q = (
            db.session.query(
                Agent.id, Agent.name,
                sa_func.count(TaskAssignment.id).label("total_assignments"),
                sa_func.sum(
                    case(
                        (TaskAssignment.state == TaskAssignmentState.DONE, 1),
                        else_=0,
                    )
                ).label("done_count"),
                sa_func.sum(
                    case(
                        (TaskAssignment.state == TaskAssignmentState.FAILED, 1),
                        else_=0,
                    )
                ).label("failed_count"),
            )
            .join(TaskAssignment, TaskAssignment.agent_id == Agent.id)
            .filter(
                Agent.owner_id == user.id,
                TaskAssignment.created_at >= since,
            )
            .group_by(Agent.id, Agent.name)
            .all()
        )
        agent_performance = []
        for a_id, a_name, total_a, done_a, failed_a in agent_perf_q:
            success_rate = round((done_a / total_a * 100), 1) if total_a else 0
            agent_performance.append({
                "id": a_id,
                "name": a_name,
                "total_assignments": total_a,
                "done": done_a,
                "failed": failed_a,
                "success_rate": success_rate,
            })

        return ApiResponse.success(
            {
                "window_days": days,
                "project_id": project_id,
                "tasks": {
                    "total": total_tasks,
                    "done": done_tasks,
                    "failed": failed_tasks,
                    "in_progress": in_progress,
                    "blocked": blocked,
                    "review": review,
                    "completion_rate": round(done_tasks / total_tasks * 100, 1) if total_tasks else 0,
                    "avg_completion_seconds": round(avg_completion_seconds, 1),
                },
                "agents": {
                    "total": total_agents,
                    "active": active_agents,
                    "paused": paused_agents,
                    "offline": offline_agents,
                    "utilization_pct": agent_utilization,
                    "kind_distribution": agent_kind_distribution,
                },
                "assignments": {
                    "total": total_assignments,
                    "done": done_assignments,
                    "failed": failed_assignments,
                },
                "workflows": {
                    "total_runs": total_wf_runs,
                    "succeeded": succeeded_wf_runs,
                    "failed": failed_wf_runs,
                    "running": running_wf_runs,
                    "success_rate": wf_success_rate,
                },
                "handoffs": handoff_count,
                "trend": trend,
                "top_agents": top_agents,
                "agent_performance": agent_performance,
            },
            "Collaboration metrics",
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to compute metrics: {str(e)}", 500).to_response()


@agents_bp.route("/dashboard/agent-monitor", methods=["GET"])
@unified_auth_required
def agent_monitor():
    """Real-time Agent status monitoring with historical trends.

    Returns per-agent status, current workload, recent activity, and
    hourly activity counts for sparkline-style trend charts.

    Query params:
      project_id – scope to a single project (optional)
      hours      – look-back window for activity trend (default 24)
    """
    try:
        user = get_current_user()
        project_id = request.args.get("project_id", type=int)
        hours = request.args.get("hours", 24, type=int)
        since = datetime.utcnow() - timedelta(hours=max(1, min(hours, 168)))

        # Get all user's agents
        agent_q = Agent.query.filter_by(owner_id=user.id)
        agents = agent_q.all()

        AGENT_OFFLINE_AFTER_SECONDS = 30 * 60
        now = datetime.utcnow()

        monitor_data = []
        for agent in agents:
            # Determine real-time status
            if agent.status == AgentStatus.ACTIVE and agent.last_seen_at:
                elapsed = (now - agent.last_seen_at).total_seconds()
                real_status = "offline" if elapsed > AGENT_OFFLINE_AFTER_SECONDS else "active"
            else:
                real_status = agent.status.value if agent.status else "unknown"

            # Current workload
            active_assignments = TaskAssignment.query.filter(
                TaskAssignment.agent_id == agent.id,
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
            ).all()

            current_tasks = []
            for a in active_assignments[:5]:
                task_info = {"id": a.task_id, "state": a.state.value}
                if a.task:
                    task_info["title"] = a.task.title[:60]
                    task_info["status"] = a.task.status.value if a.task.status else None
                current_tasks.append(task_info)

            # Reputation
            rep = AgentReputation.query.filter_by(agent_id=agent.id).first()
            rep_data = rep.to_dict() if rep else None

            # Recent experience count
            exp_count = AgentExperience.query.filter_by(
                agent_id=agent.id, is_valid=True,
            ).count()
            shared_exp_count = AgentExperience.query.filter_by(
                agent_id=agent.id, is_shared=True, is_valid=True,
            ).count()

            # Hourly activity trend (assignments created/completed per hour)
            from sqlalchemy import func as sa_func
            hourly_activity = (
                db.session.query(
                    sa_func.strftime("%Y-%m-%d %H:00", TaskAssignment.created_at).label("hour"),
                    sa_func.count(TaskAssignment.id).label("count"),
                )
                .filter(
                    TaskAssignment.agent_id == agent.id,
                    TaskAssignment.created_at >= since,
                )
                .group_by(sa_func.strftime("%Y-%m-%d %H:00", TaskAssignment.created_at))
                .order_by("hour")
                .all()
            )

            # SQLite compatibility: try strfttime, fall back to date_trunc for PostgreSQL
            if not hourly_activity:
                try:
                    hourly_activity = (
                        db.session.query(
                            sa_func.date_trunc("hour", TaskAssignment.created_at).label("hour"),
                            sa_func.count(TaskAssignment.id).label("count"),
                        )
                        .filter(
                            TaskAssignment.agent_id == agent.id,
                            TaskAssignment.created_at >= since,
                        )
                        .group_by(sa_func.date_trunc("hour", TaskAssignment.created_at))
                        .order_by("hour")
                        .all()
                    )
                except Exception:
                    hourly_activity = []

            trend = [{"hour": str(h), "count": c} for h, c in hourly_activity]

            # Cross-project access
            cross_projects = CrossProjectAgent.get_active_for_agent(agent.id)

            monitor_data.append({
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_kind": agent.kind.value if agent.kind else None,
                "real_status": real_status,
                "collaboration_role": agent.collaboration_role or "standalone",
                "capabilities": agent.capabilities or [],
                "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                "active_task_count": len(active_assignments),
                "current_tasks": current_tasks,
                "reputation": rep_data,
                "experience_count": exp_count,
                "shared_experience_count": shared_exp_count,
                "cross_project_count": len(cross_projects),
                "activity_trend": trend,
            })

        # Summary stats
        active_count = sum(1 for a in monitor_data if a["real_status"] == "active")
        offline_count = sum(1 for a in monitor_data if a["real_status"] == "offline")
        total_active_tasks = sum(a["active_task_count"] for a in monitor_data)

        return ApiResponse.success({
            "agents": monitor_data,
            "summary": {
                "total_agents": len(monitor_data),
                "active": active_count,
                "offline": offline_count,
                "other": len(monitor_data) - active_count - offline_count,
                "total_active_tasks": total_active_tasks,
                "window_hours": hours,
            },
        }, "Agent monitor data").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to get monitor data: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Workflow Triggers (CRON / Scheduled)
# ---------------------------------------------------------------------------


def _compute_next_fire(cron_expr: str, now: datetime) -> datetime | None:
    """Simple cron next-fire-time calculator.

    Supports the 5-field format: minute hour day-of-month month day-of-week.
    Uses a brute-force scan forward (max 366 days).
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return None

        def _parse_field(field: str, offset: int, size: int) -> set[int]:
            result = set()
            for part in field.split(","):
                if part == "*":
                    result.update(range(offset, offset + size))
                elif "/" in part:
                    base, step = part.split("/", 1)
                    start = offset if base == "*" else int(base)
                    step = int(step)
                    for v in range(start, offset + size, step):
                        result.add(v)
                elif "-" in part:
                    a, b = part.split("-", 1)
                    result.update(range(int(a), int(b) + 1))
                else:
                    result.add(int(part))
            return result

        minutes = _parse_field(parts[0], 0, 60)
        hours = _parse_field(parts[1], 0, 24)
        doms = _parse_field(parts[2], 1, 31)
        months = _parse_field(parts[3], 1, 12)
        dows = _parse_field(parts[4], 0, 7)  # 0=Sun, 7=Sun
        # Normalize Sunday
        if 7 in dows:
            dows.add(0)
            dows.discard(7)

        candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = now + timedelta(days=366)
        while candidate <= end:
            if (candidate.minute in minutes
                    and candidate.hour in hours
                    and candidate.day in doms
                    and candidate.month in months
                    and candidate.weekday() in {(d + 6) % 7 for d in dows}):  # Mon=0..Sun=6
                return candidate
            candidate += timedelta(minutes=1)
        return None
    except Exception:
        return None


@agents_bp.route("/workflow-triggers", methods=["GET"])
@unified_auth_required
def list_workflow_triggers():
    """List all triggers owned by the current user."""
    user = get_current_user()
    args = get_request_args()
    query = WorkflowTrigger.query.filter_by(owner_id=user.id)

    workflow_id = request.args.get("workflow_id", type=int)
    if workflow_id:
        query = query.filter_by(workflow_id=workflow_id)

    is_active = request.args.get("is_active")
    if is_active is not None:
        query = query.filter_by(is_active=is_active.lower() == "true")

    query = query.order_by(WorkflowTrigger.created_at.desc())
    result = paginate_query(query, args["page"], args["per_page"])
    return ApiResponse.success(result).to_response()


@agents_bp.route("/workflow-triggers", methods=["POST"])
@unified_auth_required
def create_workflow_trigger():
    """Create a scheduled trigger for a workflow."""
    user = get_current_user()
    data = validate_json_request(
        required_fields=["workflow_id", "name"],
        optional_fields=["cron_expr", "one_shot_at", "is_active", "project_id", "root_task_id", "context_override"],
    )
    if isinstance(data, tuple):
        return data

    workflow = Workflow.query.get(data["workflow_id"])
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    if not data.get("cron_expr") and not data.get("one_shot_at"):
        return ApiResponse.error("Must provide either cron_expr or one_shot_at", 400).to_response()

    # Compute next_fire_at
    next_fire = None
    if data.get("cron_expr"):
        next_fire = _compute_next_fire(data["cron_expr"], datetime.utcnow())
    elif data.get("one_shot_at"):
        try:
            one_shot = datetime.fromisoformat(data["one_shot_at"])
            next_fire = one_shot
        except (ValueError, TypeError):
            return ApiResponse.error("Invalid one_shot_at format, expected ISO datetime", 400).to_response()

    trigger = WorkflowTrigger.create(
        workflow_id=data["workflow_id"],
        owner_id=user.id,
        name=data["name"],
        cron_expr=data.get("cron_expr"),
        one_shot_at=next_fire if not data.get("cron_expr") else None,
        is_active=data.get("is_active", True),
        project_id=data.get("project_id"),
        root_task_id=data.get("root_task_id"),
        context_override=data.get("context_override"),
        next_fire_at=next_fire,
    )
    db.session.commit()

    AuditLog.record(
        action="workflow_trigger.created", resource_type="workflow_trigger", resource_id=trigger.id,
        actor_type="human", actor_user_id=user.id,
        detail={"workflow_id": workflow.id, "name": trigger.name},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.created(trigger.to_dict(), "Workflow trigger created").to_response()


@agents_bp.route("/workflow-triggers/<int:trigger_id>", methods=["GET"])
@unified_auth_required
def get_workflow_trigger(trigger_id):
    """Get a single trigger."""
    user = get_current_user()
    trigger = WorkflowTrigger.query.filter_by(id=trigger_id, owner_id=user.id).first()
    if not trigger:
        return ApiResponse.not_found("Trigger not found").to_response()
    return ApiResponse.success(trigger.to_dict()).to_response()


@agents_bp.route("/workflow-triggers/<int:trigger_id>", methods=["PUT"])
@unified_auth_required
def update_workflow_trigger(trigger_id):
    """Update a trigger."""
    user = get_current_user()
    trigger = WorkflowTrigger.query.filter_by(id=trigger_id, owner_id=user.id).first()
    if not trigger:
        return ApiResponse.not_found("Trigger not found").to_response()

    data = validate_json_request(
        optional_fields=["name", "cron_expr", "one_shot_at", "is_active", "project_id", "root_task_id", "context_override"],
    )
    if isinstance(data, tuple):
        return data

    for field in ("name", "project_id", "root_task_id", "context_override"):
        if field in data:
            setattr(trigger, field, data[field])

    if "cron_expr" in data:
        trigger.cron_expr = data["cron_expr"]
        trigger.next_fire_at = _compute_next_fire(data["cron_expr"], datetime.utcnow()) if data["cron_expr"] else None

    if "one_shot_at" in data:
        try:
            trigger.one_shot_at = datetime.fromisoformat(data["one_shot_at"]) if data["one_shot_at"] else None
            if trigger.one_shot_at and not trigger.cron_expr:
                trigger.next_fire_at = trigger.one_shot_at
        except (ValueError, TypeError):
            return ApiResponse.error("Invalid one_shot_at format", 400).to_response()

    if "is_active" in data:
        trigger.is_active = data["is_active"]
        if trigger.is_active and not trigger.next_fire_at:
            if trigger.cron_expr:
                trigger.next_fire_at = _compute_next_fire(trigger.cron_expr, datetime.utcnow())
            elif trigger.one_shot_at:
                trigger.next_fire_at = trigger.one_shot_at

    db.session.commit()

    AuditLog.record(
        action="workflow_trigger.updated", resource_type="workflow_trigger", resource_id=trigger.id,
        actor_type="human", actor_user_id=user.id,
        detail={"updates": list(data.keys())},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.success(trigger.to_dict(), "Trigger updated").to_response()


@agents_bp.route("/workflow-triggers/<int:trigger_id>", methods=["DELETE"])
@unified_auth_required
def delete_workflow_trigger(trigger_id):
    """Delete a trigger."""
    user = get_current_user()
    trigger = WorkflowTrigger.query.filter_by(id=trigger_id, owner_id=user.id).first()
    if not trigger:
        return ApiResponse.not_found("Trigger not found").to_response()

    db.session.delete(trigger)
    db.session.commit()

    return ApiResponse.success(None, "Trigger deleted").to_response()


@agents_bp.route("/maintenance/fire-triggers", methods=["POST"])
@unified_auth_required
def fire_due_triggers():
    """Check all active triggers and fire those that are due.

    This endpoint is designed to be called periodically (e.g. via cron or
    external scheduler) to drive the workflow trigger system.
    """
    now = datetime.utcnow()
    due_triggers = WorkflowTrigger.query.filter(
        WorkflowTrigger.is_active == True,
        WorkflowTrigger.next_fire_at != None,
        WorkflowTrigger.next_fire_at <= now,
    ).all()

    fired = []
    for trigger in due_triggers:
        workflow = Workflow.query.get(trigger.workflow_id)
        if not workflow or not workflow.is_active:
            trigger.is_active = False
            continue

        # Create a WorkflowRun
        wf_run = WorkflowRun.create(
            workflow_id=workflow.id,
            owner_id=trigger.owner_id,
            project_id=trigger.project_id,
            root_task_id=trigger.root_task_id,
            status=WorkflowStatus.PENDING,
            context=trigger.context_override or {},
        )
        db.session.flush()

        # Create step runs from definition
        definition = workflow.definition or {}
        for step_def in definition.get("steps", []):
            WorkflowStepRun.create(
                run_id=wf_run.id,
                step_key=step_def.get("step_key", ""),
                status=StepStatus.PENDING,
            )

        # Advance the workflow
        wf_run.status = WorkflowStatus.RUNNING
        _advance_workflow(wf_run)

        trigger.fire_count = (trigger.fire_count or 0) + 1
        trigger.last_fired_at = now

        # Compute next fire time
        if trigger.cron_expr:
            trigger.next_fire_at = _compute_next_fire(trigger.cron_expr, now)
        elif trigger.one_shot_at:
            # One-shot: deactivate after firing
            trigger.is_active = False
            trigger.next_fire_at = None

        fired.append({
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "workflow_run_id": wf_run.id,
        })

        AuditLog.record(
            action="workflow_trigger.fired", resource_type="workflow_trigger", resource_id=trigger.id,
            actor_type="system",
            detail={"workflow_run_id": wf_run.id, "fire_count": trigger.fire_count},
        )

    db.session.commit()
    flush_sse_notifications()

    return ApiResponse.success(
        {"fired_count": len(fired), "fired": fired},
        f"Fired {len(fired)} trigger(s)",
    ).to_response()


# ---------------------------------------------------------------------------
# Agent Direct Messaging (peer-to-peer)
# ---------------------------------------------------------------------------


@agents_bp.route("/<int:from_agent_id>/message/<int:to_agent_id>", methods=["POST"])
@unified_auth_required
def send_agent_message(from_agent_id, to_agent_id):
    """Send a direct message from one Agent to another.

    The message is delivered via both SSE (real-time) and Notification
    (persistent) to the target Agent's owner.  The source Agent must be
    owned by the current user; the target Agent must also be owned by the
    same user.
    """
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=["content"],
            optional_fields=["task_id", "message_type", "metadata"],
        )
        if isinstance(data, tuple):
            return data

        from_agent = Agent.query.filter_by(id=from_agent_id, owner_id=current_user.id).first()
        if not from_agent:
            return ApiResponse.not_found("Source agent not found").to_response()

        to_agent = Agent.query.filter_by(id=to_agent_id, owner_id=current_user.id).first()
        if not to_agent:
            return ApiResponse.not_found("Target agent not found").to_response()

        if from_agent_id == to_agent_id:
            return ApiResponse.error("Cannot send message to self", 400).to_response()

        content = data["content"]
        message_type = data.get("message_type", "direct_message")
        task_id = data.get("task_id")
        metadata = data.get("metadata", {})

        # Record as a TaskEvent if task_id is provided
        if task_id:
            TaskEvent.create(
                task_id=task_id,
                event_type=f"agent.message.{message_type}",
                actor_type="agent",
                actor_agent_id=from_agent.id,
                payload={
                    "content": content,
                    "from_agent": {"id": from_agent.id, "name": from_agent.name},
                    "to_agent": {"id": to_agent.id, "name": to_agent.name},
                    "message_type": message_type,
                    "metadata": metadata,
                },
            )

        # Create persistent notification
        Notification.create(
            user_id=current_user.id,
            agent_id=to_agent.id,
            event_type=f"agent.direct_message",
            payload={
                "content": content,
                "from_agent": {"id": from_agent.id, "name": from_agent.name},
                "to_agent": {"id": to_agent.id, "name": to_agent.name},
                "message_type": message_type,
                "task_id": task_id,
                "metadata": metadata,
            },
            task_id=task_id,
        )

        # Queue SSE
        _queue_sse(current_user.id, "agent.direct_message", {
            "content": content,
            "from_agent": {"id": from_agent.id, "name": from_agent.name, "kind": from_agent.kind.value if from_agent.kind else None},
            "to_agent": {"id": to_agent.id, "name": to_agent.name, "kind": to_agent.kind.value if to_agent.kind else None},
            "message_type": message_type,
            "task_id": task_id,
            "metadata": metadata,
        })

        db.session.commit()

        AuditLog.record(
            action="agent.direct_message", resource_type="agent", resource_id=to_agent.id,
            actor_type="agent", actor_agent_id=from_agent.id,
            actor_user_id=current_user.id,
            detail={"message_type": message_type, "task_id": task_id, "content_length": len(content)},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.success(
            {"delivered": True, "to_agent_id": to_agent.id, "to_agent_name": to_agent.name},
            f"Message sent from {from_agent.name} to {to_agent.name}",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to send message: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/messages", methods=["GET"])
@unified_auth_required
def get_agent_messages(agent_id):
    """Get recent messages for an Agent (both sent and received).

    Returns notifications of type 'agent.direct_message' plus
    broadcast messages addressed to this Agent.
    """
    try:
        current_user = get_current_user()
        agent = Agent.query.filter_by(id=agent_id, owner_id=current_user.id).first()
        if not agent:
            return ApiResponse.not_found("Agent not found").to_response()

        args = get_request_args()
        query = Notification.query.filter_by(
            user_id=current_user.id,
            event_type="agent.direct_message",
        ).order_by(Notification.created_at.desc())

        # Filter messages involving this agent (either as sender or receiver)
        # Since payload is JSON, we use a broad filter and post-process
        all_notifications = query.limit(200).all()
        messages = []
        for n in all_notifications:
            payload = n.payload or {}
            from_a = payload.get("from_agent", {})
            to_a = payload.get("to_agent", {})
            if from_a.get("id") == agent_id or to_a.get("id") == agent_id:
                messages.append(n.to_dict())

        # Paginate manually
        page = args["page"]
        per_page = args["per_page"]
        start = (page - 1) * per_page
        end = start + per_page
        page_items = messages[start:end]

        result = {
            "items": page_items,
            "total": len(messages),
            "page": page,
            "per_page": per_page,
            "pages": max(1, (len(messages) + per_page - 1) // per_page),
        }

        return ApiResponse.success(result).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve messages: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/collaborators", methods=["GET"])
@unified_auth_required
def get_agent_collaborators(agent_id):
    """Aggregate the Agent's collaboration partners from direct-message
    audit logs. Returns the top partners this Agent has exchanged messages
    with (as either sender or receiver), with counts.

    Query params: limit (default 10, max 50).
    Returns: { collaborators: [{agent_id, name, sent, received, total}] }
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    # AuditLog rows where this agent is the sender (actor_agent_id) or the
    # receiver (resource_id) of an agent.direct_message action.
    sent_rows = AuditLog.query.filter(
        AuditLog.action == "agent.direct_message",
        AuditLog.actor_agent_id == agent_id,
        AuditLog.actor_user_id == user.id,
    ).all()
    recv_rows = AuditLog.query.filter(
        AuditLog.action == "agent.direct_message",
        AuditLog.resource_type == "agent",
        AuditLog.resource_id == agent_id,
        AuditLog.actor_user_id == user.id,
    ).all()

    counts = {}  # partner_id -> {sent, received}
    for r in sent_rows:
        pid = r.resource_id
        if pid is None or pid == agent_id:
            continue
        counts.setdefault(pid, {"sent": 0, "received": 0})["sent"] += 1
    for r in recv_rows:
        pid = r.actor_agent_id
        if pid is None or pid == agent_id:
            continue
        counts.setdefault(pid, {"sent": 0, "received": 0})["received"] += 1

    partner_ids = list(counts.keys())
    partners = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(partner_ids)).all()} if partner_ids else {}

    collaborators = [
        {
            "agent_id": pid,
            "name": partners.get(pid, f"Agent#{pid}"),
            "sent": c["sent"],
            "received": c["received"],
            "total": c["sent"] + c["received"],
        }
        for pid, c in counts.items()
    ]
    collaborators.sort(key=lambda x: x["total"], reverse=True)
    collaborators = collaborators[:limit]

    return ApiResponse.success(
        data={"collaborators": collaborators, "total_partners": len(counts)},
        message="Agent collaborators",
    ).to_response()


@agents_bp.route("/collaboration-graph", methods=["GET"])
@unified_auth_required
def collaboration_graph():
    """Platform-wide Agent collaboration graph derived from direct-message
    audit logs. Returns nodes (agents) and edges (message pairs with counts
    and directional breakdown), suitable for a force/radial graph
    visualization.

    Query params: limit (default 50, max 200) caps edges returned (top by
    count); since/until (ISO date/datetime, inclusive) filter the audit
    window. Nodes include only Agents that appear in the top edges.
    Returns: { nodes: [{id, name, kind, messages}],
               edges: [{source, target, count, source_to_target, target_to_source}],
               total_edges }
    """
    user = get_current_user()
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50

    q = AuditLog.query.filter(
        AuditLog.action == "agent.direct_message",
        AuditLog.resource_type == "agent",
        AuditLog.actor_user_id == user.id,
        AuditLog.actor_agent_id.isnot(None),
    )
    since = request.args.get("since")
    if since:
        q = q.filter(AuditLog.created_at >= since)
    until = request.args.get("until")
    if until:
        q = q.filter(AuditLog.created_at <= until)
    rows = q.all()

    # Undirected edge counts with directional breakdown.
    # edge_map[key] = {"total": N, "fwd": count(min->max), "rev": count(max->min)}
    edge_map = {}
    for r in rows:
        a, b = r.actor_agent_id, r.resource_id
        if a is None or b is None or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        entry = edge_map.setdefault(key, {"total": 0, "fwd": 0, "rev": 0})
        entry["total"] += 1
        # 正向：actor 是较小 id；反向：actor 是较大 id
        if r.actor_agent_id == key[0]:
            entry["fwd"] += 1
        else:
            entry["rev"] += 1

    edges_sorted = sorted(edge_map.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]
    node_ids = set()
    edges = []
    for (a, b), entry in edges_sorted:
        node_ids.add(a)
        node_ids.add(b)
        edges.append({
            "source": a,
            "target": b,
            "count": entry["total"],
            "source_to_target": entry["fwd"],
            "target_to_source": entry["rev"],
        })

    agents = Agent.query.filter(Agent.id.in_(list(node_ids))).all() if node_ids else []
    # 批量查 reputation，避免 N+1
    rep_map = {}
    if node_ids:
        reps = AgentReputation.query.filter(AgentReputation.agent_id.in_(list(node_ids))).all()
        rep_map = {r.agent_id: r.score for r in reps}
    # Per-node total messages (degree sum)
    degree = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + e["count"]
        degree[e["target"]] = degree.get(e["target"], 0) + e["count"]
    nodes = [
        {
            "id": a.id,
            "name": a.name,
            "kind": a.kind.value if a.kind else None,
            "messages": degree.get(a.id, 0),
            "reputation": rep_map.get(a.id),
        }
        for a in agents
    ]

    return ApiResponse.success(
        data={"nodes": nodes, "edges": edges, "total_edges": len(edge_map)},
        message="Collaboration graph",
    ).to_response()


# ---------------------------------------------------------------------------
# Workflow Template Marketplace
# ---------------------------------------------------------------------------


@agents_bp.route("/workflow-templates", methods=["GET"])
@unified_auth_required
def list_workflow_templates():
    """List all built-in workflow templates."""
    category = request.args.get("category")
    templates = WORKFLOW_TEMPLATES
    if category:
        templates = [t for t in templates if t.get("category") == category]
    return ApiResponse.success(
        [{"key": t["key"], "name": t["name"], "description": t["description"], "category": t["category"], "step_count": len(t["steps"])} for t in templates],
        "Workflow templates",
    ).to_response()


@agents_bp.route("/workflow-templates/<string:template_key>", methods=["GET"])
@unified_auth_required
def get_workflow_template(template_key):
    """Get a single template by key."""
    for t in WORKFLOW_TEMPLATES:
        if t["key"] == template_key:
            return ApiResponse.success(t).to_response()
    return ApiResponse.not_found("Template not found").to_response()


@agents_bp.route("/workflow-templates/<string:template_key>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_workflow_template(template_key):
    """Create a workflow from a built-in template."""
    user = get_current_user()
    data = validate_json_request(
        optional_fields=["name", "project_id", "root_task_id"],
    )
    if isinstance(data, tuple):
        return data

    for t in WORKFLOW_TEMPLATES:
        if t["key"] == template_key:
            wf_name = data.get("name", t["name"])
            steps_data = t["steps"]
            definition = {"steps": steps_data}
            wf = Workflow.create(
                owner_id=user.id,
                name=wf_name,
                description=t["description"],
                definition=definition,
                is_active=True,
                version=1,
            )
            for sd in steps_data:
                WorkflowStep.create(
                    workflow_id=wf.id,
                    step_key=sd["step_key"],
                    name=sd["name"],
                    order=sd["order"],
                    required_capabilities=sd.get("required_capabilities", []),
                    depends_on=sd.get("depends_on", []),
                    condition=sd.get("condition"),
                    sub_workflow_id=sd.get("sub_workflow_id"),
                    on_failure=sd.get("on_failure", "abort"),
                    retry_count=sd.get("retry_count", 0),
                )
            db.session.commit()

            AuditLog.record(
                action="workflow.instantiated_from_template", resource_type="workflow", resource_id=wf.id,
                actor_type="human", actor_user_id=user.id,
                detail={"template_key": template_key, "workflow_name": wf_name},
                ip_address=_client_ip(),
            )
            db.session.commit()

            return ApiResponse.created(wf.to_dict(), f"Workflow '{wf_name}' created from template '{t['name']}'").to_response()

    return ApiResponse.not_found("Template not found").to_response()


# =========================================================================
# Agent Collaboration Channels
# =========================================================================


@agents_bp.route("/channels", methods=["GET"])
@unified_auth_required
def list_channels():
    """List collaboration channels. Optional filters: project_id, task_id, is_active."""
    user = get_current_user()
    query = AgentChannel.query.filter_by(owner_id=user.id)

    project_id = request.args.get("project_id", type=int)
    task_id = request.args.get("task_id", type=int)
    is_active = request.args.get("is_active", type=str)

    if project_id:
        query = query.filter_by(project_id=project_id)
    if task_id:
        query = query.filter_by(task_id=task_id)
    if is_active is not None:
        query = query.filter_by(is_active=is_active.lower() == "true")

    channels = query.order_by(AgentChannel.updated_at.desc()).all()
    return ApiResponse.success(
        [c.to_dict(include_members=True, include_last_message=True) for c in channels],
    ).to_response()


@agents_bp.route("/channels", methods=["POST"])
@unified_auth_required
def create_channel():
    """Create a collaboration channel."""
    user = get_current_user()
    data = validate_json_request()

    name = data.get("name", "").strip()
    if not name:
        return ApiResponse.error("Channel name is required", 400).to_response()

    channel = AgentChannel.create(
        name=name,
        description=data.get("description", ""),
        project_id=data.get("project_id"),
        task_id=data.get("task_id"),
        owner_id=user.id,
    )

    # Auto-add creator's agents as members if specified
    agent_ids = data.get("agent_ids", [])
    for aid in agent_ids:
        agent = Agent.query.filter_by(id=aid, owner_id=user.id).first()
        if agent:
            AgentChannelMember.create(
                channel_id=channel.id,
                agent_id=agent.id,
                role="owner" if agent_ids.index(aid) == 0 else "member",
            )

    AuditLog.record("channel_create", target_type="channel", target_id=channel.id, user_id=user.id,
                     details={"name": name, "agent_count": len(agent_ids)})
    return ApiResponse.created(channel.to_dict(include_members=True), "Channel created").to_response()


@agents_bp.route("/channels/<int:channel_id>", methods=["GET"])
@unified_auth_required
def get_channel(channel_id):
    """Get channel details with members."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    return ApiResponse.success(channel.to_dict(include_members=True)).to_response()


@agents_bp.route("/channels/<int:channel_id>", methods=["PUT"])
@unified_auth_required
def update_channel(channel_id):
    """Update channel properties."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    data = validate_json_request()
    if "name" in data:
        channel.name = data["name"]
    if "description" in data:
        channel.description = data["description"]
    if "is_active" in data:
        channel.is_active = data["is_active"]

    db.session.commit()
    return ApiResponse.success(channel.to_dict(include_members=True), "Channel updated").to_response()


@agents_bp.route("/channels/<int:channel_id>", methods=["DELETE"])
@unified_auth_required
def delete_channel(channel_id):
    """Delete a channel."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    db.session.delete(channel)
    db.session.commit()
    AuditLog.record("channel_delete", target_type="channel", target_id=channel_id, user_id=user.id)
    return ApiResponse.success(None, "Channel deleted").to_response()


@agents_bp.route("/channels/<int:channel_id>/members", methods=["POST"])
@unified_auth_required
def add_channel_member(channel_id):
    """Add an Agent to a channel."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    data = validate_json_request()
    agent_id = data.get("agent_id")
    if not agent_id:
        return ApiResponse.error("agent_id is required", 400).to_response()

    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    existing = AgentChannelMember.query.filter_by(channel_id=channel_id, agent_id=agent_id).first()
    if existing:
        return ApiResponse.error("Agent is already a member", 409).to_response()

    member = AgentChannelMember.create(
        channel_id=channel_id,
        agent_id=agent_id,
        role=data.get("role", "member"),
    )
    db.session.commit()
    return ApiResponse.success(member.to_dict(), "Member added").to_response()


@agents_bp.route("/channels/<int:channel_id>/members/<int:member_id>", methods=["DELETE"])
@unified_auth_required
def remove_channel_member(channel_id, member_id):
    """Remove an Agent from a channel."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    member = AgentChannelMember.query.filter_by(id=member_id, channel_id=channel_id).first()
    if not member:
        return ApiResponse.not_found("Member not found").to_response()

    db.session.delete(member)
    db.session.commit()
    return ApiResponse.success(None, "Member removed").to_response()


@agents_bp.route("/channels/<int:channel_id>/messages", methods=["GET"])
@unified_auth_required
def list_channel_messages(channel_id):
    """List messages in a channel. Supports pagination."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    before_id = request.args.get("before_id", type=int)

    query = AgentChannelMessage.query.filter_by(channel_id=channel_id)
    if before_id:
        query = query.filter(AgentChannelMessage.id < before_id)

    messages = query.order_by(AgentChannelMessage.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    # Return in chronological order
    messages.reverse()

    return ApiResponse.success([m.to_dict() for m in messages]).to_response()


@agents_bp.route("/channels/<int:channel_id>/messages", methods=["POST"])
@unified_auth_required
def send_channel_message(channel_id):
    """Send a message to a channel (from an Agent or human)."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    data = validate_json_request()
    content = data.get("content", "").strip()
    if not content:
        return ApiResponse.error("Message content is required", 400).to_response()

    msg = AgentChannelMessage.create(
        channel_id=channel_id,
        sender_agent_id=data.get("agent_id"),
        sender_user_id=user.id if not data.get("agent_id") else None,
        content=content,
        message_type=data.get("message_type", "text"),
        metadata=data.get("metadata"),
    )

    # Deliver to all channel members via Notification + SSE
    for member in channel.members:
        if member.agent_id and member.agent_id != data.get("agent_id"):
            Notification.create(
                user_id=user.id,
                agent_id=member.agent_id,
                title=f"频道消息: {channel.name}",
                message=content[:200],
                category="channel_message",
                priority="info",
                task_id=channel.task_id,
                metadata={"channel_id": channel_id, "message_id": msg.id},
            )

    db.session.commit()
    return ApiResponse.created(msg.to_dict(), "Message sent").to_response()


# =========================================================================
# Collaboration Templates
# =========================================================================


# Built-in collaboration templates
_BUILTIN_COLLAB_TEMPLATES = [
    {
        "key": "code_review_squad",
        "name": "代码审查三人组",
        "description": "一个领导者协调代码审查流程，审查员执行代码审查，测试者负责测试验证。条件路由：审查通过则进入测试，审查失败则回退修复。",
        "category": "review",
        "agent_specs": [
            {"name": "审查协调者", "kind": "coordinator", "capabilities": ["coordination", "code_review"], "collaboration_role": "leader"},
            {"name": "代码审查员", "kind": "autonomous", "capabilities": ["code_review", "reading", "frontend", "backend"], "collaboration_role": "follower"},
            {"name": "测试验证员", "kind": "autonomous", "capabilities": ["testing", "qa"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "coord_review", "name": "协调审查任务", "required_capabilities": ["coordination", "code_review"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "code_review", "name": "执行代码审查", "required_capabilities": ["code_review"], "depends_on": ["coord_review"], "on_failure": "continue"},
            {"step_key": "test_on_pass", "name": "测试验证（审查通过）", "required_capabilities": ["testing"], "depends_on": ["code_review"], "on_failure": "skip",
             "condition": {"step_key": "code_review", "operator": "succeeded"}},
            {"step_key": "fix_on_fail", "name": "修复代码（审查失败）", "required_capabilities": ["code_review", "frontend"], "depends_on": ["code_review"], "on_failure": "continue",
             "condition": {"step_key": "code_review", "operator": "failed"}},
        ],
    },
    {
        "key": "research_team",
        "name": "研究小组",
        "description": "一个领导者分配研究任务，两个研究员并行调研不同方向，最后汇总。条件路由：任一方向失败时启动备选方案。",
        "category": "research",
        "agent_specs": [
            {"name": "研究主管", "kind": "coordinator", "capabilities": ["coordination", "research"], "collaboration_role": "leader"},
            {"name": "研究员 A", "kind": "autonomous", "capabilities": ["research", "reading"], "collaboration_role": "follower"},
            {"name": "研究员 B", "kind": "autonomous", "capabilities": ["research", "documentation"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "assign_research", "name": "分配研究任务", "required_capabilities": ["coordination", "research"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "research_a", "name": "研究方向 A", "required_capabilities": ["research", "reading"], "depends_on": ["assign_research"], "on_failure": "continue"},
            {"step_key": "research_b", "name": "研究方向 B", "required_capabilities": ["research", "documentation"], "depends_on": ["assign_research"], "on_failure": "continue"},
            {"step_key": "synthesis", "name": "汇总研究成果", "required_capabilities": ["coordination", "research"], "depends_on": ["research_a", "research_b"], "on_failure": "abort"},
            {"step_key": "fallback_a", "name": "方向 A 备选方案", "required_capabilities": ["research"], "depends_on": ["research_a"], "on_failure": "skip",
             "condition": {"step_key": "research_a", "operator": "failed"}},
            {"step_key": "fallback_b", "name": "方向 B 备选方案", "required_capabilities": ["documentation"], "depends_on": ["research_b"], "on_failure": "skip",
             "condition": {"step_key": "research_b", "operator": "failed"}},
        ],
    },
    {
        "key": "devops_pipeline",
        "name": "运维流水线",
        "description": "构建、部署、监控三阶段自动化运维团队。条件路由：构建失败时跳过部署直接通知，部署成功后启动监控。",
        "category": "devops",
        "agent_specs": [
            {"name": "运维协调者", "kind": "coordinator", "capabilities": ["coordination", "devops"], "collaboration_role": "leader"},
            {"name": "构建工程师", "kind": "autonomous", "capabilities": ["devops", "deployment", "backend"], "collaboration_role": "follower"},
            {"name": "监控工程师", "kind": "autonomous", "capabilities": ["devops", "security", "monitoring"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "coordinate", "name": "协调运维任务", "required_capabilities": ["coordination", "devops"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "build", "name": "构建", "required_capabilities": ["devops", "deployment"], "depends_on": ["coordinate"], "on_failure": "continue"},
            {"step_key": "deploy_on_success", "name": "部署（构建成功）", "required_capabilities": ["deployment"], "depends_on": ["build"], "on_failure": "continue",
             "condition": {"step_key": "build", "operator": "succeeded"}},
            {"step_key": "notify_on_build_fail", "name": "通知（构建失败）", "required_capabilities": ["coordination"], "depends_on": ["build"], "on_failure": "skip",
             "condition": {"step_key": "build", "operator": "failed"}},
            {"step_key": "monitor", "name": "监控", "required_capabilities": ["devops", "security"], "depends_on": ["deploy_on_success"], "on_failure": "continue",
             "condition": {"step_key": "deploy_on_success", "operator": "succeeded"}},
        ],
    },
    {
        "key": "bug_fix_pipeline",
        "name": "Bug 修复流水线",
        "description": "诊断、修复、验证三阶段 Bug 修复团队。条件路由：严重 Bug 直接分配高级工程师，普通 Bug 由常规工程师处理。",
        "category": "development",
        "agent_specs": [
            {"name": "Bug 协调者", "kind": "coordinator", "capabilities": ["coordination", "backend"], "collaboration_role": "leader"},
            {"name": "诊断工程师", "kind": "autonomous", "capabilities": ["backend", "testing"], "collaboration_role": "follower"},
            {"name": "修复工程师", "kind": "autonomous", "capabilities": ["backend", "frontend"], "collaboration_role": "follower"},
            {"name": "验证工程师", "kind": "autonomous", "capabilities": ["testing", "qa"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "triage", "name": "Bug 分诊", "required_capabilities": ["coordination"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "diagnose", "name": "诊断 Bug", "required_capabilities": ["backend", "testing"], "depends_on": ["triage"], "on_failure": "continue"},
            {"step_key": "fix", "name": "修复 Bug", "required_capabilities": ["backend"], "depends_on": ["diagnose"], "on_failure": "continue"},
            {"step_key": "verify", "name": "验证修复", "required_capabilities": ["testing", "qa"], "depends_on": ["fix"], "on_failure": "continue",
             "condition": {"step_key": "fix", "operator": "succeeded"}},
            {"step_key": "escalate", "name": "升级处理（修复失败）", "required_capabilities": ["coordination"], "depends_on": ["fix"], "on_failure": "skip",
             "condition": {"step_key": "fix", "operator": "failed"}},
        ],
    },
]


@agents_bp.route("/collaboration-templates", methods=["GET"])
@unified_auth_required
def list_collaboration_templates():
    """List collaboration templates (built-in + user-created)."""
    user = get_current_user()
    category = request.args.get("category")

    # Built-in templates
    builtin = _BUILTIN_COLLAB_TEMPLATES
    if category:
        builtin = [t for t in builtin if t["category"] == category]

    # User-created templates
    query = CollaborationTemplate.query.filter_by(owner_id=user.id)
    if category:
        query = query.filter_by(category=category)
    user_templates = query.order_by(CollaborationTemplate.name.asc()).all()

    result = [
        {"id": f"builtin:{t['key']}", "name": t["name"], "description": t["description"],
         "category": t["category"], "agent_specs": t["agent_specs"],
         "workflow_steps": t.get("workflow_steps", []),
         "is_builtin": True}
        for t in builtin
    ] + [t.to_dict() for t in user_templates]

    return ApiResponse.success(result).to_response()


@agents_bp.route("/collaboration-templates", methods=["POST"])
@unified_auth_required
def create_collaboration_template():
    """Create a custom collaboration template."""
    user = get_current_user()
    data = validate_json_request()

    name = (data.get("name") or "").strip()
    if not name:
        return ApiResponse.error("name is required", 400).to_response()

    agent_specs = data.get("agent_specs", [])
    if not agent_specs:
        return ApiResponse.error("agent_specs is required (at least 1 agent)", 400).to_response()

    template = CollaborationTemplate.create(
        owner_id=user.id,
        name=name,
        description=data.get("description", ""),
        category=data.get("category"),
        agent_specs=agent_specs,
        workflow_id=data.get("workflow_id"),
    )

    db.session.commit()
    return ApiResponse.created(template.to_dict(), "Collaboration template created").to_response()


@agents_bp.route("/collaboration-templates/<int:template_id>", methods=["DELETE"])
@unified_auth_required
def delete_collaboration_template(template_id):
    """Delete a user-created collaboration template."""
    user = get_current_user()
    template = CollaborationTemplate.query.filter_by(id=template_id, owner_id=user.id).first()
    if not template:
        return ApiResponse.not_found("Template not found").to_response()

    db.session.delete(template)
    db.session.commit()
    return ApiResponse.success(None, "Template deleted").to_response()


@agents_bp.route("/collaboration-templates/<string:template_key>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_collaboration_template(template_key):
    """Instantiate a collaboration template: create the agents and optionally launch the workflow.

    For built-in templates, template_key is like "builtin:code_review_squad".
    For user templates, template_key is the numeric ID.
    """
    user = get_current_user()
    data = validate_json_request() or {}
    project_id = data.get("project_id")

    # Resolve template
    if template_key.startswith("builtin:"):
        key = template_key.replace("builtin:", "")
        template_data = next((t for t in _BUILTIN_COLLAB_TEMPLATES if t["key"] == key), None)
        if not template_data:
            return ApiResponse.not_found("Built-in template not found").to_response()
        agent_specs = template_data["agent_specs"]
        workflow_steps_spec = template_data.get("workflow_steps", [])
        workflow_id = None
    else:
        try:
            tid = int(template_key)
        except ValueError:
            return ApiResponse.error("Invalid template key", 400).to_response()
        template = CollaborationTemplate.query.filter_by(id=tid, owner_id=user.id).first()
        if not template:
            return ApiResponse.not_found("Template not found").to_response()
        agent_specs = template.agent_specs or []
        workflow_id = template.workflow_id
        workflow_steps_spec = []

    # Create agents
    created_agents = []
    channel = None
    for spec in agent_specs:
        try:
            kind = parse_enum(AgentKind, spec.get("kind", "autonomous"), "kind")
        except ValueError:
            kind = AgentKind.AUTONOMOUS

        agent = Agent(
            owner_id=user.id,
            name=spec.get("name", f"Agent-{len(created_agents) + 1}"),
            description=f"Created from template: {template_key}",
            kind=kind,
            status=AgentStatus.ACTIVE,
            capabilities=spec.get("capabilities", []),
            collaboration_role=spec.get("collaboration_role", "standalone"),
            provider=spec.get("provider"),
            model=spec.get("model"),
            last_seen_at=datetime.utcnow(),
            created_by=user.email,
        )
        db.session.add(agent)
        db.session.flush()  # get the ID
        created_agents.append(agent)

    db.session.commit()

    # Create a collaboration channel for the team
    if len(created_agents) >= 2:
        channel = AgentChannel.create(
            name=f"团队: {template_key}",
            description=f"从模板 {template_key} 创建的协作频道",
            project_id=project_id,
            owner_id=user.id,
        )
        for i, agent in enumerate(created_agents):
            AgentChannelMember.create(
                channel_id=channel.id,
                agent_id=agent.id,
                role="owner" if i == 0 else "member",
            )
        db.session.commit()

    # Optionally launch workflow
    wf_run = None
    if workflow_id and project_id:
        wf = Workflow.query.filter_by(id=workflow_id, owner_id=user.id).first()
        if wf:
            wf_run = WorkflowRun.create(
                workflow_id=wf.id,
                root_task_id=None,
                project_id=project_id,
                owner_id=user.id,
                status=WorkflowStatus.PENDING,
            )
            # Create step runs
            for step in wf.steps:
                WorkflowStepRun.create(
                    run_id=wf_run.id,
                    step_key=step.step_key,
                    status=StepStatus.PENDING,
                )
            db.session.commit()
            _advance_workflow(wf_run)
            db.session.commit()
    elif workflow_steps_spec and project_id:
        # Built-in template with workflow steps: auto-create workflow definition
        wf = Workflow.create(
            owner_id=user.id,
            name=f"模板工作流: {template_data.get('name', template_key)}",
            description=template_data.get('description', ''),
            project_id=project_id,
            max_parallel_steps=3,
        )
        for step_spec in workflow_steps_spec:
            # Try to match agent by capability
            agent_id = None
            req_caps = step_spec.get("required_capabilities", [])
            for agent in created_agents:
                agent_caps = set(agent.capabilities or [])
                if set(req_caps).intersection(agent_caps):
                    agent_id = agent.id
                    break

            WorkflowStep.create(
                workflow_id=wf.id,
                step_key=step_spec["step_key"],
                name=step_spec.get("name", step_spec["step_key"]),
                required_capabilities=req_caps,
                depends_on=step_spec.get("depends_on", []),
                on_failure=step_spec.get("on_failure", "abort"),
                agent_id=agent_id,
                condition=step_spec.get("condition"),
            )
        db.session.commit()

        # Launch the workflow
        wf_run = WorkflowRun.create(
            workflow_id=wf.id,
            root_task_id=None,
            project_id=project_id,
            owner_id=user.id,
            status=WorkflowStatus.PENDING,
        )
        for step in wf.steps:
            WorkflowStepRun.create(
                run_id=wf_run.id,
                step_key=step.step_key,
                status=StepStatus.PENDING,
            )
        db.session.commit()
        _advance_workflow(wf_run)
        db.session.commit()

    AuditLog.record("collaboration_template_instantiate", target_type="template",
                     target_id=0, actor_type="human", actor_user_id=user.id,
                     detail={"template_key": template_key, "agents_created": len(created_agents),
                             "channel_id": channel.id if channel else None,
                             "workflow_run_id": wf_run.id if wf_run else None},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.success({
        "agents": [a.to_dict() for a in created_agents],
        "channel": channel.to_dict(include_members=True) if channel else None,
        "workflow_run": wf_run.to_dict(include_step_runs=True) if wf_run else None,
    }, f"Instantiated template: {len(created_agents)} agent(s) created").to_response()


# =========================================================================
# Agent Knowledge Base
# =========================================================================


@agents_bp.route("/<int:agent_id>/knowledge", methods=["GET"])
@unified_auth_required
def list_knowledge_entries(agent_id):
    """List knowledge entries for an Agent, with optional filters."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    query = KnowledgeEntry.query.filter_by(agent_id=agent_id, is_valid=True)

    domain = request.args.get("domain")
    if domain:
        query = query.filter_by(domain=domain)

    entry_type = request.args.get("entry_type")
    if entry_type:
        query = query.filter_by(entry_type=entry_type)

    source_type = request.args.get("source_type")
    if source_type:
        query = query.filter_by(source_type=source_type)

    tag = request.args.get("tag")
    if tag:
        # Filter by tag in JSON array
        query = query.filter(KnowledgeEntry.tags.contains([tag]))

    search = request.args.get("search", "").strip()
    if search:
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(f"%{search}%"),
                KnowledgeEntry.content.ilike(f"%{search}%"),
            )
        )

    include_content = request.args.get("include_content", "true").lower() == "true"
    query = query.order_by(KnowledgeEntry.updated_at.desc())
    result = paginate_query(query, default_per_page=50)

    entries = [e.to_dict(include_content=include_content) for e in result.items]
    return ApiResponse.success({
        "items": entries,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
    }).to_response()


@agents_bp.route("/<int:agent_id>/knowledge", methods=["POST"])
@unified_auth_required
def create_knowledge_entry(agent_id):
    """Create a knowledge entry for an Agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request()
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return ApiResponse.error("title and content are required", 400).to_response()

    entry = KnowledgeEntry.create(
        agent_id=agent_id,
        title=title,
        content=content,
        domain=data.get("domain"),
        tags=data.get("tags", []),
        entry_type=data.get("entry_type", "insight"),
        source_task_id=data.get("source_task_id"),
        source_type=data.get("source_type", "manual"),
        confidence=data.get("confidence", 1.0),
        shared_with_project=data.get("shared_with_project", False),
        project_id=data.get("project_id"),
    )
    db.session.commit()

    AuditLog.record("knowledge_entry_create", target_type="knowledge_entry",
                     target_id=entry.id, actor_type="human", actor_user_id=user.id,
                     detail={"agent_id": agent_id, "title": title, "domain": data.get("domain")},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.created(entry.to_dict(), "Knowledge entry created").to_response()


@agents_bp.route("/<int:agent_id>/knowledge/<int:entry_id>", methods=["GET"])
@unified_auth_required
def get_knowledge_entry(agent_id, entry_id):
    """Get a specific knowledge entry."""
    user = get_current_user()
    entry = KnowledgeEntry.query.filter_by(id=entry_id, agent_id=agent_id).first()
    if not entry:
        return ApiResponse.not_found("Knowledge entry not found").to_response()

    # Access control: owner or shared with project
    agent = Agent.query.filter_by(id=agent_id).first()
    if agent and agent.owner_id != user.id:
        if not entry.shared_with_project or not entry.project_id:
            return ApiResponse.not_found("Knowledge entry not found").to_response()
        # Check project membership
        pm = ProjectMember.query.filter_by(project_id=entry.project_id, user_id=user.id).first()
        if not pm:
            return ApiResponse.not_found("Knowledge entry not found").to_response()

    # Increment access count
    entry.access_count = (entry.access_count or 0) + 1
    db.session.commit()

    return ApiResponse.success(entry.to_dict()).to_response()


@agents_bp.route("/<int:agent_id>/knowledge/<int:entry_id>", methods=["PUT"])
@unified_auth_required
def update_knowledge_entry(agent_id, entry_id):
    """Update a knowledge entry."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    entry = KnowledgeEntry.query.filter_by(id=entry_id, agent_id=agent_id).first()
    if not entry:
        return ApiResponse.not_found("Knowledge entry not found").to_response()

    data = validate_json_request()
    if "title" in data:
        entry.title = data["title"]
    if "content" in data:
        entry.content = data["content"]
    if "domain" in data:
        entry.domain = data["domain"]
    if "tags" in data:
        entry.tags = data["tags"]
    if "entry_type" in data:
        entry.entry_type = data["entry_type"]
    if "confidence" in data:
        entry.confidence = data["confidence"]
    if "is_valid" in data:
        entry.is_valid = data["is_valid"]
    if "shared_with_project" in data:
        entry.shared_with_project = data["shared_with_project"]
    if "project_id" in data:
        entry.project_id = data["project_id"]

    db.session.commit()
    return ApiResponse.success(entry.to_dict(), "Knowledge entry updated").to_response()


@agents_bp.route("/<int:agent_id>/knowledge/<int:entry_id>", methods=["DELETE"])
@unified_auth_required
def delete_knowledge_entry(agent_id, entry_id):
    """Delete (invalidate) a knowledge entry."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    entry = KnowledgeEntry.query.filter_by(id=entry_id, agent_id=agent_id).first()
    if not entry:
        return ApiResponse.not_found("Knowledge entry not found").to_response()

    # Soft delete — mark as invalid instead of removing
    entry.is_valid = False
    db.session.commit()
    return ApiResponse.success(None, "Knowledge entry deleted").to_response()


@agents_bp.route("/<int:agent_id>/knowledge/search", methods=["GET"])
@unified_auth_required
def search_knowledge(agent_id):
    """Search knowledge entries by query string, domain, or tags."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    # Access control: must be owner or shared project member
    if agent.owner_id != user.id:
        return ApiResponse.error("Access denied", 403).to_response()

    q = request.args.get("q", "").strip()
    domain = request.args.get("domain")
    tags = request.args.get("tags", "")  # comma-separated
    limit = min(int(request.args.get("limit", 20)), 100)
    entry_type = request.args.get("entry_type")

    query = KnowledgeEntry.query.filter_by(agent_id=agent_id, is_valid=True)

    if domain:
        query = query.filter_by(domain=domain)
    if entry_type:
        query = query.filter_by(entry_type=entry_type)
    if tags:
        for tag in tags.split(","):
            tag = tag.strip()
            if tag:
                query = query.filter(KnowledgeEntry.tags.contains([tag]))
    if q:
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(f"%{q}%"),
                KnowledgeEntry.content.ilike(f"%{q}%"),
            )
        )

    entries = query.order_by(KnowledgeEntry.confidence.desc(), KnowledgeEntry.access_count.desc()).limit(limit).all()
    return ApiResponse.success([e.to_dict() for e in entries]).to_response()


@agents_bp.route("/knowledge/shared", methods=["GET"])
@unified_auth_required
def list_shared_knowledge():
    """List knowledge entries shared with projects the user is a member of."""
    user = get_current_user()
    project_ids = [pm.project_id for pm in ProjectMember.query.filter_by(user_id=user.id).all()]

    if not project_ids:
        return ApiResponse.success([]).to_response()

    domain = request.args.get("domain")
    entry_type = request.args.get("entry_type")
    search = request.args.get("search", "").strip()

    query = KnowledgeEntry.query.filter(
        KnowledgeEntry.shared_with_project == True,
        KnowledgeEntry.is_valid == True,
        KnowledgeEntry.project_id.in_(project_ids),
    )
    if domain:
        query = query.filter_by(domain=domain)
    if entry_type:
        query = query.filter_by(entry_type=entry_type)
    if search:
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(f"%{search}%"),
                KnowledgeEntry.content.ilike(f"%{search}%"),
            )
        )

    query = query.order_by(KnowledgeEntry.updated_at.desc())
    result = paginate_query(query, default_per_page=50)
    entries = [e.to_dict(include_content=False) for e in result.items]
    return ApiResponse.success({
        "items": entries,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
    }).to_response()


@agents_bp.route("/<int:agent_id>/knowledge/auto-extract", methods=["POST"])
@unified_auth_required
def auto_extract_knowledge(agent_id):
    """Auto-extract knowledge from completed task assignments for an Agent.

    Reviews the Agent's recently completed tasks and generates knowledge
    entries from task summaries and results.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request() or {}
    limit = min(data.get("limit", 10), 50)

    # Find recently completed assignments with output summaries
    recent_assignments = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent_id,
        TaskAssignment.state == TaskAssignmentState.DONE,
        TaskAssignment.output_summary.isnot(None),
        TaskAssignment.output_summary != "",
    ).order_by(TaskAssignment.updated_at.desc()).limit(limit).all()

    created = []
    for assignment in recent_assignments:
        task = Task.query.get(assignment.task_id) if assignment.task_id else None
        # Check if we already have a knowledge entry for this task
        existing = KnowledgeEntry.query.filter_by(
            agent_id=agent_id,
            source_task_id=assignment.task_id,
            source_type="auto_extracted",
        ).first()
        if existing:
            continue

        title = f"经验: {task.title if task else f'任务 #{assignment.task_id}'}"
        content_parts = []
        if task:
            content_parts.append(f"任务: {task.title}")
            content_parts.append(f"描述: {task.description or '无'}")
        content_parts.append(f"执行摘要: {assignment.output_summary}")
        if assignment.notes:
            content_parts.append(f"备注: {assignment.notes}")

        # Infer domain from task tags
        domain = None
        if task and task.tags:
            task_tags = task.tags if isinstance(task.tags, list) else []
            if task_tags:
                domain = task_tags[0]

        entry = KnowledgeEntry.create(
            agent_id=agent_id,
            title=title,
            content="\n\n".join(content_parts),
            domain=domain,
            tags=task.tags if task and task.tags else [],
            entry_type="insight",
            source_task_id=assignment.task_id,
            source_type="auto_extracted",
            confidence=0.7,  # Auto-extracted starts with lower confidence
        )
        created.append(entry)

    db.session.commit()

    AuditLog.record("knowledge_auto_extract", target_type="agent",
                     target_id=agent_id, actor_type="human", actor_user_id=user.id,
                     detail={"entries_created": len(created)},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.success({
        "entries_created": len(created),
        "entries": [e.to_dict() for e in created],
    }, f"Auto-extracted {len(created)} knowledge entries").to_response()


# =========================================================================
# Workflow Version Management
# =========================================================================


@agents_bp.route("/workflows/<int:workflow_id>/versions", methods=["GET"])
@unified_auth_required
def list_workflow_versions(workflow_id):
    """List version history for a workflow."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    versions = WorkflowVersion.query.filter_by(workflow_id=workflow_id).order_by(
        WorkflowVersion.version_number.desc()
    ).all()
    return ApiResponse.success({
        "current_version": workflow.version,
        "versions": [v.to_dict() for v in versions],
    }).to_response()


@agents_bp.route("/workflows/<int:workflow_id>/versions/<int:version_number>", methods=["GET"])
@unified_auth_required
def get_workflow_version(workflow_id, version_number):
    """Get a specific workflow version snapshot."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    version = WorkflowVersion.query.filter_by(
        workflow_id=workflow_id, version_number=version_number
    ).first()
    if not version:
        return ApiResponse.not_found("Version not found").to_response()

    return ApiResponse.success(version.to_dict()).to_response()


@agents_bp.route("/workflows/<int:workflow_id>/rollback", methods=["POST"])
@unified_auth_required
def rollback_workflow(workflow_id):
    """Rollback a workflow to a specific version.

    Creates a snapshot of the current version before rolling back.
    """
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    data = validate_json_request()
    target_version = data.get("version")
    if not target_version:
        return ApiResponse.error("version is required", 400).to_response()

    target = WorkflowVersion.query.filter_by(
        workflow_id=workflow_id, version_number=target_version
    ).first()
    if not target:
        return ApiResponse.not_found(f"Version {target_version} not found").to_response()

    # Snapshot current before rollback
    current_version = workflow.version or 1
    WorkflowVersion.create(
        workflow_id=workflow.id,
        version_number=current_version,
        definition=workflow.definition or {},
        steps_snapshot=[s.to_dict() for s in workflow.steps],
        change_summary=f"Auto-snapshot before rollback (v{current_version} → v{target_version})",
        created_by=user.email,
    )

    # Apply target version
    workflow.version = current_version + 1  # New version number
    workflow.definition = target.definition or {}

    # Replace steps with the snapshot
    for old_step in workflow.steps:
        db.session.delete(old_step)
    for step_data in (target.steps_snapshot or []):
        step_key = step_data.get("step_key", "").strip()
        if not step_key:
            continue
        WorkflowStep.create(
            workflow_id=workflow.id,
            step_key=step_key,
            name=step_data.get("name", step_key),
            description=step_data.get("description", ""),
            order=step_data.get("order", 0),
            required_capabilities=step_data.get("required_capabilities", []),
            agent_id=step_data.get("agent_id"),
            task_template_id=step_data.get("task_template_id"),
            depends_on=step_data.get("depends_on", []),
            condition=step_data.get("condition"),
            sub_workflow_id=step_data.get("sub_workflow_id"),
            timeout_seconds=step_data.get("timeout_seconds"),
            retry_count=step_data.get("retry_count", 0),
            on_failure=step_data.get("on_failure", "abort"),
        )

    db.session.commit()

    AuditLog.record("workflow_rollback", target_type="workflow",
                     target_id=workflow.id, actor_type="human", actor_user_id=user.id,
                     detail={"from_version": current_version, "to_version": target_version,
                             "new_version_number": workflow.version},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.success(
        workflow.to_dict(include_steps=True),
        f"Rolled back to version {target_version} (now at v{workflow.version})"
    ).to_response()


@agents_bp.route("/workflows/<int:workflow_id>/diff/<int:v1>/<int:v2>", methods=["GET"])
@unified_auth_required
def diff_workflow_versions(workflow_id, v1, v2):
    """Compare two versions of a workflow. Returns a summary of differences."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    ver1 = WorkflowVersion.query.filter_by(workflow_id=workflow_id, version_number=v1).first()
    ver2 = WorkflowVersion.query.filter_by(workflow_id=workflow_id, version_number=v2).first()
    if not ver1 or not ver2:
        return ApiResponse.not_found("One or both versions not found").to_response()

    # Compute step-level diff
    steps1 = {s["step_key"]: s for s in (ver1.steps_snapshot or [])}
    steps2 = {s["step_key"]: s for s in (ver2.steps_snapshot or [])}

    added = [k for k in steps2 if k not in steps1]
    removed = [k for k in steps1 if k not in steps2]
    modified = []
    for k in steps1:
        if k in steps2 and steps1[k] != steps2[k]:
            modified.append(k)

    return ApiResponse.success({
        "v1": v1,
        "v2": v2,
        "added_steps": added,
        "removed_steps": removed,
        "modified_steps": modified,
        "v1_definition": ver1.definition,
        "v2_definition": ver2.definition,
        "v1_change_summary": ver1.change_summary,
        "v2_change_summary": ver2.change_summary,
    }).to_response()


# =========================================================================
# Collaboration Protocols (Proposal / Vote / Consensus / Auction / Handoff)
# =========================================================================


@agents_bp.route("/protocols", methods=["GET"])
@unified_auth_required
def list_protocols():
    """List collaboration protocols with optional filters."""
    user = get_current_user()
    query = CollaborationProtocol.query

    # Filter by project
    project_id = request.args.get("project_id", type=int)
    if project_id:
        query = query.filter_by(project_id=project_id)

    # Filter by status
    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status)

    # Filter by type
    protocol_type = request.args.get("protocol_type")
    if protocol_type:
        query = query.filter_by(protocol_type=protocol_type)

    # Filter by initiator
    initiator_id = request.args.get("initiator_agent_id", type=int)
    if initiator_id:
        query = query.filter_by(initiator_agent_id=initiator_id)

    # Only show protocols for agents owned by this user
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).all()]
    if agent_ids:
        query = query.filter(
            db.or_(
                CollaborationProtocol.initiator_agent_id.in_(agent_ids),
                CollaborationProtocol.project_id.in_(
                    [pm.project_id for pm in ProjectMember.query.filter_by(user_id=user.id).all()]
                ),
            )
        )

    query = query.order_by(CollaborationProtocol.created_at.desc())
    result = paginate_query(query, default_per_page=30)
    protocols = [p.to_dict() for p in result.items]
    return ApiResponse.success({
        "items": protocols,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
    }).to_response()


@agents_bp.route("/protocols", methods=["POST"])
@unified_auth_required
def create_protocol():
    """Create a new collaboration protocol (proposal, vote, consensus, auction, or handoff)."""
    user = get_current_user()
    data = validate_json_request()

    protocol_type = (data.get("protocol_type") or "").strip()
    if protocol_type not in [e.value for e in ProtocolType]:
        return ApiResponse.error(f"Invalid protocol_type. Must be one of: {', '.join(e.value for e in ProtocolType)}", 400).to_response()

    title = (data.get("title") or "").strip()
    if not title:
        return ApiResponse.error("title is required", 400).to_response()

    initiator_agent_id = data.get("initiator_agent_id")
    if not initiator_agent_id:
        return ApiResponse.error("initiator_agent_id is required", 400).to_response()

    # Verify the initiator agent belongs to this user
    agent = Agent.query.filter_by(id=initiator_agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.error("Initiator agent not found or not owned by you", 400).to_response()

    deadline = None
    if data.get("deadline"):
        try:
            deadline = datetime.fromisoformat(data["deadline"])
        except (ValueError, TypeError):
            return ApiResponse.error("Invalid deadline format (use ISO 8601)", 400).to_response()

    protocol = CollaborationProtocol.create(
        protocol_type=protocol_type,
        status=ProtocolStatus.OPEN.value,
        title=title,
        description=data.get("description", ""),
        initiator_agent_id=initiator_agent_id,
        channel_id=data.get("channel_id"),
        project_id=data.get("project_id"),
        task_id=data.get("task_id"),
        config=data.get("config", {}),
        deadline=deadline,
    )
    db.session.commit()

    # Notify channel members if channel is specified
    if protocol.channel_id:
        members = AgentChannelMember.query.filter_by(channel_id=protocol.channel_id).all()
        for member in members:
            if member.agent_id != initiator_agent_id:
                Notification.create(
                    agent_id=member.agent_id,
                    event_type="protocol_created",
                    title=f"新协议: {title}",
                    message=f"{agent.name} 发起了 {protocol_type} 协议: {title}",
                    payload={"protocol_id": protocol.id, "protocol_type": protocol_type},
                )
        db.session.commit()

    return ApiResponse.created(protocol.to_dict(), "Protocol created").to_response()


@agents_bp.route("/protocols/<int:protocol_id>", methods=["GET"])
@unified_auth_required
def get_protocol(protocol_id):
    """Get a protocol with its messages."""
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    return ApiResponse.success(protocol.to_dict(include_messages=True)).to_response()


@agents_bp.route("/protocols/<int:protocol_id>/respond", methods=["POST"])
@unified_auth_required
def respond_to_protocol(protocol_id):
    """Respond to a protocol (vote, bid, accept, reject, counter-proposal, comment)."""
    user = get_current_user()
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    if protocol.status not in (ProtocolStatus.OPEN.value, ProtocolStatus.VOTING.value):
        return ApiResponse.error(f"Protocol is {protocol.status}, cannot respond", 400).to_response()

    # Check deadline
    if protocol.deadline and datetime.utcnow() > protocol.deadline:
        protocol.status = ProtocolStatus.EXPIRED.value
        db.session.commit()
        return ApiResponse.error("Protocol has expired", 400).to_response()

    data = validate_json_request()
    agent_id = data.get("agent_id")
    if not agent_id:
        return ApiResponse.error("agent_id is required", 400).to_response()

    # Verify agent ownership
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.error("Agent not found or not owned by you", 400).to_response()

    message_type = (data.get("message_type") or "").strip()
    valid_types = ["vote", "bid", "accept", "reject", "comment", "counter_proposal"]
    if message_type not in valid_types:
        return ApiResponse.error(f"Invalid message_type. Must be one of: {', '.join(valid_types)}", 400).to_response()

    msg = ProtocolMessage.create(
        protocol_id=protocol_id,
        agent_id=agent_id,
        message_type=message_type,
        content=data.get("content", ""),
        payload=data.get("payload", {}),
    )

    # Auto-resolve logic based on protocol type
    _try_resolve_protocol(protocol)

    db.session.commit()
    return ApiResponse.created(msg.to_dict(), "Response recorded").to_response()


@agents_bp.route("/protocols/<int:protocol_id>/resolve", methods=["POST"])
@unified_auth_required
def resolve_protocol(protocol_id):
    """Manually resolve a protocol (force accept/reject/cancel)."""
    user = get_current_user()
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    data = validate_json_request()
    resolution = (data.get("resolution") or "").strip()
    if resolution not in ("accepted", "rejected", "cancelled"):
        return ApiResponse.error("resolution must be accepted, rejected, or cancelled", 400).to_response()

    protocol.status = resolution
    protocol.resolved_at = datetime.utcnow()
    protocol.result = data.get("result", {"manual_resolution": resolution})

    db.session.commit()
    return ApiResponse.success(protocol.to_dict(), f"Protocol {resolution}").to_response()


@agents_bp.route("/protocols/analytics", methods=["GET"])
@unified_auth_required
def protocol_analytics():
    """Analytics for collaboration protocols: usage, resolution rates, participation.

    Query params:
      days – look-back window (default 30)
    """
    user = get_current_user()
    args = get_request_args()
    days = args.get("days", 30, type=int)
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))

    # Get user's protocols (via initiator agent ownership)
    user_agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).all()]
    if not user_agent_ids:
        return ApiResponse.success({
            "total_protocols": 0,
            "by_type": {},
            "by_status": {},
            "resolution_rate": 0,
        }).to_response()

    protocols = CollaborationProtocol.query.filter(
        CollaborationProtocol.initiator_agent_id.in_(user_agent_ids),
        CollaborationProtocol.created_at >= since,
    ).all()

    by_type = {}
    by_status = {}
    resolved_count = 0
    total_messages = 0
    participation = {}  # agent_id -> message count

    for p in protocols:
        by_type[p.protocol_type] = by_type.get(p.protocol_type, 0) + 1
        by_status[p.status] = by_status.get(p.status, 0) + 1
        if p.status in ("accepted", "rejected"):
            resolved_count += 1

        # Count messages
        msgs = ProtocolMessage.query.filter_by(protocol_id=p.id).all()
        total_messages += len(msgs)
        for m in msgs:
            participation[m.agent_id] = participation.get(m.agent_id, 0) + 1

    # Top participants
    top_participants = sorted(participation.items(), key=lambda x: -x[1])[:10]
    top_participants_data = []
    for aid, count in top_participants:
        agent = Agent.query.get(aid)
        if agent:
            top_participants_data.append({
                "agent_id": aid,
                "agent_name": agent.name,
                "message_count": count,
            })

    resolution_rate = round(resolved_count / len(protocols) * 100, 1) if protocols else 0
    avg_messages = round(total_messages / len(protocols), 1) if protocols else 0

    return ApiResponse.success({
        "window_days": days,
        "total_protocols": len(protocols),
        "by_type": by_type,
        "by_status": by_status,
        "resolved_count": resolved_count,
        "resolution_rate": resolution_rate,
        "total_messages": total_messages,
        "avg_messages_per_protocol": avg_messages,
        "top_participants": top_participants_data,
    }).to_response()


@agents_bp.route("/protocols/<int:protocol_id>/deliberate", methods=["POST"])
@unified_auth_required
def add_deliberation_message(protocol_id):
    """Add a deliberation message (argument/evidence/comment) to a deliberation protocol.

    For DELIBERATION type protocols, this contributes to the required
    discussion before final voting can resolve.
    """
    user = get_current_user()
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    if protocol.protocol_type != ProtocolType.DELIBERATION.value:
        return ApiResponse.error("This endpoint is only for deliberation protocols").to_response()

    if protocol.status != "open":
        return ApiResponse.error("Protocol is no longer open for deliberation").to_response()

    data = validate_json_request()
    agent_id = data.get("agent_id")
    message_type = data.get("message_type", "comment")
    if message_type not in ("comment", "argument", "evidence"):
        return ApiResponse.error("message_type must be comment, argument, or evidence").to_response()

    # Verify agent ownership
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    msg = ProtocolMessage.create(
        protocol_id=protocol_id,
        agent_id=agent_id,
        message_type=message_type,
        content=data.get("content", ""),
        payload=data.get("payload", {}),
    )
    db.session.commit()

    # Try to resolve after adding deliberation message
    _try_resolve_protocol(protocol)
    db.session.commit()

    notify_sse("protocol_deliberation", {
        "protocol_id": protocol_id,
        "agent_id": agent_id,
        "message_type": message_type,
    })
    return ApiResponse.success(msg.to_dict(), "Deliberation message added").to_response()


def _try_resolve_protocol(protocol):
    """Auto-resolve a protocol if conditions are met.

    - Proposal: accepted if any 'accept', rejected if any 'reject'
    - Vote: resolved when quorum reached (config.quorum or simple majority)
    - Consensus: accepted only if all participants accept
    - Auction: resolved at deadline or when no new bids
    - Handoff: accepted when target agent accepts
    """
    messages = ProtocolMessage.query.filter_by(protocol_id=protocol.id).all()
    config = protocol.config or {}

    if protocol.protocol_type == ProtocolType.PROPOSAL.value:
        accepts = [m for m in messages if m.message_type == "accept"]
        rejects = [m for m in messages if m.message_type == "reject"]
        if accepts:
            protocol.status = ProtocolStatus.ACCEPTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"accepted_by": [m.agent_id for m in accepts]}
        elif rejects:
            protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"rejected_by": [m.agent_id for m in rejects]}

    elif protocol.protocol_type == ProtocolType.VOTE.value:
        votes_for = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "for"]
        votes_against = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "against"]
        quorum = config.get("quorum", 2)
        if len(votes_for) + len(votes_against) >= quorum:
            if len(votes_for) > len(votes_against):
                protocol.status = ProtocolStatus.ACCEPTED.value
            else:
                protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"votes_for": len(votes_for), "votes_against": len(votes_against)}

    elif protocol.protocol_type == ProtocolType.CONSENSUS.value:
        # Get all participants (channel members or specified in config)
        participant_ids = config.get("participant_agent_ids", [])
        if not participant_ids and protocol.channel_id:
            participant_ids = [m.agent_id for m in AgentChannelMember.query.filter_by(channel_id=protocol.channel_id).all()]
        if not participant_ids:
            return

        accepts = {m.agent_id for m in messages if m.message_type == "accept"}
        rejects = {m.agent_id for m in messages if m.message_type == "reject"}

        if rejects & set(participant_ids):
            protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"rejected_by": list(rejects & set(participant_ids))}
        elif accepts.issuperset(set(participant_ids)):
            protocol.status = ProtocolStatus.ACCEPTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"accepted_by": list(accepts)}

    elif protocol.protocol_type == ProtocolType.HANDOFF.value:
        accepts = [m for m in messages if m.message_type == "accept"]
        if accepts:
            protocol.status = ProtocolStatus.ACCEPTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"accepted_by": accepts[0].agent_id}

    elif protocol.protocol_type == ProtocolType.WEIGHTED_VOTE.value:
        # Vote weighted by agent reputation score
        votes_for = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "for"]
        votes_against = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "against"]
        quorum = config.get("quorum", 2)

        if len(votes_for) + len(votes_against) >= quorum:
            weighted_for = 0.0
            weighted_against = 0.0
            vote_breakdown = []
            for m in votes_for:
                rep = AgentReputation.query.filter_by(agent_id=m.agent_id).first()
                weight = rep.score if rep else 50.0
                weighted_for += weight
                vote_breakdown.append({"agent_id": m.agent_id, "choice": "for", "weight": weight})
            for m in votes_against:
                rep = AgentReputation.query.filter_by(agent_id=m.agent_id).first()
                weight = rep.score if rep else 50.0
                weighted_against += weight
                vote_breakdown.append({"agent_id": m.agent_id, "choice": "against", "weight": weight})

            if weighted_for > weighted_against:
                protocol.status = ProtocolStatus.ACCEPTED.value
            else:
                protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {
                "weighted_for": round(weighted_for, 2),
                "weighted_against": round(weighted_against, 2),
                "vote_breakdown": vote_breakdown,
                "total_votes": len(votes_for) + len(votes_against),
            }

    elif protocol.protocol_type == ProtocolType.RANKED_VOTE.value:
        # Ranked-choice voting with instant runoff
        # Each vote message has payload.rankings = [option1, option2, ...]
        ranked_votes = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("rankings")]
        options = config.get("options", [])
        quorum = config.get("quorum", 2)

        if len(ranked_votes) >= quorum and options:
            # Run instant-runoff voting
            active_options = list(options)
            eliminated = []
            rounds = []

            while len(active_options) > 1:
                # Count first-preference votes among active options
                counts = {opt: 0 for opt in active_options}
                for m in ranked_votes:
                    rankings = m.payload.get("rankings", [])
                    # Find highest-ranked still-active option
                    for opt in rankings:
                        if opt in active_options:
                            counts[opt] += 1
                            break

                total = sum(counts.values())
                rounds.append({"active_options": list(active_options), "counts": dict(counts), "total": total})

                if total == 0:
                    break

                # Check for majority
                max_opt = max(counts, key=counts.get)
                if counts[max_opt] > total / 2:
                    protocol.status = ProtocolStatus.ACCEPTED.value
                    protocol.resolved_at = datetime.utcnow()
                    protocol.result = {
                        "winner": max_opt,
                        "rounds": rounds,
                        "total_votes": len(ranked_votes),
                    }
                    return

                # Eliminate the option with fewest votes
                min_opt = min(counts, key=counts.get)
                active_options.remove(min_opt)
                eliminated.append(min_opt)

            if active_options:
                protocol.status = ProtocolStatus.ACCEPTED.value
                protocol.resolved_at = datetime.utcnow()
                protocol.result = {
                    "winner": active_options[0],
                    "rounds": rounds,
                    "eliminated": eliminated,
                    "total_votes": len(ranked_votes),
                }

    elif protocol.protocol_type == ProtocolType.DELIBERATION.value:
        # Multi-round deliberation: requires N discussion messages before final vote
        discussion_messages = [m for m in messages if m.message_type in ("comment", "argument", "evidence")]
        final_votes = [m for m in messages if m.message_type == "vote"]
        min_discussion = config.get("min_discussion_messages", 2)
        quorum = config.get("quorum", 2)

        # Only allow final voting after sufficient discussion
        if len(discussion_messages) >= min_discussion and len(final_votes) >= quorum:
            votes_for = [m for m in final_votes if m.payload and m.payload.get("choice") == "for"]
            votes_against = [m for m in final_votes if m.payload and m.payload.get("choice") == "against"]

            if len(votes_for) > len(votes_against):
                protocol.status = ProtocolStatus.ACCEPTED.value
            else:
                protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {
                "discussion_count": len(discussion_messages),
                "votes_for": len(votes_for),
                "votes_against": len(votes_against),
                "deliberation_complete": True,
            }


# =========================================================================
# Agent Reputation System
# =========================================================================


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

@agents_bp.route("/<int:agent_id>/experiences", methods=["GET"])
@unified_auth_required
def list_agent_experiences(agent_id):
    """List experiences for an agent, with optional filters."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    query = AgentExperience.query.filter_by(agent_id=agent_id, is_valid=True)

    # Optional filters
    experience_type = args.get("experience_type")
    if experience_type:
        query = query.filter_by(experience_type=experience_type)
    domain = args.get("domain")
    if domain:
        query = query.filter_by(domain=domain)
    task_type = args.get("task_type")
    if task_type:
        query = query.filter_by(task_type=task_type)
    is_shared = args.get("is_shared")
    if is_shared is not None:
        query = query.filter_by(is_shared=is_shared.lower() == "true")

    query = query.order_by(AgentExperience.confidence.desc(), AgentExperience.created_at.desc())
    return paginate_query(query, "experiences")


@agents_bp.route("/<int:agent_id>/experiences", methods=["POST"])
@unified_auth_required
def create_agent_experience(agent_id):
    """Manually create an experience record for an agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request()
    required = ["experience_type", "strategy"]
    for f in required:
        if not data.get(f):
            return ApiResponse.error(f"Missing required field: {f}").to_response()

    exp = AgentExperience.create(
        agent_id=agent_id,
        experience_type=data["experience_type"],
        domain=data.get("domain"),
        task_type=data.get("task_type"),
        capabilities_used=data.get("capabilities_used", []),
        strategy=data["strategy"],
        outcome_pattern=data.get("outcome_pattern"),
        key_learnings=data.get("key_learnings"),
        confidence=data.get("confidence", 0.7),
        applicability_score=data.get("applicability_score", 0.5),
        source_task_id=data.get("source_task_id"),
        is_shared=data.get("is_shared", False),
        project_id=data.get("project_id"),
    )
    db.session.commit()

    notify_sse("agent_experience_created", {
        "agent_id": agent_id,
        "experience_id": exp.id,
        "experience_type": exp.experience_type,
    })
    return ApiResponse.success(exp.to_dict(), "Experience created").to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>", methods=["GET"])
@unified_auth_required
def get_agent_experience(agent_id, experience_id):
    """Get a specific experience record."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.query.filter_by(id=experience_id, agent_id=agent_id).first()
    if not exp:
        return ApiResponse.not_found("Experience not found").to_response()

    # Increment access count
    exp.access_count = (exp.access_count or 0) + 1
    db.session.commit()

    return ApiResponse.success(exp.to_dict()).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>", methods=["PUT"])
@unified_auth_required
def update_agent_experience(agent_id, experience_id):
    """Update an experience record."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.query.filter_by(id=experience_id, agent_id=agent_id).first()
    if not exp:
        return ApiResponse.not_found("Experience not found").to_response()

    data = validate_json_request()
    updatable = ["experience_type", "domain", "task_type", "capabilities_used",
                 "strategy", "outcome_pattern", "key_learnings", "confidence",
                 "applicability_score", "is_shared", "is_valid", "project_id"]
    for field in updatable:
        if field in data:
            setattr(exp, field, data[field])

    db.session.commit()
    return ApiResponse.success(exp.to_dict(), "Experience updated").to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>", methods=["DELETE"])
@unified_auth_required
def delete_agent_experience(agent_id, experience_id):
    """Soft-delete an experience (mark as invalid)."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.query.filter_by(id=experience_id, agent_id=agent_id).first()
    if not exp:
        return ApiResponse.not_found("Experience not found").to_response()

    exp.is_valid = False
    db.session.commit()
    return ApiResponse.success(None, "Experience deleted").to_response()


@agents_bp.route("/experiences/stats", methods=["GET"])
@unified_auth_required
def experiences_stats():
    """Aggregate AgentExperience stats for the current user.

    Breaks down experiences (valid only) by domain, task_type, and
    experience_type across all of the user's Agents. Also reports shared
    count, total reuse count, and average confidence. Reveals where the
    collective knowledge base is concentrated and where it is thin.
    """
    user = get_current_user()
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({
            "total": 0, "by_domain": {}, "by_task_type": {},
            "by_experience_type": {}, "shared": 0, "total_reuses": 0, "avg_confidence": None,
            "by_confidence_bucket": {}, "top_reused": [], "by_domain_tasktype": {}, "by_domain_reuses": {},
        }).to_response()

    rows = AgentExperience.query.filter(
        AgentExperience.agent_id.in_(agent_ids),
        AgentExperience.is_valid.is_(True),
    ).with_entities(
        AgentExperience.id,
        AgentExperience.domain,
        AgentExperience.task_type,
        AgentExperience.experience_type,
        AgentExperience.is_shared,
        AgentExperience.times_reused,
        AgentExperience.confidence,
        AgentExperience.key_learnings,
    ).all()

    by_domain: dict = {}
    by_task_type: dict = {}
    by_exp_type: dict = {}
    shared = 0
    total_reuses = 0
    confidences = []
    confidence_buckets = {"0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
    reuse_candidates = []
    domain_task_matrix: dict = {}  # {domain: {task_type: count}}
    by_domain_reuses: dict = {}  # {domain: cumulative reuse count}
    for exp_id, domain, task_type, exp_type, is_shared, times_reused, confidence, key_learnings in rows:
        d = domain or "(未分类)"
        by_domain[d] = by_domain.get(d, 0) + 1
        by_domain_reuses[d] = by_domain_reuses.get(d, 0) + (times_reused or 0)
        tt = task_type or "(未分类)"
        if task_type:
            by_task_type[task_type] = by_task_type.get(task_type, 0) + 1
        domain_task_matrix.setdefault(d, {})
        domain_task_matrix[d][tt] = domain_task_matrix[d].get(tt, 0) + 1
        et = exp_type or "(未分类)"
        by_exp_type[et] = by_exp_type.get(et, 0) + 1
        if is_shared:
            shared += 1
        total_reuses += times_reused or 0
        if confidence is not None:
            confidences.append(confidence)
            c = confidence
            if c < 0.3:
                confidence_buckets["0-0.3"] += 1
            elif c < 0.5:
                confidence_buckets["0.3-0.5"] += 1
            elif c < 0.7:
                confidence_buckets["0.5-0.7"] += 1
            elif c < 0.85:
                confidence_buckets["0.7-0.85"] += 1
            else:
                confidence_buckets["0.85-1.0"] += 1
        if (times_reused or 0) > 0:
            reuse_candidates.append({
                "id": exp_id,
                "domain": d,
                "task_type": task_type,
                "experience_type": et,
                "times_reused": times_reused or 0,
                "confidence": confidence,
                "key_learnings": (key_learnings or "")[:120],
            })

    avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else None
    # Sort breakdowns by count desc for display
    by_domain_sorted = dict(sorted(by_domain.items(), key=lambda kv: kv[1], reverse=True))
    by_task_sorted = dict(sorted(by_task_type.items(), key=lambda kv: kv[1], reverse=True))
    by_domain_reuses_sorted = dict(sorted(by_domain_reuses.items(), key=lambda kv: kv[1], reverse=True))
    top_reused = sorted(reuse_candidates, key=lambda x: x["times_reused"], reverse=True)[:10]
    return ApiResponse.success({
        "total": len(rows),
        "by_domain": by_domain_sorted,
        "by_task_type": by_task_sorted,
        "by_experience_type": by_exp_type,
        "shared": shared,
        "total_reuses": total_reuses,
        "avg_confidence": avg_conf,
        "by_confidence_bucket": confidence_buckets,
        "top_reused": top_reused,
        "by_domain_tasktype": domain_task_matrix,
        "by_domain_reuses": by_domain_reuses_sorted,
    }).to_response()


@agents_bp.route("/experiences/low-confidence", methods=["GET"])
@unified_auth_required
def experiences_low_confidence():
    """List the current user's valid experiences with low confidence.

    Returns experiences (across all of the user's Agents) whose confidence
    falls below ``max_confidence`` (default 0.5), sorted by confidence
    ascending. Each entry includes agent_id, domain, task_type,
    experience_type, confidence, times_reused, and a key_learnings excerpt.
    Surfaces weak knowledge entries that may need reinforcement or removal.
    """
    user = get_current_user()
    try:
        max_confidence = max(0.0, min(1.0, float(request.args.get("max_confidence", 0.5))))
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        max_confidence = 0.5
        limit = 20

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"max_confidence": max_confidence, "items": []}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
            AgentExperience.confidence.isnot(None),
            AgentExperience.confidence < max_confidence,
        )
        .order_by(AgentExperience.confidence.asc())
        .limit(limit)
        .with_entities(
            AgentExperience.id, AgentExperience.agent_id,
            AgentExperience.domain, AgentExperience.task_type,
            AgentExperience.experience_type, AgentExperience.confidence,
            AgentExperience.times_reused, AgentExperience.key_learnings,
        )
        .all()
    )

    items = [{
        "id": r.id,
        "agent_id": r.agent_id,
        "domain": r.domain or "(未分类)",
        "task_type": r.task_type,
        "experience_type": r.experience_type or "(未分类)",
        "confidence": r.confidence,
        "times_reused": r.times_reused or 0,
        "key_learnings": (r.key_learnings or "")[:120],
    } for r in rows]

    return ApiResponse.success({"max_confidence": max_confidence, "items": items}).to_response()


@agents_bp.route("/<int:agent_id>/experiences/recommend", methods=["GET"])
@unified_auth_required
def recommend_experiences(agent_id):
    """Recommend relevant experiences for an upcoming task context.

    Query params: domain, task_type, capabilities (comma-separated)
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    domain = args.get("domain")
    task_type = args.get("task_type")
    capabilities = args.get("capabilities", "").split(",") if args.get("capabilities") else None

    experiences = AgentExperience.find_relevant_experiences(
        agent_id=agent_id,
        domain=domain,
        task_type=task_type,
        capabilities=capabilities,
        include_shared=True,
        limit=10,
    )

    # Increment reuse count for recommended experiences
    now = datetime.utcnow()
    for exp in experiences:
        exp.times_reused = (exp.times_reused or 0) + 1
        exp.last_reused_at = now

    db.session.commit()

    return ApiResponse.success(
        [e.to_dict() for e in experiences],
        f"Found {len(experiences)} relevant experiences",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/share", methods=["POST"])
@unified_auth_required
def share_agent_experience(agent_id, experience_id):
    """Share an experience with other agents in the domain."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.share_experience(experience_id, agent_id)
    if not exp:
        return ApiResponse.not_found("Experience not found or not owned by this agent").to_response()

    db.session.commit()
    notify_sse("agent_experience_shared", {
        "agent_id": agent_id,
        "experience_id": exp.id,
        "domain": exp.domain,
    })
    return ApiResponse.success(exp.to_dict(), "Experience shared").to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/learn", methods=["POST"])
@unified_auth_required
def learn_from_experience(agent_id, experience_id):
    """An agent internalizes a shared experience from another agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    learned = AgentExperience.learn_from_shared(
        target_agent_id=agent_id,
        experience_id=experience_id,
    )
    if not learned:
        return ApiResponse.error("Experience not found, not shared, or already learned").to_response()

    db.session.commit()
    notify_sse("agent_experience_learned", {
        "agent_id": agent_id,
        "experience_id": learned.id,
        "source_experience_id": experience_id,
    })
    return ApiResponse.success(learned.to_dict(), "Experience learned").to_response()


@agents_bp.route("/<int:agent_id>/experiences/shared", methods=["GET"])
@unified_auth_required
def list_shared_experiences(agent_id):
    """List shared experiences from other agents that this agent can learn from."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    query = AgentExperience.query.filter(
        AgentExperience.is_shared == True,
        AgentExperience.is_valid == True,
        AgentExperience.agent_id != agent_id,  # Exclude own experiences
    )

    domain = args.get("domain")
    if domain:
        query = query.filter_by(domain=domain)
    task_type = args.get("task_type")
    if task_type:
        query = query.filter_by(task_type=task_type)

    query = query.order_by(AgentExperience.confidence.desc(), AgentExperience.created_at.desc())
    return paginate_query(query, "experiences")


@agents_bp.route("/<int:agent_id>/experiences/auto-extract", methods=["POST"])
@unified_auth_required
def auto_extract_experiences(agent_id):
    """Auto-extract experiences from recent task outcomes for an agent.

    Scans the agent's completed workflow steps and generates experience
    records for successful and failed outcomes.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    # Find recent step runs by this agent that don't have experiences yet
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent_steps = WorkflowStepRun.query.filter(
        WorkflowStepRun.agent_id == agent_id,
        WorkflowStepRun.status.in_([StepStatus.SUCCEEDED, StepStatus.FAILED]),
        WorkflowStepRun.finished_at >= cutoff,
    ).order_by(WorkflowStepRun.finished_at.desc()).limit(50).all()

    extracted = []
    for sr in recent_steps:
        # Skip if experience already extracted for this step
        existing = AgentExperience.query.filter_by(
            agent_id=agent_id,
            source_step_key=sr.step_key,
            source_workflow_run_id=sr.run_id,
        ).first()
        if existing:
            continue

        # Get step definition
        wf_run = WorkflowRun.query.get(sr.run_id)
        step_def = None
        if wf_run:
            step_def = WorkflowStep.query.filter_by(
                workflow_id=wf_run.workflow_id, step_key=sr.step_key
            ).first()

        # Get task if available
        task = Task.query.get(sr.task_id) if sr.task_id else None

        exp = AgentExperience.extract_from_step_outcome(
            agent_id=agent_id,
            step_run=sr,
            step_def=step_def,
            task=task,
        )
        extracted.append(exp)

    db.session.commit()

    notify_sse("agent_experiences_extracted", {
        "agent_id": agent_id,
        "count": len(extracted),
    })
    return ApiResponse.success(
        [e.to_dict() for e in extracted],
        f"Extracted {len(extracted)} experiences",
    ).to_response()


# ---------------------------------------------------------------------------
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
# Agent Experience Decay & Validation endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/<int:agent_id>/experiences/decay", methods=["POST"])
@unified_auth_required
def apply_experience_decay(agent_id):
    """Apply time-based confidence decay to an Agent's experiences.

    Query params:
      days_threshold – minimum age in days before decay applies (default 30)
      decay_rate – confidence reduction factor per cycle (default 0.02)
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request() or {}
    days_threshold = data.get("days_threshold", 30)
    decay_rate = data.get("decay_rate", 0.02)

    decayed = AgentExperience.apply_decay(
        agent_id=agent_id,
        days_threshold=days_threshold,
        decay_rate=decay_rate,
    )
    db.session.commit()

    return ApiResponse.success(
        {"decayed_count": decayed},
        f"Applied decay to {decayed} experiences",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/validate", methods=["POST"])
@unified_auth_required
def validate_experience(agent_id, experience_id):
    """Cross-validate an experience by another Agent.

    The validator agent confirms or refutes the experience's accuracy,
    affecting its confidence score.
    """
    user = get_current_user()
    # Verify the validator agent belongs to the user
    validator = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not validator:
        return ApiResponse.not_found("Validator agent not found").to_response()

    data = validate_json_request()
    is_accurate = data.get("is_accurate", True)
    if "is_accurate" not in data:
        return ApiResponse.error("is_accurate is required (true/false)").to_response()

    result = AgentExperience.cross_validate(
        experience_id=experience_id,
        validator_agent_id=agent_id,
        is_accurate=is_accurate,
    )
    if not result:
        return ApiResponse.not_found("Experience not found or already invalid").to_response()

    db.session.commit()
    action = "验证通过" if is_accurate else "已反驳"
    return ApiResponse.success(
        result.to_dict(),
        f"经验已{action}，新置信度: {result.confidence}",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/validation-stats", methods=["GET"])
@unified_auth_required
def get_experience_validation_stats(agent_id):
    """Get validation statistics for an Agent's experiences."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    stats = AgentExperience.get_validation_stats(agent_id)
    return ApiResponse.success(stats).to_response()


@agents_bp.route("/maintenance/decay-all-experiences", methods=["POST"])
@unified_auth_required
def decay_all_experiences():
    """System maintenance: apply decay to all agents' experiences.

    Typically called by a scheduled job or admin action.
    """
    user = get_current_user()
    data = validate_json_request() or {}
    days_threshold = data.get("days_threshold", 30)
    decay_rate = data.get("decay_rate", 0.02)

    decayed = AgentExperience.apply_decay(
        agent_id=None,  # All agents
        days_threshold=days_threshold,
        decay_rate=decay_rate,
    )
    db.session.commit()

    return ApiResponse.success(
        {"decayed_count": decayed},
        f"Applied decay to {decayed} experiences across all agents",
    ).to_response()


# ---------------------------------------------------------------------------
# Agent Adaptive Capabilities endpoints
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


# ---------------------------------------------------------------------------
# Increment 85: Agent collaboration sandbox — secure execution isolation
# ---------------------------------------------------------------------------

_VALID_SANDBOX_LEVELS = {"strict", "moderate", "permissive"}


def _sandbox_body(body, partial=False):
    """Extract and validate sandbox fields from a request body."""
    fields = {
        "name": body.get("name"),
        "description": body.get("description"),
        "agent_id": body.get("agent_id"),
        "security_level": body.get("security_level", "moderate"),
        "allowed_tools": body.get("allowed_tools", []),
        "blocked_tools": body.get("blocked_tools", []),
        "allowed_network_hosts": body.get("allowed_network_hosts", []),
        "fs_write_paths": body.get("fs_write_paths", []),
        "fs_read_paths": body.get("fs_read_paths", []),
        "max_memory_mb": body.get("max_memory_mb", 0),
        "max_cpu_seconds": body.get("max_cpu_seconds", 0),
        "max_output_tokens": body.get("max_output_tokens", 0),
        "timeout_seconds": body.get("timeout_seconds", 0),
        "is_active": body.get("is_active", True),
    }
    if not partial:
        if not fields["name"]:
            return None, "Sandbox name is required"
    if fields["security_level"] not in _VALID_SANDBOX_LEVELS:
        return None, f"security_level must be one of {_VALID_SANDBOX_LEVELS}"
    # Coerce list fields
    for k in ("allowed_tools", "blocked_tools", "allowed_network_hosts", "fs_write_paths", "fs_read_paths"):
        if fields[k] is None:
            fields[k] = []
        elif not isinstance(fields[k], list):
            return None, f"{k} must be a list"
    # Coerce int fields
    for k in ("max_memory_mb", "max_cpu_seconds", "max_output_tokens", "timeout_seconds"):
        try:
            fields[k] = int(fields[k] or 0)
        except (TypeError, ValueError):
            return None, f"{k} must be an integer"
    if fields["agent_id"] is not None:
        try:
            fields["agent_id"] = int(fields["agent_id"])
        except (TypeError, ValueError):
            return None, "agent_id must be an integer"
    return fields, None


# ---------------------------------------------------------------------------
# Preset sandbox policy templates (Increment 90)
# ---------------------------------------------------------------------------

SANDBOX_TEMPLATES = [
    {
        "key": "read_only_research",
        "name": "只读研究",
        "description": "严格隔离：无网络、无写盘、仅允许只读工具。适合信息检索与分析类任务。",
        "security_level": "strict",
        "allowed_tools": ["search", "read_file", "list_files"],
        "blocked_tools": [],
        "allowed_network_hosts": [],
        "fs_write_paths": [],
        "fs_read_paths": [],
        "max_memory_mb": 256,
        "max_cpu_seconds": 120,
        "max_output_tokens": 8000,
        "timeout_seconds": 300,
    },
    {
        "key": "code_generation",
        "name": "代码生成",
        "description": "中等隔离：受限网络（仅文档站）、范围写盘、允许代码生成工具。适合编码类任务。",
        "security_level": "moderate",
        "allowed_tools": ["read_file", "write_file", "run_tests", "search"],
        "blocked_tools": ["delete_file", "execute_shell"],
        "allowed_network_hosts": ["docs.python.org", "developer.mozilla.org", "registry.npmjs.org"],
        "fs_write_paths": ["/tmp/work", "/workspace/src"],
        "fs_read_paths": ["/workspace", "/data/in"],
        "max_memory_mb": 512,
        "max_cpu_seconds": 600,
        "max_output_tokens": 16000,
        "timeout_seconds": 900,
    },
    {
        "key": "data_analysis",
        "name": "数据分析",
        "description": "中等隔离：允许读取数据源、写入输出目录、网络访问数据 API。适合数据处理任务。",
        "security_level": "moderate",
        "allowed_tools": ["read_file", "write_file", "query_database", "http_get"],
        "blocked_tools": ["execute_shell", "delete_file"],
        "allowed_network_hosts": ["api.data.example.com"],
        "fs_write_paths": ["/data/out", "/tmp/analysis"],
        "fs_read_paths": ["/data"],
        "max_memory_mb": 1024,
        "max_cpu_seconds": 1800,
        "max_output_tokens": 32000,
        "timeout_seconds": 1800,
    },
    {
        "key": "full_autonomy",
        "name": "完全自主",
        "description": "宽松隔离：全网络、全盘、仅黑名单危险工具。适合受信任的自主执行场景。",
        "security_level": "permissive",
        "allowed_tools": [],
        "blocked_tools": ["rm_rf", "format_disk", "shutdown"],
        "allowed_network_hosts": [],
        "fs_write_paths": [],
        "fs_read_paths": [],
        "max_memory_mb": 2048,
        "max_cpu_seconds": 3600,
        "max_output_tokens": 64000,
        "timeout_seconds": 3600,
    },
    {
        "key": "sandboxed_review",
        "name": "沙盒评审",
        "description": "严格隔离：无网络无写盘，仅允许读取和评审工具，短超时。适合代码/文档评审。",
        "security_level": "strict",
        "allowed_tools": ["read_file", "list_files", "comment"],
        "blocked_tools": [],
        "allowed_network_hosts": [],
        "fs_write_paths": [],
        "fs_read_paths": ["/workspace"],
        "max_memory_mb": 128,
        "max_cpu_seconds": 60,
        "max_output_tokens": 4000,
        "timeout_seconds": 180,
    },
]


@agents_bp.route("/sandbox-templates", methods=["GET"])
@unified_auth_required
def list_sandbox_templates():
    """List preset sandbox policy templates."""
    return ApiResponse.success({"templates": SANDBOX_TEMPLATES}).to_response()


@agents_bp.route("/sandbox-templates/<template_key>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_sandbox_template(template_key):
    """Create a sandbox policy from a preset template.

    Body (optional): { name?, agent_id?, overrides?: {...} }
    """
    user = get_current_user()
    template = next((t for t in SANDBOX_TEMPLATES if t["key"] == template_key), None)
    if not template:
        return ApiResponse.not_found("Sandbox template not found").to_response()
    body = validate_json_request() or {}
    overrides = body.get("overrides") or {}
    # Merge template with overrides
    fields = {k: v for k, v in template.items() if k != "key"}
    fields["name"] = body.get("name") or f"{template['name']} (副本)"
    if body.get("agent_id") is not None:
        fields["agent_id"] = body.get("agent_id")
        agent = Agent.query.get(fields["agent_id"])
        if not agent or agent.owner_id != user.id:
            return ApiResponse.error("Agent not found or not owned by you").to_response()
    else:
        fields["agent_id"] = None
    # Apply overrides for overridable fields
    for k in ("allowed_tools", "blocked_tools", "allowed_network_hosts", "fs_write_paths", "fs_read_paths",
              "max_memory_mb", "max_cpu_seconds", "max_output_tokens", "timeout_seconds", "security_level", "description"):
        if k in overrides and overrides[k] is not None:
            fields[k] = overrides[k]
    validated, err = _sandbox_body(fields)
    if err:
        return ApiResponse.error(err).to_response()
    sandbox = AgentSandbox(owner_id=user.id, **validated)
    db.session.add(sandbox)
    AuditLog.record(
        action="sandbox.template_instantiate", resource_type="agent_sandbox", resource_id=None,
        actor_type="human", actor_user_id=user.id,
        detail={"template_key": template_key, "agent_id": fields.get("agent_id"),
                "security_level": fields.get("security_level")},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_created", {"sandbox_id": sandbox.id, "from_template": template_key})
    flush_sse_notifications()
    return ApiResponse.success(sandbox.to_dict(include_stats=True), f"Sandbox created from template '{template['name']}'").to_response()


@agents_bp.route("/sandboxes", methods=["GET"])
@unified_auth_required
def list_sandboxes():
    """List sandbox policies owned by the current user (optionally filtered by agent)."""
    user = get_current_user()
    q = AgentSandbox.query.filter_by(owner_id=user.id)
    agent_id = request.args.get("agent_id", type=int)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    active_only = request.args.get("active_only", type=str)
    if active_only and active_only.lower() == "true":
        q = q.filter_by(is_active=True)
    include_stats = request.args.get("include_stats", "true").lower() == "true"
    q = q.order_by(AgentSandbox.created_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=20)
    items = [s.to_dict(include_stats=include_stats) for s in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/sandboxes", methods=["POST"])
@unified_auth_required
def create_sandbox():
    """Create a new sandbox policy."""
    user = get_current_user()
    body = validate_json_request()
    fields, err = _sandbox_body(body)
    if err:
        return ApiResponse.error(err).to_response()
    if fields["agent_id"] is not None:
        agent = Agent.query.get(fields["agent_id"])
        if not agent or agent.owner_id != user.id:
            return ApiResponse.error("Agent not found or not owned by you").to_response()
    sandbox = AgentSandbox(owner_id=user.id, **fields)
    db.session.add(sandbox)
    db.session.commit()
    _queue_sse(user.id, "sandbox_created", {"sandbox_id": sandbox.id, "agent_id": sandbox.agent_id})
    flush_sse_notifications()
    return ApiResponse.success(sandbox.to_dict(include_stats=True), "Sandbox created").to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>", methods=["GET"])
@unified_auth_required
def get_sandbox(sandbox_id):
    """Get a sandbox policy by ID."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    return ApiResponse.success(sandbox.to_dict(include_stats=True)).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>", methods=["PUT"])
@unified_auth_required
def update_sandbox(sandbox_id):
    """Update an existing sandbox policy."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    body = validate_json_request()
    fields, err = _sandbox_body(body, partial=True)
    if err:
        return ApiResponse.error(err).to_response()
    if fields["agent_id"] is not None:
        agent = Agent.query.get(fields["agent_id"])
        if not agent or agent.owner_id != user.id:
            return ApiResponse.error("Agent not found or not owned by you").to_response()
    for k, v in fields.items():
        if v is not None or k in ("is_active", "description"):
            setattr(sandbox, k, v)
    AuditLog.record(
        action="sandbox.update", resource_type="agent_sandbox", resource_id=sandbox_id,
        actor_type="human", actor_user_id=user.id,
        detail={"changed_fields": [k for k, v in fields.items() if v is not None or k in ("is_active", "description")]},
    )
    db.session.commit()
    return ApiResponse.success(sandbox.to_dict(include_stats=True), "Sandbox updated").to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>", methods=["DELETE"])
@unified_auth_required
def delete_sandbox(sandbox_id):
    """Delete a sandbox policy (only if no active executions)."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    active = SandboxExecution.query.filter_by(
        sandbox_id=sandbox_id, status=SandboxExecutionStatus.RUNNING
    ).count()
    if active:
        return ApiResponse.error(f"Cannot delete: {active} active execution(s) reference this sandbox").to_response()
    AuditLog.record(
        action="sandbox.delete", resource_type="agent_sandbox", resource_id=sandbox_id,
        actor_type="human", actor_user_id=user.id,
        detail={"name": sandbox.name, "security_level": sandbox.security_level.value if sandbox.security_level else None,
                "agent_id": sandbox.agent_id},
    )
    db.session.delete(sandbox)
    db.session.commit()
    return ApiResponse.success({"deleted": True}, "Sandbox deleted").to_response()


@agents_bp.route("/<int:agent_id>/sandbox", methods=["GET"])
@unified_auth_required
def get_agent_sandbox(agent_id):
    """Get the active sandbox bound to an agent."""
    user = get_current_user()
    agent = Agent.query.get(agent_id)
    if not agent or agent.owner_id != user.id:
        return ApiResponse.error("Agent not found or not owned by you", 404).to_response()
    sandbox = AgentSandbox.get_for_agent(agent_id)
    if not sandbox:
        return ApiResponse.success({"sandbox": None}, "No active sandbox bound to this agent").to_response()
    return ApiResponse.success({"sandbox": sandbox.to_dict(include_stats=True)}).to_response()


@agents_bp.route("/<int:agent_id>/sandbox/bind", methods=["POST"])
@unified_auth_required
def bind_agent_sandbox(agent_id):
    """Bind a sandbox policy to an agent (replaces any existing active binding)."""
    user = get_current_user()
    agent = Agent.query.get(agent_id)
    if not agent or agent.owner_id != user.id:
        return ApiResponse.error("Agent not found or not owned by you", 404).to_response()
    body = validate_json_request()
    sandbox_id = body.get("sandbox_id")
    if not sandbox_id:
        return ApiResponse.error("sandbox_id is required").to_response()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    # Deactivate any other active sandboxes bound to this agent
    AgentSandbox.query.filter(
        AgentSandbox.agent_id == agent_id,
        AgentSandbox.is_active == True,
        AgentSandbox.id != sandbox_id,
    ).update({"is_active": False})
    sandbox.agent_id = agent_id
    sandbox.is_active = True
    AuditLog.record(
        action="sandbox.bind", resource_type="agent_sandbox", resource_id=sandbox_id,
        actor_type="human", actor_user_id=user.id,
        detail={"agent_id": agent_id, "security_level": sandbox.security_level.value if sandbox.security_level else None},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_bound", {"agent_id": agent_id, "sandbox_id": sandbox_id})
    flush_sse_notifications()
    return ApiResponse.success({"sandbox": sandbox.to_dict(include_stats=True)}, "Sandbox bound to agent").to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/policy", methods=["GET"])
@unified_auth_required
def get_sandbox_policy(sandbox_id):
    """Get the serializable policy envelope for an executor to consume."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    return ApiResponse.success({"policy": sandbox.to_policy_dict()}).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/check", methods=["POST"])
@unified_auth_required
def check_sandbox_action(sandbox_id):
    """Dry-run check of an action against a sandbox policy (no execution).

    Body: { action: "tool"|"network"|"fs_write", target: <tool_name|host|path> }
    Returns whether the action is permitted and why.
    """
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    body = validate_json_request()
    action = body.get("action")
    target = body.get("target")
    if not action or not target:
        return ApiResponse.error("action and target are required").to_response()
    if action == "tool":
        allowed, reason = sandbox.check_tool(target)
    elif action == "network":
        allowed, reason = sandbox.check_network(target)
    elif action == "fs_write":
        allowed, reason = sandbox.check_fs_write(target)
    else:
        return ApiResponse.error("action must be tool, network, or fs_write").to_response()
    return ApiResponse.success({"allowed": allowed, "reason": reason, "action": action, "target": target}).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/executions", methods=["GET"])
@unified_auth_required
def list_sandbox_executions(sandbox_id):
    """List executions under a sandbox policy."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    q = SandboxExecution.query.filter_by(sandbox_id=sandbox_id)
    status_filter = request.args.get("status")
    if status_filter:
        q = q.filter_by(status=SandboxExecutionStatus(status_filter))
    agent_id = request.args.get("agent_id", type=int)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    q = q.order_by(SandboxExecution.started_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=20)
    items = [e.to_dict(include_violations=False) for e in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/executions/<int:execution_id>", methods=["GET"])
@unified_auth_required
def get_sandbox_execution(execution_id):
    """Get a sandbox execution record with violations."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    return ApiResponse.success({"execution": execution.to_dict(include_violations=True)}).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/executions", methods=["POST"])
@unified_auth_required
def start_sandbox_execution(sandbox_id):
    """Start a new sandboxed execution for an agent.

    Freezes the policy snapshot, creates a RUNNING SandboxExecution, and returns
    the execution record + policy envelope for the executor to enforce.
    """
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    body = validate_json_request()
    agent_id = body.get("agent_id")
    run_id = body.get("run_id")
    step_run_id = body.get("step_run_id")
    if not agent_id:
        return ApiResponse.error("agent_id is required").to_response()
    agent = Agent.query.get(agent_id)
    if not agent or agent.owner_id != user.id:
        return ApiResponse.error("Agent not found or not owned by you").to_response()
    execution = SandboxExecution(
        sandbox_id=sandbox_id,
        agent_id=agent_id,
        run_id=run_id,
        step_run_id=step_run_id,
        status=SandboxExecutionStatus.RUNNING,
        policy_snapshot=sandbox.to_policy_dict(),
        started_at=datetime.utcnow(),
        tool_calls=0,
        network_calls=0,
    )
    db.session.add(execution)
    db.session.commit()
    _queue_sse(user.id, "sandbox_execution_started", {
        "execution_id": execution.id, "sandbox_id": sandbox_id, "agent_id": agent_id,
    })
    flush_sse_notifications()
    return ApiResponse.success({
        "execution": execution.to_dict(),
        "policy": sandbox.to_policy_dict(),
    }, "Sandboxed execution started").to_response()


@agents_bp.route("/executions/<int:execution_id>/complete", methods=["POST"])
@unified_auth_required
def complete_sandbox_execution(execution_id):
    """Mark a sandboxed execution as completed."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    body = validate_json_request()
    execution.finish(
        SandboxExecutionStatus.COMPLETED,
        summary=body.get("output_summary"),
        error=body.get("error"),
    )
    # Update aggregated usage if provided
    if body.get("peak_memory_mb") is not None:
        execution.peak_memory_mb = body.get("peak_memory_mb")
    if body.get("cpu_seconds") is not None:
        execution.cpu_seconds = body.get("cpu_seconds")
    if body.get("output_tokens") is not None:
        execution.output_tokens = body.get("output_tokens")
    if body.get("tool_calls") is not None:
        execution.tool_calls = body.get("tool_calls")
    if body.get("network_calls") is not None:
        execution.network_calls = body.get("network_calls")
    db.session.commit()
    _queue_sse(user.id, "sandbox_execution_completed", {"execution_id": execution_id})
    flush_sse_notifications()
    return ApiResponse.success({"execution": execution.to_dict()}, "Execution completed").to_response()


@agents_bp.route("/executions/<int:execution_id>/revoke", methods=["POST"])
@unified_auth_required
def revoke_sandbox_execution(execution_id):
    """Manually revoke (terminate) a running sandboxed execution."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    if execution.status != SandboxExecutionStatus.RUNNING:
        return ApiResponse.error(f"Execution is not running (status={execution.status.value})").to_response()
    execution.finish(SandboxExecutionStatus.REVOKED, reason="Manually revoked by owner")
    AuditLog.record(
        action="sandbox.execution_revoke", resource_type="sandbox_execution", resource_id=execution.id,
        actor_type="human", actor_user_id=user.id,
        detail={"sandbox_id": execution.sandbox_id, "agent_id": execution.agent_id},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_execution_revoked", {"execution_id": execution_id})
    flush_sse_notifications()
    return ApiResponse.success({"execution": execution.to_dict()}, "Execution revoked").to_response()


@agents_bp.route("/executions/<int:execution_id>/violation", methods=["POST"])
@unified_auth_required
def report_sandbox_violation(execution_id):
    """Report a policy violation during a sandboxed execution.

    Body: { violation_type, attempted_action, detail, terminate?: bool }
    If terminate is true, the execution is marked VIOLATED.
    """
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    body = validate_json_request()
    vtype = body.get("violation_type")
    try:
        vtype_enum = SandboxViolationType(vtype)
    except ValueError:
        return ApiResponse.error(f"Invalid violation_type: {vtype}").to_response()
    v = execution.record_violation(
        vtype_enum,
        detail=body.get("detail", ""),
        attempted_action=body.get("attempted_action"),
    )
    if body.get("terminate"):
        execution.finish(
            SandboxExecutionStatus.VIOLATED,
            reason=f"Policy violation: {vtype_enum.value}",
        )
    AuditLog.record(
        action="sandbox.violation", resource_type="sandbox_violation", resource_id=v.id,
        actor_type="human", actor_user_id=user.id,
        detail={"execution_id": execution_id, "violation_type": vtype_enum.value,
                "agent_id": execution.agent_id, "terminated": bool(body.get("terminate"))},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_violation", {
        "execution_id": execution_id, "violation_type": vtype_enum.value,
    })
    flush_sse_notifications()
    return ApiResponse.success({
        "violation": v.to_dict(),
        "execution": execution.to_dict(),
    }, "Violation recorded").to_response()


@agents_bp.route("/executions/<int:execution_id>/violations", methods=["GET"])
@unified_auth_required
def list_execution_violations(execution_id):
    """List violations recorded for a sandboxed execution."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    q = SandboxViolation.query.filter_by(execution_id=execution_id).order_by(SandboxViolation.blocked_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=50)
    items = [v.to_dict() for v in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/sandboxes/dashboard", methods=["GET"])
@unified_auth_required
def sandbox_dashboard():
    """Aggregate sandbox stats for the current user."""
    user = get_current_user()
    sandboxes = AgentSandbox.query.filter_by(owner_id=user.id).all()
    sandbox_ids = [s.id for s in sandboxes]
    total_executions = 0
    running = 0
    violations_total = 0
    by_level = {"strict": 0, "moderate": 0, "permissive": 0}
    by_status = {}
    for s in sandboxes:
        by_level[s.security_level.value if s.security_level else "moderate"] += 1
    if sandbox_ids:
        total_executions = SandboxExecution.query.filter(SandboxExecution.sandbox_id.in_(sandbox_ids)).count()
        running = SandboxExecution.query.filter(
            SandboxExecution.sandbox_id.in_(sandbox_ids),
            SandboxExecution.status == SandboxExecutionStatus.RUNNING,
        ).count()
        violations_total = SandboxViolation.query.filter(SandboxViolation.sandbox_id.in_(sandbox_ids)).count()
        # Status breakdown
        for st in SandboxExecutionStatus:
            cnt = SandboxExecution.query.filter(
                SandboxExecution.sandbox_id.in_(sandbox_ids),
                SandboxExecution.status == st,
            ).count()
            by_status[st.value] = cnt
    return ApiResponse.success({
        "total_sandboxes": len(sandboxes),
        "total_executions": total_executions,
        "running_executions": running,
        "total_violations": violations_total,
        "by_level": by_level,
        "by_status": by_status,
    }).to_response()


@agents_bp.route("/sandboxes/violation-trend", methods=["GET"])
@unified_auth_required
def sandbox_violation_trend():
    """Daily sandbox violation counts + by-type breakdown for the current user.

    Buckets by calendar day (UTC) using ``blocked_at``. Also returns a
    by-violation-type aggregate over the window. Useful for spotting whether
    a policy tightening or Agent change is producing more violations.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    sandbox_ids = [s.id for s in AgentSandbox.query.filter_by(owner_id=user.id).with_entities(AgentSandbox.id).all()]
    if not sandbox_ids:
        return ApiResponse.success({"days": days, "trend": [], "by_type": {}}).to_response()

    from sqlalchemy import func as sa_func
    daily = (
        db.session.query(
            sa_func.date(SandboxViolation.blocked_at).label("date"),
            sa_func.count(SandboxViolation.id).label("count"),
        )
        .filter(SandboxViolation.sandbox_id.in_(sandbox_ids), SandboxViolation.blocked_at >= since)
        .group_by(sa_func.date(SandboxViolation.blocked_at))
        .order_by(sa_func.date(SandboxViolation.blocked_at))
        .all()
    )
    trend = [{"date": str(d), "count": c} for d, c in daily]

    by_type = {}
    for vt in SandboxViolationType:
        by_type[vt.value] = SandboxViolation.query.filter(
            SandboxViolation.sandbox_id.in_(sandbox_ids),
            SandboxViolation.violation_type == vt,
            SandboxViolation.blocked_at >= since,
        ).count()

    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "by_type": by_type,
    }).to_response()


@agents_bp.route("/sandboxes/violations-by-agent", methods=["GET"])
@unified_auth_required
def sandbox_violations_by_agent():
    """Per-Agent sandbox violation counts for the current user.

    Aggregates SandboxViolation by ``agent_id`` over the lookback window,
    with a by-violation-type sub-count. Returns the top N by total, enriched
    with the Agent's name/kind. Reveals which Agents most frequently attempt
    disallowed actions.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    sandbox_ids = [s.id for s in AgentSandbox.query.filter_by(owner_id=user.id).with_entities(AgentSandbox.id).all()]
    if not sandbox_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    rows = SandboxViolation.query.filter(
        SandboxViolation.sandbox_id.in_(sandbox_ids),
        SandboxViolation.blocked_at >= since,
    ).with_entities(SandboxViolation.agent_id, SandboxViolation.violation_type).all()

    agg: dict = {}
    for aid, vt in rows:
        if aid is None:
            continue
        entry = agg.setdefault(aid, {"agent_id": aid, "total": 0, "by_type": {}})
        entry["total"] += 1
        key = vt.value if hasattr(vt, "value") else str(vt)
        entry["by_type"][key] = entry["by_type"].get(key, 0) + 1

    top = sorted(agg.values(), key=lambda x: x["total"], reverse=True)[:limit]
    agent_ids = [e["agent_id"] for e in top]
    agents = {a.id: a for a in Agent.query.filter(Agent.id.in_(agent_ids)).all()} if agent_ids else {}
    for e in top:
        a = agents.get(e["agent_id"])
        e["name"] = a.name if a else None
        e["kind"] = a.kind.value if a and a.kind else None
    return ApiResponse.success({"days": days, "items": top}).to_response()


@agents_bp.route("/sandboxes/template-usage", methods=["GET"])
@unified_auth_required
def sandbox_template_usage():
    """Sandbox policy template instantiation stats for the current user.

    Aggregates ``sandbox.template_instantiate`` audit events by template_key:
    how many times each preset template was instantiated, and how many of
    those instances were bound to an Agent (vs. left as a reusable policy).
    Reveals which templates are most popular in practice.
    """
    user = get_current_user()
    rows = AuditLog.query.filter(
        AuditLog.action == "sandbox.template_instantiate",
        AuditLog.actor_user_id == user.id,
    ).all()
    agg: dict = {}
    for r in rows:
        d = r.detail or {}
        key = d.get("template_key")
        if not key:
            continue
        entry = agg.setdefault(key, {"template_key": key, "uses": 0, "bound_to_agent": 0})
        entry["uses"] += 1
        if d.get("agent_id") is not None:
            entry["bound_to_agent"] += 1
    items = sorted(agg.values(), key=lambda x: x["uses"], reverse=True)
    return ApiResponse.success({"items": items}).to_response()


@agents_bp.route("/productivity", methods=["GET"])
@unified_auth_required
def agent_productivity():
    """Per-Agent productivity stats for the current user.

    Aggregates TaskAssignment rows by agent: total assignments, completed
    (DONE), failed, cancelled, completion rate, and average completion
    duration (completed_at - claimed_at, in hours) for done assignments.
    Reveals each Agent's throughput and reliability.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        days = 30
        limit = 20

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id,
            TaskAssignment.state,
            TaskAssignment.claimed_at,
            TaskAssignment.completed_at,
        )
        .all()
    )

    agg: dict = {}
    durations = {}  # agent_id -> list of hours
    for aid, state, claimed_at, completed_at in rows:
        bucket = agg.setdefault(aid, {
            "agent_id": aid, "total": 0, "done": 0, "failed": 0,
            "cancelled": 0, "expired": 0, "in_progress": 0,
        })
        bucket["total"] += 1
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            if claimed_at and completed_at and completed_at > claimed_at:
                durations.setdefault(aid, []).append((completed_at - claimed_at).total_seconds() / 3600)
        elif s == "failed":
            bucket["failed"] += 1
        elif s == "cancelled":
            bucket["cancelled"] += 1
        elif s == "expired":
            bucket["expired"] += 1
        else:
            bucket["in_progress"] += 1

    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(list(agg.keys()))).with_entities(Agent.id, Agent.name).all()} if agg else {}
    items = []
    for aid, b in agg.items():
        done = b["done"]
        total = b["total"]
        ds = durations.get(aid, [])
        items.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "total": total,
            "done": done,
            "failed": b["failed"],
            "cancelled": b["cancelled"],
            "expired": b["expired"],
            "in_progress": b["in_progress"],
            "completion_rate": round(done / total * 100, 1) if total else 0,
            "avg_completion_hours": round(sum(ds) / len(ds), 2) if ds else None,
        })
    items.sort(key=lambda x: x["done"], reverse=True)

    return ApiResponse.success({"days": days, "items": items[:limit]}).to_response()


@agents_bp.route("/productivity/trend", methods=["GET"])
@unified_auth_required
def agent_productivity_trend():
    """Daily Agent assignment completion trend for the current user.

    Buckets done TaskAssignments (state=DONE, completed_at within window)
    by day, returning per-day done count and failed count (state=FAILED).
    Reveals whether throughput is rising or falling over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "trend": [], "total_done": 0, "total_failed": 0}).to_response()

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
            TaskAssignment.state.in_([TaskAssignmentState.DONE, TaskAssignmentState.FAILED]),
        )
        .with_entities(
            TaskAssignment.state,
            func.date(TaskAssignment.completed_at).label("d"),
        )
        .all()
    )

    by_day: dict = {}
    total_done = 0
    total_failed = 0
    for state, d in rows:
        if not d:
            continue
        key = str(d)
        bucket = by_day.setdefault(key, {"date": key, "done": 0, "failed": 0})
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            total_done += 1
        elif s == "failed":
            bucket["failed"] += 1
            total_failed += 1

    trend = sorted(by_day.values(), key=lambda x: x["date"])
    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_done": total_done,
        "total_failed": total_failed,
    }).to_response()


@agents_bp.route("/productivity/alerts", methods=["GET"])
@unified_auth_required
def agent_productivity_alerts():
    """Low-efficiency Agent alert list for the current user.

    Returns Agents whose assignment completion rate falls below
    ``min_completion_rate`` (default 50%) OR whose failure rate exceeds
    ``max_failure_rate`` (default 30%) within the window, provided they have
    at least ``min_assignments`` (default 3) assignments. Each entry includes
    the same productivity fields as ``/agents/productivity`` plus the
    triggering reason. Surfaces Agents needing attention.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        min_completion_rate = max(0, min(100, float(request.args.get("min_completion_rate", 50))))
        max_failure_rate = max(0, min(100, float(request.args.get("max_failure_rate", 30))))
        min_assignments = max(1, min(1000, int(request.args.get("min_assignments", 3))))
    except (TypeError, ValueError):
        days = 30
        min_completion_rate = 50
        max_failure_rate = 30
        min_assignments = 3

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id, TaskAssignment.state,
            TaskAssignment.claimed_at, TaskAssignment.completed_at,
        )
        .all()
    )

    agg: dict = {}
    durations = {}
    for aid, state, claimed_at, completed_at in rows:
        bucket = agg.setdefault(aid, {
            "agent_id": aid, "total": 0, "done": 0, "failed": 0,
            "cancelled": 0, "expired": 0, "in_progress": 0,
        })
        bucket["total"] += 1
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            if claimed_at and completed_at and completed_at > claimed_at:
                durations.setdefault(aid, []).append((completed_at - claimed_at).total_seconds() / 3600)
        elif s == "failed":
            bucket["failed"] += 1
        elif s == "cancelled":
            bucket["cancelled"] += 1
        elif s == "expired":
            bucket["expired"] += 1
        else:
            bucket["in_progress"] += 1

    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(list(agg.keys()))).with_entities(Agent.id, Agent.name).all()} if agg else {}
    items = []
    for aid, b in agg.items():
        total = b["total"]
        if total < min_assignments:
            continue
        done = b["done"]
        failed = b["failed"]
        completion_rate = round(done / total * 100, 1) if total else 0
        failure_rate = round(failed / total * 100, 1) if total else 0
        reasons = []
        if completion_rate < min_completion_rate:
            reasons.append(f"完成率 {completion_rate}% < {min_completion_rate}%")
        if failure_rate > max_failure_rate:
            reasons.append(f"失败率 {failure_rate}% > {max_failure_rate}%")
        if not reasons:
            continue
        ds = durations.get(aid, [])
        items.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "total": total,
            "done": done,
            "failed": failed,
            "cancelled": b["cancelled"],
            "expired": b["expired"],
            "in_progress": b["in_progress"],
            "completion_rate": completion_rate,
            "failure_rate": failure_rate,
            "avg_completion_hours": round(sum(ds) / len(ds), 2) if ds else None,
            "reasons": reasons,
        })
    # 最差优先：按完成率升序、失败率降序
    items.sort(key=lambda x: (x["completion_rate"], -x["failure_rate"]))

    return ApiResponse.success({
        "days": days,
        "min_completion_rate": min_completion_rate,
        "max_failure_rate": max_failure_rate,
        "min_assignments": min_assignments,
        "items": items,
    }).to_response()


@agents_bp.route("/productivity/by-kind", methods=["GET"])
@unified_auth_required
def agent_productivity_by_kind():
    """Productivity comparison grouped by Agent kind for the current user.

    Aggregates TaskAssignment rows by the owning Agent's ``kind`` field:
    per-kind totals, done, failed, cancelled, expired, in_progress,
    agent count, average completion rate, average failure rate, and average
    completion duration (hours). Surfaces how each Agent class performs
    relative to its peers of the same kind.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    # kind per agent
    kind_map = {
        aid: (k.value if k else "unknown")
        for aid, k in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.kind).all()
    }

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id, TaskAssignment.state,
            TaskAssignment.claimed_at, TaskAssignment.completed_at,
        )
        .all()
    )

    agg: dict = {}  # kind -> bucket
    durations: dict = {}  # kind -> list of hours
    agents_seen: dict = {}  # kind -> set of agent_id
    for aid, state, claimed_at, completed_at in rows:
        kind = kind_map.get(aid, "unknown")
        bucket = agg.setdefault(kind, {
            "kind": kind, "total": 0, "done": 0, "failed": 0,
            "cancelled": 0, "expired": 0, "in_progress": 0,
        })
        bucket["total"] += 1
        agents_seen.setdefault(kind, set()).add(aid)
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            if claimed_at and completed_at and completed_at > claimed_at:
                durations.setdefault(kind, []).append((completed_at - claimed_at).total_seconds() / 3600)
        elif s == "failed":
            bucket["failed"] += 1
        elif s == "cancelled":
            bucket["cancelled"] += 1
        elif s == "expired":
            bucket["expired"] += 1
        else:
            bucket["in_progress"] += 1

    items = []
    for kind, b in agg.items():
        total = b["total"]
        done = b["done"]
        failed = b["failed"]
        ds = durations.get(kind, [])
        completion_rate = round(done / total * 100, 1) if total else 0
        failure_rate = round(failed / total * 100, 1) if total else 0
        items.append({
            "kind": kind,
            "agent_count": len(agents_seen.get(kind, set())),
            "total": total,
            "done": done,
            "failed": failed,
            "cancelled": b["cancelled"],
            "expired": b["expired"],
            "in_progress": b["in_progress"],
            "completion_rate": completion_rate,
            "failure_rate": failure_rate,
            "avg_completion_hours": round(sum(ds) / len(ds), 2) if ds else None,
        })
    # 完成率降序，失败率升序
    items.sort(key=lambda x: (-x["completion_rate"], x["failure_rate"]))

    return ApiResponse.success({"days": days, "items": items}).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/sandbox-execution", methods=["GET"])
@unified_auth_required
def get_step_sandbox_execution(run_id, step_key):
    """Get the sandbox execution (if any) bound to a workflow step run.

    This surfaces the auto-started sandboxed execution created when the step's
    agent had an active sandbox policy.
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    execution = SandboxExecution.query.filter_by(step_run_id=sr.id).order_by(
        SandboxExecution.created_at.desc()
    ).first()
    if not execution:
        return ApiResponse.success({"execution": None}, "No sandboxed execution for this step").to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.not_found("Sandboxed execution not found").to_response()
    return ApiResponse.success({
        "execution": execution.to_dict(include_violations=True),
        "sandbox": sandbox.to_dict(),
        "policy": execution.policy_snapshot or sandbox.to_policy_dict(),
    }).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/sandbox-violation", methods=["POST"])
@unified_auth_required
def report_step_sandbox_violation(run_id, step_key):
    """Report a sandbox policy violation for a workflow step's execution.

    If terminate_step is true, the step is marked FAILED and the sandboxed
    execution is marked VIOLATED. Otherwise only the violation is recorded
    and the step continues.
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    execution = SandboxExecution.query.filter_by(step_run_id=sr.id).order_by(
        SandboxExecution.created_at.desc()
    ).first()
    if not execution:
        return ApiResponse.error("No sandboxed execution for this step").to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.not_found("Sandboxed execution not found").to_response()
    body = validate_json_request()
    vtype = body.get("violation_type")
    try:
        vtype_enum = SandboxViolationType(vtype)
    except ValueError:
        return ApiResponse.error(f"Invalid violation_type: {vtype}").to_response()
    v = execution.record_violation(
        vtype_enum,
        detail=body.get("detail", ""),
        attempted_action=body.get("attempted_action"),
    )
    terminate = body.get("terminate_step", False)
    terminated = False
    if terminate and execution.status == SandboxExecutionStatus.RUNNING:
        execution.finish(
            SandboxExecutionStatus.VIOLATED,
            reason=f"Policy violation: {vtype_enum.value}",
        )
        # Mark the step as failed and cancel the bound run
        now = datetime.utcnow()
        sr.status = StepStatus.FAILED
        sr.error = f"Sandbox violation: {vtype_enum.value} — {body.get('detail', '')}"
        sr.finished_at = now
        if sr.assignment_id:
            old_assignment = TaskAssignment.query.get(sr.assignment_id)
            if old_assignment and old_assignment.state in LEASED_EXECUTION_STATES:
                old_assignment.state = TaskAssignmentState.CANCELLED
                old_assignment.completed_at = now
        bound_runs = AgentRun.query.filter_by(
            assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
        ).all()
        for r in bound_runs:
            r.status = AgentRunStatus.FAILED
            r.ended_at = now
            r.error = sr.error
        terminated = True
    AuditLog.record(
        action="sandbox.step_violation", resource_type="sandbox_violation", resource_id=v.id,
        actor_type="human", actor_user_id=user.id, project_id=wf_run.project_id,
        detail={"run_id": run_id, "step_key": step_key, "violation_type": vtype_enum.value,
                "agent_id": execution.agent_id, "terminated": terminated},
    )
    db.session.commit()
    if terminated:
        # Re-advance the workflow so downstream steps / failure handling proceed
        _advance_workflow(wf_run)
        db.session.commit()
    _queue_sse(user.id, "sandbox_step_violation", {
        "run_id": run_id, "step_key": step_key,
        "violation_type": vtype_enum.value, "terminated": terminated,
    })
    flush_sse_notifications()
    return ApiResponse.success({
        "violation": v.to_dict(),
        "execution": execution.to_dict(),
        "step_terminated": terminated,
    }, "Violation recorded" + (" and step terminated" if terminated else "")).to_response()


# ---------------------------------------------------------------------------
# Increment 88: Workflow step dynamic reconfiguration (runtime overrides)
# ---------------------------------------------------------------------------


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/override", methods=["PUT"])
@unified_auth_required
def set_step_runtime_override(run_id, step_key):
    """Dynamically reconfigure a not-yet-terminal step within a running workflow.

    Sets runtime overrides on the WorkflowStepRun that take precedence over the
    workflow definition when the step starts. Only allowed while the step is
    still PENDING/WAITING (not yet started) — except for timeout_seconds, which
    may be adjusted on a RUNNING step.

    Body: { overrides: { agent_id?, required_capabilities?, timeout_seconds?,
             retry_count?, on_failure?, condition?, task_template_id?,
             sub_workflow_id? }, merge?: bool (default true) }
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    if sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
        return ApiResponse.error(f"Cannot override a terminal step (status={sr.status.value})").to_response()

    body = validate_json_request()
    overrides = body.get("overrides") or {}
    if not isinstance(overrides, dict) or not overrides:
        return ApiResponse.error("overrides must be a non-empty object").to_response()

    # Validate keys and value types
    validated = {}
    for k, v in overrides.items():
        if k not in _RUNTIME_OVERRIDABLE_KEYS:
            return ApiResponse.error(f"Cannot override '{k}' (not in allowlist)").to_response()
        if k in ("agent_id", "task_template_id", "sub_workflow_id", "timeout_seconds", "retry_count"):
            if v is not None:
                try:
                    validated[k] = int(v)
                except (TypeError, ValueError):
                    return ApiResponse.error(f"{k} must be an integer or null").to_response()
            else:
                validated[k] = None
        elif k == "required_capabilities":
            if not isinstance(v, list):
                return ApiResponse.error("required_capabilities must be a list").to_response()
            validated[k] = v
        elif k == "on_failure":
            if v not in ("abort", "skip", "continue"):
                return ApiResponse.error("on_failure must be abort|skip|continue").to_response()
            validated[k] = v
        elif k == "condition":
            if v is not None and not isinstance(v, dict):
                return ApiResponse.error("condition must be an object or null").to_response()
            validated[k] = v

    # Reject reconfig of start-time-only params on a RUNNING step
    start_time_only = {"agent_id", "required_capabilities", "task_template_id", "sub_workflow_id", "condition", "on_failure"}
    if sr.status == StepStatus.RUNNING:
        forbidden = set(validated.keys()) & start_time_only
        if forbidden:
            return ApiResponse.error(
                f"Cannot override {sorted(forbidden)} on a running step (only timeout_seconds/retry_count allowed)"
            ).to_response()

    merge = body.get("merge", True)
    if merge:
        current = dict(sr.runtime_overrides or {})
        current.update(validated)
        sr.runtime_overrides = current
    else:
        sr.runtime_overrides = validated

    AuditLog.record(
        action="workflow_step_overridden", resource_type="workflow_step_run", resource_id=sr.id,
        actor_type="human", actor_user_id=user.id, project_id=wf_run.project_id,
        detail={"run_id": run_id, "step_key": step_key, "overrides": validated, "merge": merge},
    )
    db.session.commit()
    _queue_sse(user.id, "workflow_step_overridden", {
        "run_id": run_id, "step_key": step_key, "overrides": validated,
    })
    flush_sse_notifications()

    # Compute effective params for the response
    effective = {}
    for k in _RUNTIME_OVERRIDABLE_KEYS:
        effective[k] = sr.get_effective_param(k)
    return ApiResponse.success({
        "step_run": sr.to_dict(),
        "overrides": sr.runtime_overrides or {},
        "effective_params": effective,
    }, "Step runtime overrides applied").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/override", methods=["DELETE"])
@unified_auth_required
def clear_step_runtime_override(run_id, step_key):
    """Clear runtime overrides for a step run, reverting to the workflow definition."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    sr.runtime_overrides = {}
    AuditLog.record(
        action="workflow_step_override_cleared", resource_type="workflow_step_run", resource_id=sr.id,
        actor_type="human", actor_user_id=user.id, project_id=wf_run.project_id,
        detail={"run_id": run_id, "step_key": step_key},
    )
    db.session.commit()
    effective = {}
    for k in _RUNTIME_OVERRIDABLE_KEYS:
        effective[k] = sr.get_effective_param(k)
    return ApiResponse.success({
        "step_run": sr.to_dict(),
        "effective_params": effective,
    }, "Step runtime overrides cleared").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/effective-params", methods=["GET"])
@unified_auth_required
def get_step_effective_params(run_id, step_key):
    """Get the effective parameters for a step run (overrides merged with definition)."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    effective = {}
    for k in _RUNTIME_OVERRIDABLE_KEYS:
        effective[k] = sr.get_effective_param(k)
    return ApiResponse.success({
        "step_run": sr.to_dict(),
        "overrides": sr.runtime_overrides or {},
        "effective_params": effective,
    }).to_response()


# ---------------------------------------------------------------------------
# Increment 89: Agent collaboration conflict detection & resolution
# ---------------------------------------------------------------------------

# How long a protocol may stay open without resolution before flagging deadlock.
_PROTOCOL_DEADLOCK_HOURS = 48


def _detect_duplicate_claims(user, now):
    """Detect tasks with more than one active assignment (duplicate claims)."""
    conflicts = []
    # Find task_ids with >1 active assignment
    from sqlalchemy import func
    dupes = (
        db.session.query(TaskAssignment.task_id, func.count(TaskAssignment.id).label("cnt"))
        .join(Task, TaskAssignment.task_id == Task.id)
        .filter(
            Task.creator_id == user.id,
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
        )
        .group_by(TaskAssignment.task_id)
        .having(func.count(TaskAssignment.id) > 1)
        .all()
    )
    for task_id, cnt in dupes:
        assignments = TaskAssignment.query.filter_by(task_id=task_id).filter(
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES)
        ).order_by(TaskAssignment.created_at.asc()).all()
        agent_ids = list({a.agent_id for a in assignments if a.agent_id})
        # Avoid duplicate conflict records for the same task
        existing = AgentConflict.query.filter_by(
            owner_id=user.id, conflict_type=ConflictType.DUPLICATE_CLAIM,
            task_id=task_id, status=ConflictStatus.DETECTED,
        ).first()
        if existing:
            continue
        # Suggest highest reputation wins
        winner = None
        best_score = -1
        for aid in agent_ids:
            rep = AgentReputation.query.filter_by(agent_id=aid).first()
            score = rep.score if rep and rep.score else 50
            if score > best_score:
                best_score = score
                winner = aid
        conflicts.append(AgentConflict(
            owner_id=user.id,
            conflict_type=ConflictType.DUPLICATE_CLAIM,
            severity=ConflictSeverity.CRITICAL,
            status=ConflictStatus.DETECTED,
            task_id=task_id,
            agent_ids=agent_ids,
            title=f"重复认领: 任务 #{task_id} 有 {cnt} 个活跃分配",
            description=f"任务 #{task_id} 同时被 {cnt} 个 Agent 活跃分配，可能导致重复执行。",
            evidence={"assignment_count": cnt, "assignments": [
                {"assignment_id": a.id, "agent_id": a.agent_id, "state": a.state.value if a.state else None, "created_at": a.created_at.isoformat() if a.created_at else None}
                for a in assignments
            ]},
            suggested_strategy=ConflictResolutionStrategy.HIGHEST_REPUTATION,
        ))
    return conflicts


def _detect_assignment_stale(user, now):
    """Detect assignments whose lease has expired but state is still active."""
    conflicts = []
    stale = (
        TaskAssignment.query
        .join(Task, TaskAssignment.task_id == Task.id)
        .filter(
            Task.creator_id == user.id,
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            TaskAssignment.lease_expires_at.isnot(None),
            TaskAssignment.lease_expires_at < now,
        )
        .all()
    )
    for a in stale:
        existing = AgentConflict.query.filter_by(
            owner_id=user.id, conflict_type=ConflictType.ASSIGNMENT_STALE,
            task_id=a.task_id, status=ConflictStatus.DETECTED,
        ).first()
        if existing:
            continue
        conflicts.append(AgentConflict(
            owner_id=user.id,
            conflict_type=ConflictType.ASSIGNMENT_STALE,
            severity=ConflictSeverity.WARNING,
            status=ConflictStatus.DETECTED,
            task_id=a.task_id,
            agent_ids=[a.agent_id] if a.agent_id else [],
            title=f"过期分配: 任务 #{a.task_id} 分配 #{a.id}",
            description=f"分配 #{a.id} 的租约已于 {a.lease_expires_at.isoformat() if a.lease_expires_at else '?'} 过期，但状态仍为 {a.state.value if a.state else '?'}。",
            evidence={"assignment_id": a.id, "expired_at": a.lease_expires_at.isoformat() if a.lease_expires_at else None},
            suggested_strategy=ConflictResolutionStrategy.AUTO_RETRY,
        ))
    return conflicts


def _detect_protocol_deadlock(user, now):
    """Detect protocols that have been open too long without resolution."""
    conflicts = []
    cutoff = now - timedelta(hours=_PROTOCOL_DEADLOCK_HOURS)
    stuck = CollaborationProtocol.query.filter(
        CollaborationProtocol.owner_id == user.id,
        CollaborationProtocol.status.in_([ProtocolStatus.OPEN, ProtocolStatus.VOTING]),
        CollaborationProtocol.created_at < cutoff,
    ).all()
    for p in stuck:
        existing = AgentConflict.query.filter_by(
            owner_id=user.id, conflict_type=ConflictType.PROTOCOL_DEADLOCK,
            protocol_id=p.id, status=ConflictStatus.DETECTED,
        ).first()
        if existing:
            continue
        msg_count = ProtocolMessage.query.filter_by(protocol_id=p.id).count()
        conflicts.append(AgentConflict(
            owner_id=user.id,
            conflict_type=ConflictType.PROTOCOL_DEADLOCK,
            severity=ConflictSeverity.WARNING,
            status=ConflictStatus.DETECTED,
            protocol_id=p.id,
            agent_ids=[],
            title=f"协议僵局: 协议 #{p.id} 开放超过 {_PROTOCOL_DEADLOCK_HOURS}h",
            description=f"协议 #{p.id} (类型 {p.protocol_type.value if p.protocol_type else '?'}) 已开放 {((now - p.created_at).total_seconds() / 3600):.1f} 小时仍未决议，共 {msg_count} 条消息。",
            evidence={"protocol_id": p.id, "open_hours": round((now - p.created_at).total_seconds() / 3600, 1), "message_count": msg_count},
            suggested_strategy=ConflictResolutionStrategy.ESCALATE,
        ))
    return conflicts


@agents_bp.route("/conflicts/scan", methods=["POST"])
@unified_auth_required
def scan_conflicts():
    """Run a conflict detection scan for the current user.

    Detects duplicate claims, stale assignments, and protocol deadlocks.
    Creates AgentConflict records for newly-detected issues (skips duplicates).
    """
    user = get_current_user()
    now = datetime.utcnow()
    detected = []
    detected.extend(_detect_duplicate_claims(user, now))
    detected.extend(_detect_assignment_stale(user, now))
    detected.extend(_detect_protocol_deadlock(user, now))
    for c in detected:
        db.session.add(c)
    db.session.commit()
    if detected:
        _queue_sse(user.id, "conflicts_detected", {"count": len(detected)})
        flush_sse_notifications()
        AuditLog.record(
            action="conflicts.scan",
            resource_type="system",
            resource_id=0,
            actor_type="human",
            actor_user_id=user.id,
            detail={"detected": len(detected), "types": [c.conflict_type.value for c in detected]},
            ip_address=_client_ip(),
        )
    return ApiResponse.success({
        "detected": len(detected),
        "conflicts": [c.to_dict() for c in detected],
    }, f"Scan complete: {len(detected)} new conflict(s) detected").to_response()


@agents_bp.route("/conflicts", methods=["GET"])
@unified_auth_required
def list_conflicts():
    """List conflicts for the current user, optionally filtered."""
    user = get_current_user()
    q = AgentConflict.query.filter_by(owner_id=user.id)
    status_filter = request.args.get("status")
    if status_filter:
        try:
            q = q.filter_by(status=ConflictStatus(status_filter))
        except ValueError:
            pass
    type_filter = request.args.get("type")
    if type_filter:
        try:
            q = q.filter_by(conflict_type=ConflictType(type_filter))
        except ValueError:
            pass
    active_only = request.args.get("active_only", "true").lower() == "true"
    if active_only and not status_filter:
        q = q.filter(AgentConflict.status.in_([
            ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING
        ]))
    q = q.order_by(AgentConflict.created_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=20)
    items = [c.to_dict() for c in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/conflicts/<int:conflict_id>", methods=["GET"])
@unified_auth_required
def get_conflict(conflict_id):
    """Get a conflict by ID."""
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    return ApiResponse.success({"conflict": c.to_dict()}).to_response()


@agents_bp.route("/conflicts/<int:conflict_id>/acknowledge", methods=["POST"])
@unified_auth_required
def acknowledge_conflict(conflict_id):
    """Mark a conflict as acknowledged (seen by owner)."""
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    c.status = ConflictStatus.ACKNOWLEDGED
    db.session.commit()
    return ApiResponse.success({"conflict": c.to_dict()}, "Conflict acknowledged").to_response()


@agents_bp.route("/conflicts/<int:conflict_id>/ignore", methods=["POST"])
@unified_auth_required
def ignore_conflict(conflict_id):
    """Dismiss a conflict without action."""
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    c.status = ConflictStatus.IGNORED
    c.resolution = "Dismissed by owner"
    c.resolved_at = datetime.utcnow()
    c.resolved_by_user_id = user.id
    AuditLog.record(
        action="conflict.ignore", resource_type="agent_conflict", resource_id=c.id,
        actor_type="human", actor_user_id=user.id,
        detail={"conflict_type": c.conflict_type.value if c.conflict_type else None},
    )
    db.session.commit()
    return ApiResponse.success({"conflict": c.to_dict()}, "Conflict ignored").to_response()


@agents_bp.route("/conflicts/<int:conflict_id>/resolve", methods=["POST"])
@unified_auth_required
def resolve_conflict(conflict_id):
    """Resolve a conflict with a chosen strategy.

    Body: { strategy, description? }
    For DUPLICATE_CLAIM + FIRST_WINS/HIGHEST_REPUTATION/LEAST_LOADED, this also
    revokes the losing assignments. For ASSIGMENT_STALE + AUTO_RETRY, it cancels
    the stale assignment.
    """
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    body = validate_json_request()
    strategy_str = body.get("strategy")
    try:
        strategy = ConflictResolutionStrategy(strategy_str)
    except ValueError:
        return ApiResponse.error(f"Invalid strategy: {strategy_str}").to_response()
    now = datetime.utcnow()
    actions = []

    # Automated side-effects for specific conflict/strategy combos
    if c.conflict_type == ConflictType.DUPLICATE_CLAIM and c.task_id:
        assignments = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES)
        ).order_by(TaskAssignment.created_at.asc()).all()
        winner_id = None
        if strategy == ConflictResolutionStrategy.FIRST_WINS and assignments:
            winner_id = assignments[0].id
        elif strategy == ConflictResolutionStrategy.HIGHEST_REPUTATION:
            best = None
            best_score = -1
            for a in assignments:
                rep = AgentReputation.query.filter_by(agent_id=a.agent_id).first()
                score = rep.score if rep and rep.score else 50
                if score > best_score:
                    best_score = score
                    best = a
            winner_id = best.id if best else None
        elif strategy == ConflictResolutionStrategy.LEAST_LOADED:
            best = None
            least = None
            for a in assignments:
                cnt = TaskAssignment.query.filter(
                    TaskAssignment.agent_id == a.agent_id,
                    TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                ).count()
                if least is None or cnt < least:
                    least = cnt
                    best = a
            winner_id = best.id if best else None
        if winner_id is not None:
            for a in assignments:
                if a.id != winner_id:
                    a.state = TaskAssignmentState.CANCELLED
                    a.completed_at = now
                    actions.append(f"cancelled assignment #{a.id} (agent {a.agent_id})")

    elif c.conflict_type == ConflictType.ASSIGNMENT_STALE and c.task_id:
        if strategy in (ConflictResolutionStrategy.AUTO_RETRY, ConflictResolutionStrategy.FIRST_WINS):
            stale_assignments = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
                TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                TaskAssignment.lease_expires_at.isnot(None),
                TaskAssignment.lease_expires_at < now,
            ).all()
            for a in stale_assignments:
                a.state = TaskAssignmentState.EXPIRED
                a.completed_at = now
                actions.append(f"expired stale assignment #{a.id}")

    c.resolve(strategy, body.get("description") or "; ".join(actions) or "Resolved manually", resolved_by_user_id=user.id)
    AuditLog.record(
        action="conflict.resolve", resource_type="agent_conflict", resource_id=c.id,
        actor_type="human", actor_user_id=user.id,
        detail={"strategy": strategy.value, "conflict_type": c.conflict_type.value if c.conflict_type else None,
                "actions": actions},
    )
    db.session.commit()
    _queue_sse(user.id, "conflict_resolved", {"conflict_id": conflict_id, "strategy": strategy.value})
    flush_sse_notifications()
    return ApiResponse.success({
        "conflict": c.to_dict(),
        "actions": actions,
    }, "Conflict resolved").to_response()


@agents_bp.route("/conflicts/dashboard", methods=["GET"])
@unified_auth_required
def conflicts_dashboard():
    """Aggregate conflict stats for the current user."""
    user = get_current_user()
    qs = AgentConflict.query.filter_by(owner_id=user.id)
    total = qs.count()
    by_type = {}
    by_status = {}
    by_severity = {}
    for ct in ConflictType:
        by_type[ct.value] = qs.filter_by(conflict_type=ct).count()
    for cs in ConflictStatus:
        by_status[cs.value] = qs.filter_by(status=cs).count()
    for sev in ConflictSeverity:
        by_severity[sev.value] = qs.filter_by(severity=sev).count()
    active = qs.filter(AgentConflict.status.in_([
        ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING
    ])).count()

    # Resolution latency stats: how long conflicts sit before being cleared.
    # Buckets: <1h, 1-24h, 1-7d, >7d. Reveals whether conflicts languish.
    resolved_rows = qs.filter(
        AgentConflict.resolved_at.isnot(None),
    ).with_entities(AgentConflict.created_at, AgentConflict.resolved_at).all()
    latencies = []
    for created, resolved in resolved_rows:
        if created and resolved and resolved > created:
            latencies.append((resolved - created).total_seconds())
    latency_stats = {"count": len(latencies), "avg_seconds": None,
                     "median_seconds": None, "max_seconds": None,
                     "by_bucket": {"under_1h": 0, "1h_to_24h": 0, "1d_to_7d": 0, "over_7d": 0}}
    if latencies:
        latencies.sort()
        latency_stats["avg_seconds"] = round(sum(latencies) / len(latencies), 1)
        mid = len(latencies) // 2
        latency_stats["median_seconds"] = round(latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2, 1)
        latency_stats["max_seconds"] = round(latencies[-1], 1)
        for s in latencies:
            if s < 3600:
                latency_stats["by_bucket"]["under_1h"] += 1
            elif s < 86400:
                latency_stats["by_bucket"]["1h_to_24h"] += 1
            elif s < 604800:
                latency_stats["by_bucket"]["1d_to_7d"] += 1
            else:
                latency_stats["by_bucket"]["over_7d"] += 1

    return ApiResponse.success({
        "total": total,
        "active": active,
        "by_type": by_type,
        "by_status": by_status,
        "by_severity": by_severity,
        "resolution_latency": latency_stats,
    }).to_response()


@agents_bp.route("/conflicts/by-agent", methods=["GET"])
@unified_auth_required
def conflicts_by_agent():
    """Per-Agent conflict involvement counts for the current user.

    Each conflict carries a JSON ``agent_ids`` list of parties; this expands
    those lists and counts, per Agent: total conflicts, active conflicts, and
    conflicts where the Agent appeared. Returns the top N by total, enriched
    with the Agent's name/kind for display. Reveals which Agents are most
    conflict-prone.
    """
    user = get_current_user()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    rows = AgentConflict.query.filter_by(owner_id=user.id).with_entities(
        AgentConflict.agent_ids, AgentConflict.status
    ).all()
    active_statuses = {ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING}
    agg: dict = {}
    for agent_ids, status in rows:
        for aid in (agent_ids or []):
            entry = agg.setdefault(aid, {"agent_id": aid, "total": 0, "active": 0})
            entry["total"] += 1
            if status in active_statuses:
                entry["active"] += 1

    top = sorted(agg.values(), key=lambda x: x["total"], reverse=True)[:limit]
    agent_ids = [e["agent_id"] for e in top]
    agents = {a.id: a for a in Agent.query.filter(Agent.id.in_(agent_ids)).all()} if agent_ids else {}
    for e in top:
        a = agents.get(e["agent_id"])
        e["name"] = a.name if a else None
        e["kind"] = a.kind.value if a and a.kind else None
    return ApiResponse.success({"items": top}).to_response()


@agents_bp.route("/conflicts/strategy-stats", methods=["GET"])
@unified_auth_required
def conflicts_strategy_stats():
    """Resolution strategy effectiveness for the current user.

    For each ``ConflictResolutionStrategy`` actually used (resolution_strategy
    set on a resolved/ignored conflict): usage count, and the recurrence rate
    — the fraction of conflicts resolved with that strategy whose ``task_id``
    later saw another conflict. A high recurrence rate flags strategies that
    suppress rather than solve. Conflicts without a task_id are excluded from
    recurrence calculation (cannot be linked to a later conflict).
    """
    user = get_current_user()
    rows = AgentConflict.query.filter_by(owner_id=user.id).filter(
        AgentConflict.resolution_strategy.isnot(None)
    ).with_entities(
        AgentConflict.resolution_strategy, AgentConflict.task_id
    ).all()

    usage: dict = {}
    task_strategies: list = []  # (task_id, strategy) for recurrence join
    for strat, task_id in rows:
        if strat is None:
            continue
        s = strat.value if hasattr(strat, "value") else str(strat)
        entry = usage.setdefault(s, {"strategy": s, "uses": 0, "recurrences": 0, "with_task": 0})
        entry["uses"] += 1
        if task_id is not None:
            entry["with_task"] += 1
            task_strategies.append((task_id, s))

    # Recurrence: a task that had a conflict resolved with strategy S, then
    # later had *any* conflict again. Count distinct tasks per strategy.
    all_task_conflicts = AgentConflict.query.filter_by(owner_id=user.id).filter(
        AgentConflict.task_id.isnot(None)
    ).with_entities(AgentConflict.task_id).all()
    task_conflict_count: dict = {}
    for (tid,) in all_task_conflicts:
        task_conflict_count[tid] = task_conflict_count.get(tid, 0) + 1

    seen_tasks: dict = {}
    for tid, s in task_strategies:
        if tid in seen_tasks:
            continue
        seen_tasks[tid] = s
        if task_conflict_count.get(tid, 0) > 1:
            usage[s]["recurrences"] += 1

    items = []
    for s, entry in usage.items():
        denom = entry["with_task"] or 1
        items.append({
            "strategy": s,
            "uses": entry["uses"],
            "with_task": entry["with_task"],
            "recurrences": entry["recurrences"],
            "recurrence_rate": round(entry["recurrences"] / denom, 3),
        })
    items.sort(key=lambda x: x["uses"], reverse=True)
    return ApiResponse.success({"items": items}).to_response()


@agents_bp.route("/conflicts/trend", methods=["GET"])
@unified_auth_required
def conflicts_trend():
    """Daily conflict detection vs resolution counts for the current user.

    Buckets by calendar day (UTC). ``detected`` uses ``created_at`` (when the
    scan found the conflict), ``resolved`` uses ``resolved_at`` (filled on
    resolve/ignore/auto-resolve). Useful for charting whether conflicts are
    accumulating faster than they are being cleared.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    from sqlalchemy import func as sa_func
    daily_detected = (
        db.session.query(
            sa_func.date(AgentConflict.created_at).label("date"),
            sa_func.count(AgentConflict.id).label("detected"),
        )
        .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
        .group_by(sa_func.date(AgentConflict.created_at))
        .all()
    )
    daily_resolved = (
        db.session.query(
            sa_func.date(AgentConflict.resolved_at).label("date"),
            sa_func.count(AgentConflict.id).label("resolved"),
        )
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.resolved_at.isnot(None),
            AgentConflict.resolved_at >= since,
        )
        .group_by(sa_func.date(AgentConflict.resolved_at))
        .all()
    )

    trend_map: dict = {}
    for d, c in daily_detected:
        key = str(d)
        trend_map[key] = {"date": key, "detected": c, "resolved": 0}
    for d, c in daily_resolved:
        key = str(d)
        if key in trend_map:
            trend_map[key]["resolved"] = c
        else:
            trend_map[key] = {"date": key, "detected": 0, "resolved": c}
    trend = sorted(trend_map.values(), key=lambda x: x["date"])
    return ApiResponse.success({
        "days": days,
        "trend": trend,
    }).to_response()


# Strategies considered safe to apply automatically (low-risk, reversible).
_AUTO_SAFE_STRATEGIES = {
    ConflictResolutionStrategy.AUTO_RETRY,
    ConflictResolutionStrategy.LEAST_LOADED,
}


@agents_bp.route("/maintenance/auto-resolve-conflicts", methods=["POST"])
@unified_auth_required
def auto_resolve_conflicts():
    """Maintenance endpoint: scan for conflicts and auto-resolve low-severity
    ones using their suggested strategy, when that strategy is in the safe set.

    CRITICAL conflicts are never auto-resolved — they require human judgement.
    Returns counts of scanned / auto-resolved / skipped.
    """
    user = get_current_user()
    now = datetime.utcnow()
    # Run detection first to surface any new conflicts
    detected = []
    detected.extend(_detect_duplicate_claims(user, now))
    detected.extend(_detect_assignment_stale(user, now))
    detected.extend(_detect_protocol_deadlock(user, now))
    for c in detected:
        db.session.add(c)
    db.session.flush()

    # Collect all DETECTED conflicts eligible for auto-resolution
    candidates = AgentConflict.query.filter(
        AgentConflict.owner_id == user.id,
        AgentConflict.status == ConflictStatus.DETECTED,
        AgentConflict.severity != ConflictSeverity.CRITICAL,
    ).all()

    auto_resolved = []
    skipped = []
    for c in candidates:
        strategy = c.suggested_strategy
        if strategy is None or strategy not in _AUTO_SAFE_STRATEGIES:
            skipped.append({"conflict_id": c.id, "reason": "no safe suggested strategy", "suggested": strategy.value if strategy else None})
            continue
        actions = []
        # Apply the same side-effects as the manual resolve endpoint
        if c.conflict_type == ConflictType.DUPLICATE_CLAIM and c.task_id:
            assignments = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES)
            ).order_by(TaskAssignment.created_at.asc()).all()
            if strategy == ConflictResolutionStrategy.LEAST_LOADED and assignments:
                best = None
                least = None
                for a in assignments:
                    cnt = TaskAssignment.query.filter(
                        TaskAssignment.agent_id == a.agent_id,
                        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                    ).count()
                    if least is None or cnt < least:
                        least = cnt
                        best = a
                if best:
                    for a in assignments:
                        if a.id != best.id:
                            a.state = TaskAssignmentState.CANCELLED
                            a.completed_at = now
                            actions.append(f"auto-cancelled assignment #{a.id}")
        elif c.conflict_type == ConflictType.ASSIGNMENT_STALE and c.task_id:
            if strategy == ConflictResolutionStrategy.AUTO_RETRY:
                stale = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
                    TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                    TaskAssignment.lease_expires_at.isnot(None),
                    TaskAssignment.lease_expires_at < now,
                ).all()
                for a in stale:
                    a.state = TaskAssignmentState.EXPIRED
                    a.completed_at = now
                    actions.append(f"auto-expired stale assignment #{a.id}")
        c.resolve(strategy, "Auto-resolved by maintenance scan" + (f": {' '.join(actions)}" if actions else ""), resolved_by_user_id=None)
        auto_resolved.append({"conflict_id": c.id, "strategy": strategy.value, "actions": actions})

    db.session.commit()
    if auto_resolved:
        _queue_sse(user.id, "conflicts_auto_resolved", {"count": len(auto_resolved)})
        flush_sse_notifications()
        AuditLog.record(
            action="conflicts.auto_resolve",
            resource_type="system",
            resource_id=0,
            actor_type="system",
            detail={"detected": len(detected), "auto_resolved": len(auto_resolved), "skipped": len(skipped)},
            ip_address=_client_ip(),
        )
    return ApiResponse.success({
        "detected": len(detected),
        "auto_resolved": len(auto_resolved),
        "skipped": len(skipped),
        "resolved_details": auto_resolved,
        "skipped_details": skipped,
    }, f"Auto-resolve: {len(auto_resolved)} resolved, {len(skipped)} skipped").to_response()


# =========================================================================
# Global collaboration orchestrator
# =========================================================================


@agents_bp.route("/maintenance/orchestrate", methods=["POST"])
@unified_auth_required
def orchestrate():
    """Global collaboration orchestrator: runs the full collaboration
    maintenance cycle in a single call. Designed to be invoked by an external
    scheduler (cron) every few minutes to drive the multi-Agent platform
    without manual per-endpoint triggering.

    Stages (executed in order, each isolated so a failure in one stage does
    not abort the others):
      1. Health: mark stale agents offline, expire stale leases, escalate
         overdue tasks.
      2. Workflow timeout: mark timed-out steps FAILED and re-advance
         affected workflows.
      3. Trigger firing: launch workflow runs for any due triggers.
      4. Conflict resolution: detect new conflicts and auto-resolve
         low-severity ones with safe strategies.

    Returns a per-stage summary plus an overall duration.
    """
    user = get_current_user()
    report, duration, message = _run_orchestration(user, actor_type="human")
    try:
        from core.orchestrator_scheduler import record_last_run
        record_last_run(report, duration, message)
    except Exception:
        pass  # best-effort: status endpoint is non-critical
    return ApiResponse.success(
        {**report, "duration_seconds": round(duration, 3)},
        message,
    ).to_response()


@agents_bp.route("/maintenance/orchestrator/status", methods=["GET"])
@unified_auth_required
def orchestrator_status():
    """Return the built-in scheduler state and the last orchestration cycle
    summary (if the scheduler is enabled)."""
    try:
        from core.orchestrator_scheduler import scheduler_status
        status = scheduler_status()
    except Exception as e:
        return ApiResponse.error(f"Scheduler status unavailable: {str(e)}").to_response()
    return ApiResponse.success(status, "Orchestrator status").to_response()


@agents_bp.route("/maintenance/orchestrator/history", methods=["GET"])
@unified_auth_required
def orchestrator_history():
    """Return recent orchestration run records for trend analysis.

    Query params: limit (default 20, max 100), triggered_by (manual|scheduler).
    """
    user = get_current_user()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    q = OrchestrationRun.query.filter_by(owner_id=user.id)
    tb = request.args.get("triggered_by")
    if tb in ("manual", "scheduler"):
        q = q.filter(OrchestrationRun.triggered_by == tb)
    runs = q.order_by(OrchestrationRun.created_at.desc()).limit(limit).all()
    # Trend aggregates
    items = [r.to_dict() for r in runs]
    return ApiResponse.success({
        "items": items,
        "count": len(items),
        "trend": {
            "total_runs": len(items),
            "manual_runs": sum(1 for i in items if i.get("triggered_by") == "manual"),
            "scheduler_runs": sum(1 for i in items if i.get("triggered_by") == "scheduler"),
            "avg_duration": round(sum(i.get("duration_seconds", 0) for i in items) / len(items), 3) if items else 0,
            "total_errors": sum(i.get("error_count", 0) for i in items),
            "total_conflicts_resolved": sum(i.get("conflicts_auto_resolved", 0) for i in items),
            "total_triggers_fired": sum(i.get("triggers_fired", 0) for i in items),
        },
    }, "Orchestrator history").to_response()


@agents_bp.route("/maintenance/orchestrator/daily-trend", methods=["GET"])
@unified_auth_required
def orchestrator_daily_trend():
    """Daily aggregation of orchestration runs for trend visualization.

    Buckets OrchestrationRun records by the date portion of created_at,
    aligned with the security events daily-trend time dimension so the two
    can be rendered on a unified timeline. Optional filters: triggered_by
    (manual|scheduler), since, until (ISO date/datetime, inclusive).
    Returns:
      {
        days: [{date, runs, manual_runs, scheduler_runs, triggers_fired,
                conflicts_resolved, errors, avg_duration}],
        totals: {runs, manual_runs, scheduler_runs, triggers_fired,
                 conflicts_resolved, errors}
      }
    """
    user = get_current_user()
    q = OrchestrationRun.query.filter_by(owner_id=user.id)
    tb = request.args.get("triggered_by")
    if tb in ("manual", "scheduler"):
        q = q.filter(OrchestrationRun.triggered_by == tb)
    since = request.args.get("since")
    if since:
        q = q.filter(OrchestrationRun.created_at >= since)
    until = request.args.get("until")
    if until:
        q = q.filter(OrchestrationRun.created_at <= until)
    runs = q.order_by(OrchestrationRun.created_at.desc()).limit(1000).all()

    buckets = {}  # date -> accumulators
    for r in runs:
        ts = (r.created_at.isoformat() if r.created_at else "")
        day = ts[:10] if len(ts) >= 10 else None
        if not day:
            continue
        b = buckets.setdefault(day, {
            "runs": 0, "manual_runs": 0, "scheduler_runs": 0,
            "triggers_fired": 0, "conflicts_resolved": 0, "errors": 0,
            "duration_sum": 0.0,
        })
        b["runs"] += 1
        if r.triggered_by == "manual":
            b["manual_runs"] += 1
        elif r.triggered_by == "scheduler":
            b["scheduler_runs"] += 1
        b["triggers_fired"] += r.triggers_fired or 0
        b["conflicts_resolved"] += r.conflicts_auto_resolved or 0
        b["errors"] += r.error_count or 0
        b["duration_sum"] += r.duration_seconds or 0.0

    sorted_days = sorted(buckets.items(), key=lambda kv: kv[0])
    days = []
    for d, b in sorted_days:
        days.append({
            "date": d,
            "runs": b["runs"],
            "manual_runs": b["manual_runs"],
            "scheduler_runs": b["scheduler_runs"],
            "triggers_fired": b["triggers_fired"],
            "conflicts_resolved": b["conflicts_resolved"],
            "errors": b["errors"],
            "avg_duration": round(b["duration_sum"] / b["runs"], 3) if b["runs"] else 0,
        })
    totals = {
        "runs": sum(d["runs"] for d in days),
        "manual_runs": sum(d["manual_runs"] for d in days),
        "scheduler_runs": sum(d["scheduler_runs"] for d in days),
        "triggers_fired": sum(d["triggers_fired"] for d in days),
        "conflicts_resolved": sum(d["conflicts_resolved"] for d in days),
        "errors": sum(d["errors"] for d in days),
    }
    return ApiResponse.success(
        data={"days": days, "totals": totals},
        message="Orchestrator daily trend",
    ).to_response()


def _run_orchestration(user, actor_type="human"):
    """Core orchestration logic, reusable by both the HTTP endpoint and the
    built-in background scheduler. Returns (report_dict, duration_seconds, message).
    Does NOT call get_current_user(); the caller supplies the user scope.
    """
    start = datetime.utcnow()
    report = {
        "stale_agents": 0,
        "stale_agent_ids": [],
        "expired_leases": 0,
        "escalated_tasks": 0,
        "escalated_task_ids": [],
        "timed_out_steps": 0,
        "triggers_fired": 0,
        "trigger_run_ids": [],
        "conflicts_detected": 0,
        "conflicts_auto_resolved": 0,
        "conflicts_skipped": 0,
        "errors": [],
    }

    # --- Stage 1: health (stale agents, expired leases, overdue escalation) ---
    try:
        now = datetime.utcnow()
        stale_agents = mark_stale_agents_offline(owner_id=user.id)
        report["stale_agents"] = len(stale_agents)
        report["stale_agent_ids"] = [a.id for a in stale_agents]

        expired_assignments = TaskAssignment.query.filter(
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            TaskAssignment.lease_expires_at.isnot(None),
            TaskAssignment.lease_expires_at < now,
        ).join(Task).join(Project).filter(Project.owner_id == user.id).all()
        for assignment in expired_assignments:
            assignment.state = TaskAssignmentState.EXPIRED
            assignment.completed_at = now
            for run in AgentRun.query.filter_by(
                assignment_id=assignment.id, status=AgentRunStatus.RUNNING
            ).all():
                run.status = AgentRunStatus.EXPIRED
                run.ended_at = now
        report["expired_leases"] = len(expired_assignments)

        escalated_ids = _escalate_overdue_tasks(owner_id=user.id)
        report["escalated_tasks"] = len(escalated_ids)
        report["escalated_task_ids"] = escalated_ids
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"health: {str(e)}")

    # --- Stage 2: workflow step timeouts + re-advance ---
    try:
        now = datetime.utcnow()
        running_steps = WorkflowStepRun.query.filter(
            WorkflowStepRun.status == StepStatus.RUNNING,
        ).join(WorkflowRun).filter(
            WorkflowRun.owner_id == user.id,
            WorkflowRun.status == WorkflowStatus.RUNNING,
        ).all()
        timed_out = []
        for sr in running_steps:
            wf_run = sr.run
            if not wf_run or not wf_run.workflow:
                continue
            step_def = WorkflowStep.query.filter_by(
                workflow_id=wf_run.workflow_id, step_key=sr.step_key
            ).first()
            step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
            if not step_def or not step_def.timeout_seconds or step_def.timeout_seconds <= 0:
                continue
            if sr.started_at:
                elapsed = (now - sr.started_at).total_seconds()
                if elapsed > step_def.timeout_seconds:
                    sr.status = StepStatus.FAILED
                    sr.error = f"Step timed out after {int(elapsed)}s (limit: {step_def.timeout_seconds}s)"
                    sr.finished_at = now
                    try:
                        if sr.assignment_id:
                            bound_run = AgentRun.query.filter_by(
                                assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                            ).first()
                            if bound_run:
                                _maybe_finish_sandboxed_execution(
                                    bound_run, SandboxExecutionStatus.TIMEOUT,
                                    error=sr.error,
                                )
                    except Exception:
                        pass
                    timed_out.append({"run_id": wf_run.id, "step_key": sr.step_key})
        db.session.commit()
        for run_id in set(t["run_id"] for t in timed_out):
            wf_run = WorkflowRun.query.get(run_id)
            if wf_run:
                _advance_workflow(wf_run)
        db.session.commit()
        report["timed_out_steps"] = len(timed_out)
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"workflow_timeout: {str(e)}")

    # --- Stage 3: fire due triggers (system-wide, same as fire_due_triggers) ---
    try:
        now = datetime.utcnow()
        due_triggers = WorkflowTrigger.query.filter(
            WorkflowTrigger.is_active == True,
            WorkflowTrigger.next_fire_at != None,
            WorkflowTrigger.next_fire_at <= now,
        ).all()
        fired_run_ids = []
        for trigger in due_triggers:
            workflow = Workflow.query.get(trigger.workflow_id)
            if not workflow or not workflow.is_active:
                trigger.is_active = False
                continue
            wf_run = WorkflowRun.create(
                workflow_id=workflow.id,
                owner_id=trigger.owner_id,
                project_id=trigger.project_id,
                root_task_id=trigger.root_task_id,
                status=WorkflowStatus.PENDING,
                context=trigger.context_override or {},
            )
            db.session.flush()
            definition = workflow.definition or {}
            for step_def in definition.get("steps", []):
                WorkflowStepRun.create(
                    run_id=wf_run.id,
                    step_key=step_def.get("step_key", ""),
                    status=StepStatus.PENDING,
                )
            wf_run.status = WorkflowStatus.RUNNING
            _advance_workflow(wf_run)
            trigger.fire_count = (trigger.fire_count or 0) + 1
            trigger.last_fired_at = now
            if trigger.cron_expr:
                trigger.next_fire_at = _compute_next_fire(trigger.cron_expr, now)
            elif trigger.one_shot_at:
                trigger.is_active = False
                trigger.next_fire_at = None
            fired_run_ids.append(wf_run.id)
            AuditLog.record(
                action="workflow_trigger.fired", resource_type="workflow_trigger", resource_id=trigger.id,
                actor_type="system",
                detail={"workflow_run_id": wf_run.id, "fire_count": trigger.fire_count, "via": "orchestrator"},
            )
        db.session.commit()
        report["triggers_fired"] = len(fired_run_ids)
        report["trigger_run_ids"] = fired_run_ids
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"triggers: {str(e)}")

    # --- Stage 4: conflict detection + auto-resolution ---
    try:
        now = datetime.utcnow()
        detected = []
        detected.extend(_detect_duplicate_claims(user, now))
        detected.extend(_detect_assignment_stale(user, now))
        detected.extend(_detect_protocol_deadlock(user, now))
        for c in detected:
            db.session.add(c)
        db.session.flush()

        candidates = AgentConflict.query.filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.status == ConflictStatus.DETECTED,
            AgentConflict.severity != ConflictSeverity.CRITICAL,
        ).all()
        auto_resolved = 0
        skipped = 0
        for c in candidates:
            strategy = c.suggested_strategy
            if strategy is None or strategy not in _AUTO_SAFE_STRATEGIES:
                skipped += 1
                continue
            c.resolve(strategy, f"Auto-resolved by orchestrator via {strategy.value}",
                      resolved_by_user_id=user.id)
            auto_resolved += 1
        report["conflicts_detected"] = len(detected)
        report["conflicts_auto_resolved"] = auto_resolved
        report["conflicts_skipped"] = skipped
        if detected or auto_resolved:
            AuditLog.record(
                action="conflicts.auto_resolve", resource_type="system", resource_id=0,
                actor_type="system",
                detail={"detected": len(detected), "auto_resolved": auto_resolved,
                        "skipped": skipped, "via": "orchestrator"},
                ip_address=_client_ip(),
            )
            _queue_sse(user.id, "conflicts_auto_resolved", {
                "count": auto_resolved, "via": "orchestrator",
            })
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"conflicts: {str(e)}")

    flush_sse_notifications()
    duration = (datetime.utcnow() - start).total_seconds()
    message = (f"Orchestration complete: {report['stale_agents']} stale agent(s), "
               f"{report['timed_out_steps']} timed-out step(s), {report['triggers_fired']} trigger(s) fired, "
               f"{report['conflicts_auto_resolved']} conflict(s) auto-resolved"
               + (f", {len(report['errors'])} error(s)" if report["errors"] else ""))
    AuditLog.record(
        action="maintenance.orchestrate", resource_type="system", resource_id=0,
        actor_type=actor_type, actor_user_id=user.id,
        detail={**{k: v for k, v in report.items() if k != "errors"},
                "error_count": len(report["errors"]), "duration_seconds": duration},
        ip_address=_client_ip() if actor_type == "human" else None,
    )
    # Persist a historical record for trend analysis
    try:
        OrchestrationRun.record(
            owner_id=user.id,
            triggered_by="scheduler" if actor_type == "system" else "manual",
            report=report, duration=duration, summary=message,
        )
    except Exception:
        pass  # never fail the cycle on history-write error
    db.session.commit()
    return report, duration, message

