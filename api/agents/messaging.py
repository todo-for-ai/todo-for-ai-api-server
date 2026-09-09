"""
Agent collaboration API — messaging routes.

Broadcast messages, direct messages, message feed, and collaborator
aggregation.（workflow-triggers / workflow-templates / collaboration-templates
已拆分至各自模块。）
"""

from datetime import datetime

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentStatus,
    AgentChannel,
    AuditLog,
    Notification,
    Project,
    Task,
    TaskEvent,
    get_request_args,
    validate_json_request,
    record_task_event,
    get_owned_agent_or_response,
    _client_ip,
    _queue_sse,
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
        if not isinstance(data, dict):
            return data  # validate_json_request 的错误响应（Response 对象）

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
        if not isinstance(data, dict):
            return data  # validate_json_request 的错误响应（Response 对象）

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
