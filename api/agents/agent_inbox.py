"""
Agent collaboration API - inbox and notification routes.

Agent @mention inbox (directed task events) and persistent user notifications.
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
    AuditLog,
    Notification,
    Project,
    Task,
    TaskEvent,
    get_request_args,
    validate_json_request,
    get_owned_agent_or_response,
    flush_sse_notifications,
    _client_ip,
)


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

