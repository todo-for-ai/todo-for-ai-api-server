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



