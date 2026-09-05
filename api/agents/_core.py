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
            optional_fields=["name", "description", "kind", "status", "provider", "model", "capabilities", "config", "collaboration_role", "role_template_id"],
        )

        if isinstance(data, tuple):
            return data

        # 岗位角色绑定：校验模板存在且为内置或同工作区
        if "role_template_id" in data:
            from models import AgentRoleTemplate, AgentRoleTemplateStatus
            raw_role = data["role_template_id"]
            if raw_role in (None, "", 0):
                data["role_template_id"] = None
            else:
                template = db.session.get(AgentRoleTemplate, int(raw_role))
                if not template or template.status != AgentRoleTemplateStatus.ACTIVE:
                    return ApiResponse.error("Role template not found or inactive", 400).to_response()
                if not template.is_builtin and template.workspace_id != agent.workspace_id:
                    return ApiResponse.error("Role template does not belong to this agent workspace", 400).to_response()
                data["role_template_id"] = template.id

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


# =========================================================================
# Agent broadcast messaging
# =========================================================================
