"""
Agent collaboration API — task operations routes.

Task events, handoffs, subtasks, inbox, notifications, shared context, and run logs.
"""

import csv
import io
from datetime import datetime, timedelta

from flask import make_response, request

from workflow_templates import WORKFLOW_TEMPLATES

from ._shared import (  # noqa: E402
    agents_bp,
    ApiResponse,
    get_request_args,
    paginate_query,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
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
    notify_sse,
    _pending_sse,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    CAPABILITY_TOKEN_PATTERN,
    LOCKED_TERMINAL_ASSIGNMENT_STATES,
    POSTABLE_EVENT_TYPES,
    POSTABLE_EVENT_CONTENT_MAX,
    AGENT_ASSIGNMENT_TARGET_STATES,
    HUMAN_ACTIVE_TARGET_STATES,
    HUMAN_REVIEW_TARGET_STATES,
    HUMAN_WAITING_TARGET_STATES,
    HUMAN_DONE_TARGET_STATES,
    ACTIVE_ASSIGNMENT_STATES,
    AssignmentUpdateError,
    parse_enum,
    get_owned_agent_or_response,
    get_owned_task_or_response,
    serialize_claim_response,
    serialize_review_queue_item,
    paginate_serialized,
    record_task_event,
    record_assignment_update_rejected,
    parse_requested_update_enums,
    validate_agent_assignment_update,
    validate_human_assignment_update,
    build_task_snapshot,
    active_assignment_filter,
    expire_assignment,
    expire_stale_assignments,
    normalize_match_terms,
    score_task_for_agent,
    _expand_capabilities,
    expire_stale_assignments_for_task,
    find_active_assignment,
    find_claimable_task,
    _score_task_with_caps,
    _check_parent_blocking,
    apply_assignment_update,
    _CAPABILITY_HIERARCHY,
)


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



