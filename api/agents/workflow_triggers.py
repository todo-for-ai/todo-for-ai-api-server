"""Workflow trigger CRUD routes（workflow-triggers）。"""

from datetime import datetime

from flask import request

from .dashboard import _compute_next_fire
from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    AuditLog,
    Workflow,
    WorkflowTrigger,
    get_request_args,
    paginate_query,
    validate_json_request,
    _client_ip,
)

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
    if not isinstance(data, dict):
        return data  # validate_json_request 的错误响应（Response 对象）

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
    if not isinstance(data, dict):
        return data  # validate_json_request 的错误响应（Response 对象）

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
