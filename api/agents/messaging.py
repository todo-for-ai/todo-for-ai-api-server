"""
Agent collaboration API — messaging and collaboration template routes.

Broadcast messages, direct messages, collaborators, workflow triggers,
workflow templates, and collaboration templates.
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




