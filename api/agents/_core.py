"""
Agent collaboration API — core routes.

Routes that don't clearly belong to a specific sub-module live here.
As the split progresses, more groups will be extracted into their own
files under ``api/agents/``.
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


@agents_bp.route("/workflows/run-duration-percentiles", methods=["GET"])
@unified_auth_required
def workflow_run_duration_percentiles():
    """Daily trend of workflow run duration percentiles (P50/P90/P95).

    For each day, aggregates completed WorkflowRun durations (finished_at -
    started_at in seconds) and returns P50, P90, P95. Useful for spotting
    regressions in workflow execution time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    cutoff = datetime.utcnow() - timedelta(days=days)
    runs = (
        db.session.query(
            func.date(WorkflowRun.finished_at).label("day"),
            WorkflowRun.finished_at,
            WorkflowRun.started_at,
        )
        .filter(
            WorkflowRun.status == WorkflowStatus.COMPLETED,
            WorkflowRun.finished_at >= cutoff,
            WorkflowRun.started_at.isnot(None),
            WorkflowRun.finished_at.isnot(None),
        )
        .order_by(func.date(WorkflowRun.finished_at))
        .all()
    )

    # Group by day
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in runs:
        dur = (r.finished_at - r.started_at).total_seconds()
        if dur >= 0:
            by_day[str(r.day)].append(dur)

    def percentile(sorted_vals, pct):
        n = len(sorted_vals)
        if n == 0:
            return 0
        idx = int(pct * (n - 1))
        return round(sorted_vals[idx], 1)

    buckets = []
    total_runs = 0
    total_duration = 0.0
    for day in sorted(by_day):
        vals = sorted(by_day[day])
        n = len(vals)
        total_runs += n
        total_duration += sum(vals)
        buckets.append({
            "date": day,
            "count": n,
            "p50": percentile(vals, 0.50),
            "p90": percentile(vals, 0.90),
            "p95": percentile(vals, 0.95),
            "median": percentile(vals, 0.50),
            "avg": round(sum(vals) / n, 1) if n else 0,
        })

    return ApiResponse.success({
        "buckets": buckets,
        "total_runs": total_runs,
        "total_avg_duration": round(total_duration / total_runs, 1) if total_runs else 0,
    }).to_response()


@agents_bp.route("/workflows/step-failure-rate", methods=["GET"])
@unified_auth_required
def workflow_step_failure_rate():
    """Per-step-key failure rate ranking.

    For each step_key, counts total step runs and failed ones,
    computing the failure rate percentage. Sorted by failure rate
    descending. Reveals which workflow steps are the least reliable.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days = 30
        limit = 15

    cutoff = datetime.utcnow() - timedelta(days=days)

    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(WorkflowRun.owner_id == user.id, WorkflowStepRun.created_at >= cutoff)
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.status,
        )
        .all()
    )

    from collections import defaultdict
    step_data: dict = defaultdict(lambda: {"total": 0, "failed": 0})
    for step_key, status in rows:
        if not step_key:
            continue
        step_data[step_key]["total"] += 1
        if status == StepStatus.FAILED:
            step_data[step_key]["failed"] += 1

    items = []
    total_steps = 0
    total_failed = 0
    for step_key, d in step_data.items():
        total_steps += d["total"]
        total_failed += d["failed"]
        items.append({
            "step_key": step_key,
            "total": d["total"],
            "failed": d["failed"],
            "failure_rate": round(d["failed"] / d["total"] * 100, 1) if d["total"] else 0.0,
        })
    items.sort(key=lambda x: x["failure_rate"], reverse=True)

    return ApiResponse.success({
        "items": items[:limit],
        "total_steps": total_steps,
        "total_failed": total_failed,
    }).to_response()


@agents_bp.route("/workflows/failed-steps/by-duration", methods=["GET"])
@unified_auth_required
def workflow_failed_steps_by_duration():
    """Rank failed workflow steps by average duration (finished - started).

    Only FAILED WorkflowStepRun rows with both timestamps are considered.
    For each ``step_key`` reports total failures, average/median/max duration
    in seconds, sorted by average duration descending. Reveals which failing
    steps burn the most wall-clock time before giving up.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        days = 30
        limit = 20

    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.started_at.isnot(None),
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.started_at,
            WorkflowStepRun.finished_at,
        )
        .all()
    )

    agg: dict = {}
    for step_key, started, finished in rows:
        if not (started and finished and finished > started):
            continue
        dur = (finished - started).total_seconds()
        entry = agg.setdefault(step_key, {"step_key": step_key, "durations": []})
        entry["durations"].append(dur)

    items = []
    for step_key, e in agg.items():
        ds = sorted(e["durations"])
        n = len(ds)
        avg = round(sum(ds) / n, 1)
        median = round(ds[n // 2], 1) if n % 2 == 1 else round((ds[n // 2 - 1] + ds[n // 2]) / 2, 1)
        items.append({
            "step_key": step_key,
            "failures": n,
            "avg_duration_seconds": avg,
            "median_duration_seconds": median,
            "max_duration_seconds": round(ds[-1], 1),
        })
    items.sort(key=lambda x: x["avg_duration_seconds"], reverse=True)
    return ApiResponse.success({
        "days": days,
        "total_failed_steps": sum(i["failures"] for i in items),
        "items": items[:limit],
    }).to_response()


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


@agents_bp.route("/workflows/step-cofailure-matrix", methods=["GET"])
@unified_auth_required
def workflow_step_cofailure_matrix():
    """Step-key co-failure matrix for the current user.

    For each failed workflow run, collects the set of failed step_keys.
    Builds a symmetric co-occurrence matrix: for each pair (step_a, step_b),
    counts how many runs both failed. Returns top N step_keys by failure
    count with the N×N matrix. Reveals which steps tend to fail together,
    indicating shared failure causes or cascading failures.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(2, min(15, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days = 30
        limit = 8

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]

    # Get all failed step runs in window, grouped by run_id
    failed_steps = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.agent_id.in_(agent_ids) if agent_ids else sa_false(),
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(WorkflowStepRun.run_id, WorkflowStepRun.step_key)
        .all()
    )

    # Group failed step_keys by run_id
    run_failed: dict = {}  # {run_id: set(step_keys)}
    for run_id, step_key in failed_steps:
        if run_id not in run_failed:
            run_failed[run_id] = set()
        if step_key:
            run_failed[run_id].add(step_key)

    # Count per-step failures and co-failure pairs
    step_fail_count: dict = {}  # {step_key: count}
    pair_count: dict = {}  # {(a, b): count} where a < b
    for step_keys in run_failed.values():
        keys = sorted(step_keys)
        for k in keys:
            step_fail_count[k] = step_fail_count.get(k, 0) + 1
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pair = (keys[i], keys[j])
                pair_count[pair] = pair_count.get(pair, 0) + 1

    if not step_fail_count:
        return ApiResponse.success({"step_keys": [], "matrix": {}, "max_cofailure": 0, "total_runs_with_multi_failure": 0}).to_response()

    # Top N step_keys by failure count
    top_keys = sorted(step_fail_count.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    top_key_list = [k for k, _ in top_keys]
    top_key_set = set(top_key_list)

    # Build matrix
    matrix: dict = {}  # {step_a: {step_b: count}}
    max_cofailure = 0
    for (a, b), c in pair_count.items():
        if a in top_key_set and b in top_key_set:
            matrix.setdefault(a, {})[b] = c
            matrix.setdefault(b, {})[a] = c
            if c > max_cofailure:
                max_cofailure = c

    total_multi = sum(1 for ks in run_failed.values() if len(ks) >= 2)

    return ApiResponse.success({
        "step_keys": [{"step_key": k, "failures": step_fail_count[k]} for k in top_key_list],
        "matrix": matrix,
        "max_cofailure": max_cofailure,
        "total_runs_with_multi_failure": total_multi,
    }).to_response()


@agents_bp.route("/workflows/step-retry-topology", methods=["GET"])
@unified_auth_required
def workflow_step_retry_topology():
    """Step retry topology for the current user's workflows.

    Groups WorkflowStepRun by (workflow_id, step_key) and counts attempts
    (attempt > 1). Returns per-step: total_runs, retry_count, retry_rate,
    first_attempt_success_rate, retry_success_rate. Shows whether retries
    actually recover failures.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(30, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days = 30
        limit = 15

    since = datetime.utcnow() - timedelta(days=days)

    # Get workflow IDs owned by user
    wf_ids = [wid for wid, in WorkflowRun.query.filter(
        WorkflowRun.owner_id == user.id,
        WorkflowRun.finished_at.isnot(None),
        WorkflowRun.finished_at >= since,
    ).with_entities(WorkflowRun.workflow_id).all()]

    if not wf_ids:
        return ApiResponse.success({"days": days, "steps": [], "total_retries": 0}).to_response()

    # Get step runs for those runs
    rows = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.run_id.in_(
                WorkflowRun.query.filter(
                    WorkflowRun.owner_id == user.id,
                    WorkflowRun.finished_at >= since,
                ).with_entities(WorkflowRun.id)
            ),
        )
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.attempt,
            WorkflowStepRun.status,
        )
        .all()
    )

    # Group by step_key
    step_data: dict = {}  # {step_key: {total_runs, retries, first_success, retry_success}}
    for step_key, attempt, status in rows:
        if not step_key:
            continue
        d = step_data.setdefault(step_key, {"total_runs": 0, "retries": 0, "first_success": 0, "retry_success": 0, "first_attempts": 0, "retry_attempts": 0})
        d["total_runs"] += 1
        s = status.value if status else ""
        if attempt == 1 or attempt is None:
            d["first_attempts"] += 1
            if s == "succeeded":
                d["first_success"] += 1
        else:
            d["retries"] += 1
            d["retry_attempts"] += 1
            if s == "succeeded":
                d["retry_success"] += 1

    # Sort by retry count desc, limit
    sorted_steps = sorted(step_data.items(), key=lambda kv: kv[1]["retries"], reverse=True)[:limit]
    total_retries = sum(d["retries"] for _, d in sorted_steps)
    steps_out = []
    for sk, d in sorted_steps:
        first_total = max(d["first_attempts"], 1)
        retry_total = max(d["retry_attempts"], 1)
        steps_out.append({
            "step_key": sk,
            "total_runs": d["total_runs"],
            "retries": d["retries"],
            "retry_rate": round(d["retries"] / max(d["total_runs"], 1) * 100, 1),
            "first_attempt_success_rate": round(d["first_success"] / first_total * 100, 1),
            "retry_success_rate": round(d["retry_success"] / retry_total * 100, 1) if d["retry_attempts"] else 0.0,
        })

    return ApiResponse.success({
        "days": days,
        "steps": steps_out,
        "total_retries": total_retries,
    }).to_response()


@agents_bp.route("/workflows/step-hourly-distribution", methods=["GET"])
@unified_auth_required
def workflow_step_hourly_distribution():
    """Step execution hour-of-day distribution for the current user.

    Groups WorkflowStepRun by (step_key, hour_of_day) based on
    started_at. Returns per-step hourly distribution revealing which
    steps run during business hours vs overnight.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.run_id.in_(
                WorkflowRun.query.filter(
                    WorkflowRun.owner_id == user.id,
                    WorkflowRun.finished_at >= since,
                ).with_entities(WorkflowRun.id)
            ),
            WorkflowStepRun.started_at.isnot(None),
        )
        .with_entities(WorkflowStepRun.step_key, WorkflowStepRun.started_at)
        .all()
    )

    step_hours: dict = {}  # {step_key: {hour: count}}
    step_total: dict = {}  # {step_key: total}
    for step_key, started_at in rows:
        if not step_key:
            continue
        h = started_at.hour
        bucket = step_hours.setdefault(step_key, {})
        bucket[h] = bucket.get(h, 0) + 1
        step_total[step_key] = step_total.get(step_key, 0) + 1

    # Top N by total executions
    top = sorted(step_total.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    steps_out = []
    for sk, total in top:
        hours = step_hours.get(sk, {})
        peak_hour = max(hours, key=hours.get) if hours else None
        # Business hours ratio (8-18)
        biz = sum(hours.get(h, 0) for h in range(8, 18))
        steps_out.append({
            "step_key": sk,
            "total": total,
            "hours": hours,
            "peak_hour": peak_hour,
            "business_hours_ratio": round(biz / total * 100, 1) if total else 0.0,
        })

    return ApiResponse.success({
        "days": days,
        "steps": steps_out,
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


@agents_bp.route("/workflows/success-rate-by-workflow", methods=["GET"])
@unified_auth_required
def workflow_success_rate_by_workflow():
    """Per-workflow run success rate comparison for the current user.

    Groups finished workflow runs by workflow_id. Per workflow: total runs,
    succeeded, failed, cancelled, success_rate, avg duration (seconds).
    Sorted by total runs descending, limited to top N. Reveals which
    workflows are the most/least reliable.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    # Get finished runs in window
    rows = (
        WorkflowRun.query
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowRun.finished_at.isnot(None),
            WorkflowRun.finished_at >= since,
        )
        .with_entities(
            WorkflowRun.workflow_id,
            WorkflowRun.status,
            WorkflowRun.started_at,
            WorkflowRun.finished_at,
        )
        .all()
    )

    # Resolve workflow names
    wf_ids = list(set(r.workflow_id for r in rows))
    name_map = {}
    if wf_ids:
        for wid, wname in db.session.query(Workflow.id, Workflow.name).filter(Workflow.id.in_(wf_ids)).all():
            name_map[wid] = wname or f"Workflow#{wid}"

    wf_data: dict = {}  # {wf_id: {total, succeeded, failed, cancelled, dur_sum, dur_n}}
    for wid, status, started, finished in rows:
        if wid not in wf_data:
            wf_data[wid] = {"total": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "dur_sum": 0.0, "dur_n": 0}
        wf_data[wid]["total"] += 1
        s = status.value if status else ""
        if s == "succeeded":
            wf_data[wid]["succeeded"] += 1
        elif s == "failed":
            wf_data[wid]["failed"] += 1
        elif s == "cancelled":
            wf_data[wid]["cancelled"] += 1
        if started and finished:
            dur = (finished - started).total_seconds()
            wf_data[wid]["dur_sum"] += dur
            wf_data[wid]["dur_n"] += 1

    items = []
    for wid, d in sorted(wf_data.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]:
        total = d["total"]
        items.append({
            "workflow_id": wid,
            "name": name_map.get(wid, f"Workflow#{wid}"),
            "total": total,
            "succeeded": d["succeeded"],
            "failed": d["failed"],
            "cancelled": d["cancelled"],
            "success_rate": round(d["succeeded"] / total * 100, 1) if total else 0.0,
            "avg_duration": round(d["dur_sum"] / d["dur_n"], 1) if d["dur_n"] else 0.0,
        })

    return ApiResponse.success({"workflows": items}).to_response()


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


# Global collaboration orchestrator
# =========================================================================




@agents_bp.route("/workflows/step-dependency-bottleneck", methods=["GET"])
@unified_auth_required
def workflow_step_dependency_bottleneck():
    """Identify bottleneck steps in workflow DAG critical paths.

    For each workflow with step dependency information, computes:
    - The critical path (longest total duration path through the DAG)
    - Average duration per step across completed runs
    - Bottleneck score: step's share of total critical path time

    Returns per-workflow critical path with step durations and bottleneck scores.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    # Find workflows owned by user that have step definitions with depends_on
    workflows = (
        Workflow.query
        .filter(Workflow.owner_id == user.id)
        .all()
    )

    results = []
    for wf in workflows:
        steps = wf.steps or []
        if not steps:
            continue

        # Build step_key -> depends_on mapping from definitions
        step_defs = {}  # step_key -> {depends_on: [...], name: ...}
        for s in steps:
            dep = s.depends_on or []
            if not isinstance(dep, list):
                dep = []
            step_defs[s.step_key] = {"depends_on": dep, "name": s.name or s.step_key}

        # Only analyze workflows with at least one dependency edge
        has_dep = any(v["depends_on"] for v in step_defs.values())
        if not has_dep:
            continue

        # Get average duration per step_key from completed step runs
        step_dur_rows = (
            db.session.query(
                WorkflowStepRun.step_key,
                func.avg(
                    func.extract("epoch", WorkflowStepRun.finished_at - WorkflowStepRun.started_at)
                ),
            )
            .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
            .filter(
                WorkflowRun.owner_id == user.id,
                WorkflowRun.workflow_id == wf.id,
                WorkflowStepRun.started_at.isnot(None),
                WorkflowStepRun.finished_at.isnot(None),
                WorkflowStepRun.started_at >= since,
            )
            .group_by(WorkflowStepRun.step_key)
            .all()
        )
        avg_durations = {row[0]: float(row[1]) if row[1] else 0.0 for row in step_dur_rows}

        # Only include steps that have actual execution data
        active_steps = {k: v for k, v in step_defs.items() if k in avg_durations}
        if not active_steps:
            continue

        # Topological sort using Kahn's algorithm
        in_degree = {k: 0 for k in active_steps}
        adj = {k: [] for k in active_steps}  # dep -> [dependents]
        for sk, info in active_steps.items():
            for dep in info["depends_on"]:
                if dep in active_steps:
                    in_degree[sk] += 1
                    adj[dep].append(sk)

        queue = [k for k, d in in_degree.items() if d == 0]
        topo_order = []
        while queue:
            node = queue.pop(0)
            topo_order.append(node)
            for nb in adj[node]:
                in_degree[nb] -= 1
                if in_degree[nb] == 0:
                    queue.append(nb)

        # If cycle detected, skip this workflow
        if len(topo_order) != len(active_steps):
            continue

        # Compute longest path (critical path) using DP
        # dist[sk] = longest total duration to reach sk
        dist = {k: 0.0 for k in active_steps}
        parent = {k: None for k in active_steps}
        for sk in topo_order:
            for dep in active_steps[sk]["depends_on"]:
                if dep in active_steps:
                    candidate = dist[dep] + avg_durations.get(sk, 0.0)
                    if candidate > dist[sk]:
                        dist[sk] = candidate
                        parent[sk] = dep
            # If no dependencies, dist = own duration
            if not active_steps[sk]["depends_on"] or all(d not in active_steps for d in active_steps[sk]["depends_on"]):
                dist[sk] = max(dist[sk], avg_durations.get(sk, 0.0))

        # Find the endpoint with the longest distance
        end_node = max(topo_order, key=lambda k: dist[k]) if topo_order else None
        if end_node is None:
            continue

        # Trace back the critical path
        critical_path = []
        cur = end_node
        visited = set()
        while cur is not None and cur not in visited:
            visited.add(cur)
            critical_path.append(cur)
            cur = parent[cur]
        critical_path.reverse()

        total_cp_duration = sum(avg_durations.get(sk, 0.0) for sk in critical_path)
        if total_cp_duration <= 0:
            continue

        path_steps = []
        for sk in critical_path:
            dur = avg_durations.get(sk, 0.0)
            path_steps.append({
                "step_key": sk,
                "name": active_steps[sk]["name"],
                "depends_on": active_steps[sk]["depends_on"],
                "avg_duration": round(dur, 1),
                "bottleneck_score": round(dur / total_cp_duration * 100, 1),
            })

        # Also include all steps with duration for reference
        all_steps_info = []
        for sk, info in sorted(active_steps.items(), key=lambda kv: avg_durations.get(kv[0], 0.0), reverse=True):
            all_steps_info.append({
                "step_key": sk,
                "name": info["name"],
                "depends_on": info["depends_on"],
                "avg_duration": round(avg_durations.get(sk, 0.0), 1),
                "is_on_critical_path": sk in critical_path,
            })

        results.append({
            "workflow_id": wf.id,
            "workflow_name": wf.name or f"Workflow#{wf.id}",
            "critical_path": path_steps,
            "critical_path_duration": round(total_cp_duration, 1),
            "all_steps": all_steps_info,
            "total_steps": len(step_defs),
            "active_steps": len(active_steps),
        })

    # Sort by critical path duration descending, limit
    results.sort(key=lambda r: r["critical_path_duration"], reverse=True)
    return ApiResponse.success({"workflows": results[:limit]}).to_response()














