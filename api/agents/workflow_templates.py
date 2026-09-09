"""Built-in workflow template routes（workflow-templates）。"""

from flask import request

from workflow_templates import WORKFLOW_TEMPLATES

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    AuditLog,
    Workflow,
    WorkflowStep,
    validate_json_request,
    _client_ip,
)

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
    if not isinstance(data, dict):
        return data  # validate_json_request 的错误响应（Response 对象）

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
            db.session.flush()  # 先取 wf.id，再建 steps
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
