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
    AuditLog,
    Workflow,
    WorkflowStep,
    WorkflowVersion,
    _client_ip,
)
from .workflow_external_steps import normalize_incoming_integration_config


def _create_step(workflow_id, step_data, previous_config=None):
    """Create one WorkflowStep from an API payload (shared by POST / PUT)."""
    step_key = (step_data.get("step_key") or "").strip()
    if not step_key:
        return None
    integration_config = normalize_incoming_integration_config(
        step_data.get("integration_config"), previous=previous_config,
    )
    return WorkflowStep.create(
        workflow_id=workflow_id,
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
        integration_config=integration_config,
        timeout_seconds=step_data.get("timeout_seconds"),
        retry_count=step_data.get("retry_count", 0),
        on_failure=step_data.get("on_failure", "abort"),
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
    # get_request_args 返回普通 dict，带 type= 的过滤须走 request.args
    is_active = request.args.get("is_active", type=bool)
    query = Workflow.query.filter_by(owner_id=user.id)
    if is_active is not None:
        query = query.filter_by(is_active=is_active)
    query = query.order_by(Workflow.updated_at.desc())
    result = paginate_query(
        query, args["page"], args["per_page"],
        serializer=lambda w: w.to_dict(include_steps=True),
    )
    return ApiResponse.success(result).to_response()


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
    db.session.flush()  # 立即取 workflow.id——下方步骤创建依赖它（create 不 flush）

    # Create steps if provided
    for step_data in data.get("steps", []):
        try:
            step = _create_step(workflow.id, step_data)
        except ValueError as exc:
            db.session.rollback()
            return ApiResponse.error(str(exc), 400).to_response()
        if step is None:
            continue

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
        # Remember each old step's stored connector config so a masked
        # api_key round-trip (or an omitted key) can reuse the ciphertext.
        previous_configs = {s.step_key: s.integration_config for s in workflow.steps}
        # Delete existing steps
        for old_step in workflow.steps:
            db.session.delete(old_step)
        # Create new steps
        for step_data in data["steps"]:
            step_key = (step_data.get("step_key") or "").strip()
            try:
                step = _create_step(
                    workflow.id, step_data,
                    previous_config=previous_configs.get(step_key),
                )
            except ValueError as exc:
                db.session.rollback()
                return ApiResponse.error(str(exc), 400).to_response()
            if step is None:
                continue

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

# ── DSL 导入导出（借鉴 Dify app DSL：可移植/可分享，敏感字段清洗） ──────


@agents_bp.route("/workflows/<int:workflow_id>/export", methods=["GET"])
@unified_auth_required
def export_workflow_dsl_route(workflow_id):
    """Export a workflow as portable DSL. Returns {dsl_text, warnings}."""
    from .workflow_dsl import dumps_workflow_dsl, export_workflow_dsl

    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()
    dsl = export_workflow_dsl(workflow)
    warnings = dsl.pop("warnings", [])
    return ApiResponse.success({
        "dsl_text": dumps_workflow_dsl(dsl),
        "warnings": warnings,
    }, "Workflow exported").to_response()


@agents_bp.route("/workflows/import", methods=["POST"])
@unified_auth_required
def import_workflow_dsl_route():
    """Import a workflow from DSL text (YAML or JSON). Creates a new workflow.

    Body: {dsl_text: str, name?: str}  — name overrides the DSL workflow name.
    Connection api_keys are NOT part of the DSL; fill them in afterwards.
    """
    from .workflow_dsl import DslError, import_workflow_dsl, loads_workflow_dsl

    user = get_current_user()
    data = validate_json_request(required_fields=["dsl_text"], optional_fields=["name"])
    if isinstance(data, tuple):
        return data
    try:
        dsl = loads_workflow_dsl(data["dsl_text"])
        workflow, warnings = import_workflow_dsl(
            user.id, dsl, name_override=data.get("name"))
    except DslError as exc:
        return ApiResponse.error(str(exc), 400).to_response()
    db.session.commit()
    AuditLog.record(
        action="workflow.imported", resource_type="workflow",
        resource_id=workflow.id, actor_type="human", actor_user_id=user.id,
        detail={"name": workflow.name}, ip_address=_client_ip(),
    )
    db.session.commit()
    return ApiResponse.created(
        workflow.to_dict(include_steps=True),
        "Workflow imported" + (f"（{len(warnings)} 条警告）" if warnings else ""),
    ).to_response()


# ── 单步测试运行（借鉴 Dify single_step_run：不建运行记录调试单个步骤） ──


@agents_bp.route("/workflows/<int:workflow_id>/steps/<step_key>/test-run", methods=["POST"])
@unified_auth_required
def test_run_workflow_step(workflow_id, step_key):
    """Debug a single step without launching a run.

    Body: {instructions?: str, context?: object, timeout_seconds?: int}

    - agent 步骤：预览将被选中的 Agent 与任务内容（零副作用）
    - 连接器步骤（dify/coze）：真实调用远端工作流并返回结果
    """
    from .workflow_test_run import test_run_step

    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()
    step_def = WorkflowStep.query.filter_by(
        workflow_id=workflow.id, step_key=step_key).first()
    if not step_def:
        return ApiResponse.not_found("Step not found").to_response()

    # 测试运行允许空请求体（全部字段可选）
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return ApiResponse.error("Request body must be a JSON object", 400).to_response()
    try:
        result = test_run_step(
            workflow, step_def,
            instructions=str(data.get("instructions") or ""),
            context=data.get("context") if isinstance(data.get("context"), dict) else None,
            timeout_seconds=data.get("timeout_seconds"),
        )
    except Exception as exc:  # noqa: BLE001 — 测试运行失败直接作为结果返回
        return ApiResponse.error(f"测试运行失败: {exc}", 400).to_response()
    return ApiResponse.success(result, "Step test-run finished").to_response()
