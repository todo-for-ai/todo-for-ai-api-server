"""
Workflow API routes.

CRUD operations for workflows.
Extracted from api/agents/_core.py for better organization.
"""

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    validate_json_request,
    get_request_args,
    paginate_query,
    get_current_user,
    unified_auth_required,
    db,
    Workflow,
    WorkflowStep,
    WorkflowVersion,
)


def _workflow_owned_by_user(workflow_id, user):
    """Return the workflow if it belongs to *user*, else None."""
    return Workflow.query.filter_by(id=workflow_id, owner_id=user.id).first()


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