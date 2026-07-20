"""
Agent collaboration API - shared context routes.

Per-task shared context entries: list, upsert, delete for cross-Agent
state sharing during collaboration.
"""

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    Project,
    SharedContext,
    Task,
    validate_json_request,
)


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


