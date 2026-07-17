"""
Agent sandbox CRUD, templates, executions, violations, and analytics endpoints.
"""

from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxLevel,
    SandboxViolationType,
    SandboxExecutionStatus,
    WorkflowStepRun,
    WorkflowRun,
    AuditLog,
    get_request_args,
    paginate_query,
    parse_enum,
)

SANDBOX_TEMPLATES = [
    {
        "key": "read_only_research",
        "name": "只读研究",
        "description": "严格隔离：无网络、无写盘、仅允许只读工具。适合信息检索与分析类任务。",
        "security_level": "strict",
        "allowed_tools": ["search", "read_file", "list_files"],
        "blocked_tools": [],
        "allowed_network_hosts": [],
        "fs_write_paths": [],
        "fs_read_paths": [],
        "max_memory_mb": 256,
        "max_cpu_seconds": 120,
        "max_output_tokens": 8000,
        "timeout_seconds": 300,
    },
    {
        "key": "code_generation",
        "name": "代码生成",
        "description": "中等隔离：受限网络（仅文档站）、范围写盘、允许代码生成工具。适合编码类任务。",
        "security_level": "moderate",
        "allowed_tools": ["read_file", "write_file", "run_tests", "search"],
        "blocked_tools": ["delete_file", "execute_shell"],
        "allowed_network_hosts": ["docs.python.org", "developer.mozilla.org", "registry.npmjs.org"],
        "fs_write_paths": ["/tmp/work", "/workspace/src"],
        "fs_read_paths": ["/workspace", "/data/in"],
        "max_memory_mb": 512,
        "max_cpu_seconds": 600,
        "max_output_tokens": 16000,
        "timeout_seconds": 900,
    },
    {
        "key": "data_analysis",
        "name": "数据分析",
        "description": "中等隔离：允许读取数据源、写入输出目录、网络访问数据 API。适合数据处理任务。",
        "security_level": "moderate",
        "allowed_tools": ["read_file", "write_file", "query_database", "http_get"],
        "blocked_tools": ["execute_shell", "delete_file"],
        "allowed_network_hosts": ["api.data.example.com"],
        "fs_write_paths": ["/data/out", "/tmp/analysis"],
        "fs_read_paths": ["/data"],
        "max_memory_mb": 1024,
        "max_cpu_seconds": 1800,
        "max_output_tokens": 32000,
        "timeout_seconds": 1800,
    },
    {
        "key": "full_autonomy",
        "name": "完全自主",
        "description": "宽松隔离：全网络、全盘、仅黑名单危险工具。适合受信任的自主执行场景。",
        "security_level": "permissive",
        "allowed_tools": [],
        "blocked_tools": ["rm_rf", "format_disk", "shutdown"],
        "allowed_network_hosts": [],
        "fs_write_paths": [],
        "fs_read_paths": [],
        "max_memory_mb": 2048,
        "max_cpu_seconds": 3600,
        "max_output_tokens": 64000,
        "timeout_seconds": 3600,
    },
    {
        "key": "sandboxed_review",
        "name": "沙盒评审",
        "description": "严格隔离：无网络无写盘，仅允许读取和评审工具，短超时。适合代码/文档评审。",
        "security_level": "strict",
        "allowed_tools": ["read_file", "list_files", "comment"],
        "blocked_tools": [],
        "allowed_network_hosts": [],
        "fs_write_paths": [],
        "fs_read_paths": ["/workspace"],
        "max_memory_mb": 128,
        "max_cpu_seconds": 60,
        "max_output_tokens": 4000,
        "timeout_seconds": 180,
    },
]


@agents_bp.route("/sandbox-templates", methods=["GET"])
@unified_auth_required
def list_sandbox_templates():
    """List preset sandbox policy templates."""
    return ApiResponse.success({"templates": SANDBOX_TEMPLATES}).to_response()


@agents_bp.route("/sandbox-templates/<template_key>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_sandbox_template(template_key):
    """Create a sandbox policy from a preset template.

    Body (optional): { name?, agent_id?, overrides?: {...} }
    """
    user = get_current_user()
    template = next((t for t in SANDBOX_TEMPLATES if t["key"] == template_key), None)
    if not template:
        return ApiResponse.not_found("Sandbox template not found").to_response()
    body = validate_json_request() or {}
    overrides = body.get("overrides") or {}
    # Merge template with overrides
    fields = {k: v for k, v in template.items() if k != "key"}
    fields["name"] = body.get("name") or f"{template['name']} (副本)"
    if body.get("agent_id") is not None:
        fields["agent_id"] = body.get("agent_id")
        agent = Agent.query.get(fields["agent_id"])
        if not agent or agent.owner_id != user.id:
            return ApiResponse.error("Agent not found or not owned by you").to_response()
    else:
        fields["agent_id"] = None
    # Apply overrides for overridable fields
    for k in ("allowed_tools", "blocked_tools", "allowed_network_hosts", "fs_write_paths", "fs_read_paths",
              "max_memory_mb", "max_cpu_seconds", "max_output_tokens", "timeout_seconds", "security_level", "description"):
        if k in overrides and overrides[k] is not None:
            fields[k] = overrides[k]
    validated, err = _sandbox_body(fields)
    if err:
        return ApiResponse.error(err).to_response()
    sandbox = AgentSandbox(owner_id=user.id, **validated)
    db.session.add(sandbox)
    AuditLog.record(
        action="sandbox.template_instantiate", resource_type="agent_sandbox", resource_id=None,
        actor_type="human", actor_user_id=user.id,
        detail={"template_key": template_key, "agent_id": fields.get("agent_id"),
                "security_level": fields.get("security_level")},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_created", {"sandbox_id": sandbox.id, "from_template": template_key})
    flush_sse_notifications()
    return ApiResponse.success(sandbox.to_dict(include_stats=True), f"Sandbox created from template '{template['name']}'").to_response()


@agents_bp.route("/sandboxes", methods=["GET"])
@unified_auth_required
def list_sandboxes():
    """List sandbox policies owned by the current user (optionally filtered by agent)."""
    user = get_current_user()
    q = AgentSandbox.query.filter_by(owner_id=user.id)
    agent_id = request.args.get("agent_id", type=int)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    active_only = request.args.get("active_only", type=str)
    if active_only and active_only.lower() == "true":
        q = q.filter_by(is_active=True)
    include_stats = request.args.get("include_stats", "true").lower() == "true"
    q = q.order_by(AgentSandbox.created_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=20)
    items = [s.to_dict(include_stats=include_stats) for s in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/sandboxes", methods=["POST"])
@unified_auth_required
def create_sandbox():
    """Create a new sandbox policy."""
    user = get_current_user()
    body = validate_json_request()
    fields, err = _sandbox_body(body)
    if err:
        return ApiResponse.error(err).to_response()
    if fields["agent_id"] is not None:
        agent = Agent.query.get(fields["agent_id"])
        if not agent or agent.owner_id != user.id:
            return ApiResponse.error("Agent not found or not owned by you").to_response()
    sandbox = AgentSandbox(owner_id=user.id, **fields)
    db.session.add(sandbox)
    db.session.commit()
    _queue_sse(user.id, "sandbox_created", {"sandbox_id": sandbox.id, "agent_id": sandbox.agent_id})
    flush_sse_notifications()
    return ApiResponse.success(sandbox.to_dict(include_stats=True), "Sandbox created").to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>", methods=["GET"])
@unified_auth_required
def get_sandbox(sandbox_id):
    """Get a sandbox policy by ID."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    return ApiResponse.success(sandbox.to_dict(include_stats=True)).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>", methods=["PUT"])
@unified_auth_required
def update_sandbox(sandbox_id):
    """Update an existing sandbox policy."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    body = validate_json_request()
    fields, err = _sandbox_body(body, partial=True)
    if err:
        return ApiResponse.error(err).to_response()
    if fields["agent_id"] is not None:
        agent = Agent.query.get(fields["agent_id"])
        if not agent or agent.owner_id != user.id:
            return ApiResponse.error("Agent not found or not owned by you").to_response()
    for k, v in fields.items():
        if v is not None or k in ("is_active", "description"):
            setattr(sandbox, k, v)
    AuditLog.record(
        action="sandbox.update", resource_type="agent_sandbox", resource_id=sandbox_id,
        actor_type="human", actor_user_id=user.id,
        detail={"changed_fields": [k for k, v in fields.items() if v is not None or k in ("is_active", "description")]},
    )
    db.session.commit()
    return ApiResponse.success(sandbox.to_dict(include_stats=True), "Sandbox updated").to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>", methods=["DELETE"])
@unified_auth_required
def delete_sandbox(sandbox_id):
    """Delete a sandbox policy (only if no active executions)."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    active = SandboxExecution.query.filter_by(
        sandbox_id=sandbox_id, status=SandboxExecutionStatus.RUNNING
    ).count()
    if active:
        return ApiResponse.error(f"Cannot delete: {active} active execution(s) reference this sandbox").to_response()
    AuditLog.record(
        action="sandbox.delete", resource_type="agent_sandbox", resource_id=sandbox_id,
        actor_type="human", actor_user_id=user.id,
        detail={"name": sandbox.name, "security_level": sandbox.security_level.value if sandbox.security_level else None,
                "agent_id": sandbox.agent_id},
    )
    db.session.delete(sandbox)
    db.session.commit()
    return ApiResponse.success({"deleted": True}, "Sandbox deleted").to_response()


@agents_bp.route("/<int:agent_id>/sandbox", methods=["GET"])
@unified_auth_required
def get_agent_sandbox(agent_id):
    """Get the active sandbox bound to an agent."""
    user = get_current_user()
    agent = Agent.query.get(agent_id)
    if not agent or agent.owner_id != user.id:
        return ApiResponse.error("Agent not found or not owned by you", 404).to_response()
    sandbox = AgentSandbox.get_for_agent(agent_id)
    if not sandbox:
        return ApiResponse.success({"sandbox": None}, "No active sandbox bound to this agent").to_response()
    return ApiResponse.success({"sandbox": sandbox.to_dict(include_stats=True)}).to_response()


@agents_bp.route("/<int:agent_id>/sandbox/bind", methods=["POST"])
@unified_auth_required
def bind_agent_sandbox(agent_id):
    """Bind a sandbox policy to an agent (replaces any existing active binding)."""
    user = get_current_user()
    agent = Agent.query.get(agent_id)
    if not agent or agent.owner_id != user.id:
        return ApiResponse.error("Agent not found or not owned by you", 404).to_response()
    body = validate_json_request()
    sandbox_id = body.get("sandbox_id")
    if not sandbox_id:
        return ApiResponse.error("sandbox_id is required").to_response()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    # Deactivate any other active sandboxes bound to this agent
    AgentSandbox.query.filter(
        AgentSandbox.agent_id == agent_id,
        AgentSandbox.is_active == True,
        AgentSandbox.id != sandbox_id,
    ).update({"is_active": False})
    sandbox.agent_id = agent_id
    sandbox.is_active = True
    AuditLog.record(
        action="sandbox.bind", resource_type="agent_sandbox", resource_id=sandbox_id,
        actor_type="human", actor_user_id=user.id,
        detail={"agent_id": agent_id, "security_level": sandbox.security_level.value if sandbox.security_level else None},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_bound", {"agent_id": agent_id, "sandbox_id": sandbox_id})
    flush_sse_notifications()
    return ApiResponse.success({"sandbox": sandbox.to_dict(include_stats=True)}, "Sandbox bound to agent").to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/policy", methods=["GET"])
@unified_auth_required
def get_sandbox_policy(sandbox_id):
    """Get the serializable policy envelope for an executor to consume."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    return ApiResponse.success({"policy": sandbox.to_policy_dict()}).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/check", methods=["POST"])
@unified_auth_required
def check_sandbox_action(sandbox_id):
    """Dry-run check of an action against a sandbox policy (no execution).

    Body: { action: "tool"|"network"|"fs_write", target: <tool_name|host|path> }
    Returns whether the action is permitted and why.
    """
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    body = validate_json_request()
    action = body.get("action")
    target = body.get("target")
    if not action or not target:
        return ApiResponse.error("action and target are required").to_response()
    if action == "tool":
        allowed, reason = sandbox.check_tool(target)
    elif action == "network":
        allowed, reason = sandbox.check_network(target)
    elif action == "fs_write":
        allowed, reason = sandbox.check_fs_write(target)
    else:
        return ApiResponse.error("action must be tool, network, or fs_write").to_response()
    return ApiResponse.success({"allowed": allowed, "reason": reason, "action": action, "target": target}).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/executions", methods=["GET"])
@unified_auth_required
def list_sandbox_executions(sandbox_id):
    """List executions under a sandbox policy."""
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    q = SandboxExecution.query.filter_by(sandbox_id=sandbox_id)
    status_filter = request.args.get("status")
    if status_filter:
        q = q.filter_by(status=SandboxExecutionStatus(status_filter))
    agent_id = request.args.get("agent_id", type=int)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    q = q.order_by(SandboxExecution.started_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=20)
    items = [e.to_dict(include_violations=False) for e in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/executions/<int:execution_id>", methods=["GET"])
@unified_auth_required
def get_sandbox_execution(execution_id):
    """Get a sandbox execution record with violations."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    return ApiResponse.success({"execution": execution.to_dict(include_violations=True)}).to_response()


@agents_bp.route("/sandboxes/<int:sandbox_id>/executions", methods=["POST"])
@unified_auth_required
def start_sandbox_execution(sandbox_id):
    """Start a new sandboxed execution for an agent.

    Freezes the policy snapshot, creates a RUNNING SandboxExecution, and returns
    the execution record + policy envelope for the executor to enforce.
    """
    user = get_current_user()
    sandbox = AgentSandbox.query.get(sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Sandbox not found", 404).to_response()
    body = validate_json_request()
    agent_id = body.get("agent_id")
    run_id = body.get("run_id")
    step_run_id = body.get("step_run_id")
    if not agent_id:
        return ApiResponse.error("agent_id is required").to_response()
    agent = Agent.query.get(agent_id)
    if not agent or agent.owner_id != user.id:
        return ApiResponse.error("Agent not found or not owned by you").to_response()
    execution = SandboxExecution(
        sandbox_id=sandbox_id,
        agent_id=agent_id,
        run_id=run_id,
        step_run_id=step_run_id,
        status=SandboxExecutionStatus.RUNNING,
        policy_snapshot=sandbox.to_policy_dict(),
        started_at=datetime.utcnow(),
        tool_calls=0,
        network_calls=0,
    )
    db.session.add(execution)
    db.session.commit()
    _queue_sse(user.id, "sandbox_execution_started", {
        "execution_id": execution.id, "sandbox_id": sandbox_id, "agent_id": agent_id,
    })
    flush_sse_notifications()
    return ApiResponse.success({
        "execution": execution.to_dict(),
        "policy": sandbox.to_policy_dict(),
    }, "Sandboxed execution started").to_response()


@agents_bp.route("/executions/<int:execution_id>/complete", methods=["POST"])
@unified_auth_required
def complete_sandbox_execution(execution_id):
    """Mark a sandboxed execution as completed."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    body = validate_json_request()
    execution.finish(
        SandboxExecutionStatus.COMPLETED,
        summary=body.get("output_summary"),
        error=body.get("error"),
    )
    # Update aggregated usage if provided
    if body.get("peak_memory_mb") is not None:
        execution.peak_memory_mb = body.get("peak_memory_mb")
    if body.get("cpu_seconds") is not None:
        execution.cpu_seconds = body.get("cpu_seconds")
    if body.get("output_tokens") is not None:
        execution.output_tokens = body.get("output_tokens")
    if body.get("tool_calls") is not None:
        execution.tool_calls = body.get("tool_calls")
    if body.get("network_calls") is not None:
        execution.network_calls = body.get("network_calls")
    db.session.commit()
    _queue_sse(user.id, "sandbox_execution_completed", {"execution_id": execution_id})
    flush_sse_notifications()
    return ApiResponse.success({"execution": execution.to_dict()}, "Execution completed").to_response()


@agents_bp.route("/executions/<int:execution_id>/revoke", methods=["POST"])
@unified_auth_required
def revoke_sandbox_execution(execution_id):
    """Manually revoke (terminate) a running sandboxed execution."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    if execution.status != SandboxExecutionStatus.RUNNING:
        return ApiResponse.error(f"Execution is not running (status={execution.status.value})").to_response()
    execution.finish(SandboxExecutionStatus.REVOKED, reason="Manually revoked by owner")
    AuditLog.record(
        action="sandbox.execution_revoke", resource_type="sandbox_execution", resource_id=execution.id,
        actor_type="human", actor_user_id=user.id,
        detail={"sandbox_id": execution.sandbox_id, "agent_id": execution.agent_id},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_execution_revoked", {"execution_id": execution_id})
    flush_sse_notifications()
    return ApiResponse.success({"execution": execution.to_dict()}, "Execution revoked").to_response()


@agents_bp.route("/executions/<int:execution_id>/violation", methods=["POST"])
@unified_auth_required
def report_sandbox_violation(execution_id):
    """Report a policy violation during a sandboxed execution.

    Body: { violation_type, attempted_action, detail, terminate?: bool }
    If terminate is true, the execution is marked VIOLATED.
    """
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    body = validate_json_request()
    vtype = body.get("violation_type")
    try:
        vtype_enum = SandboxViolationType(vtype)
    except ValueError:
        return ApiResponse.error(f"Invalid violation_type: {vtype}").to_response()
    v = execution.record_violation(
        vtype_enum,
        detail=body.get("detail", ""),
        attempted_action=body.get("attempted_action"),
    )
    if body.get("terminate"):
        execution.finish(
            SandboxExecutionStatus.VIOLATED,
            reason=f"Policy violation: {vtype_enum.value}",
        )
    AuditLog.record(
        action="sandbox.violation", resource_type="sandbox_violation", resource_id=v.id,
        actor_type="human", actor_user_id=user.id,
        detail={"execution_id": execution_id, "violation_type": vtype_enum.value,
                "agent_id": execution.agent_id, "terminated": bool(body.get("terminate"))},
    )
    db.session.commit()
    _queue_sse(user.id, "sandbox_violation", {
        "execution_id": execution_id, "violation_type": vtype_enum.value,
    })
    flush_sse_notifications()
    return ApiResponse.success({
        "violation": v.to_dict(),
        "execution": execution.to_dict(),
    }, "Violation recorded").to_response()


@agents_bp.route("/executions/<int:execution_id>/violations", methods=["GET"])
@unified_auth_required
def list_execution_violations(execution_id):
    """List violations recorded for a sandboxed execution."""
    user = get_current_user()
    execution = SandboxExecution.query.get(execution_id)
    if not execution:
        return ApiResponse.error("Execution not found", 404).to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.error("Execution not found", 404).to_response()
    q = SandboxViolation.query.filter_by(execution_id=execution_id).order_by(SandboxViolation.blocked_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=50)
    items = [v.to_dict() for v in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/sandboxes/dashboard", methods=["GET"])
@unified_auth_required
def sandbox_dashboard():
    """Aggregate sandbox stats for the current user."""
    user = get_current_user()
    sandboxes = AgentSandbox.query.filter_by(owner_id=user.id).all()
    sandbox_ids = [s.id for s in sandboxes]
    total_executions = 0
    running = 0
    violations_total = 0
    by_level = {"strict": 0, "moderate": 0, "permissive": 0}
    by_status = {}
    for s in sandboxes:
        by_level[s.security_level.value if s.security_level else "moderate"] += 1
    if sandbox_ids:
        total_executions = SandboxExecution.query.filter(SandboxExecution.sandbox_id.in_(sandbox_ids)).count()
        running = SandboxExecution.query.filter(
            SandboxExecution.sandbox_id.in_(sandbox_ids),
            SandboxExecution.status == SandboxExecutionStatus.RUNNING,
        ).count()
        violations_total = SandboxViolation.query.filter(SandboxViolation.sandbox_id.in_(sandbox_ids)).count()
        # Status breakdown
        for st in SandboxExecutionStatus:
            cnt = SandboxExecution.query.filter(
                SandboxExecution.sandbox_id.in_(sandbox_ids),
                SandboxExecution.status == st,
            ).count()
            by_status[st.value] = cnt
    return ApiResponse.success({
        "total_sandboxes": len(sandboxes),
        "total_executions": total_executions,
        "running_executions": running,
        "total_violations": violations_total,
        "by_level": by_level,
        "by_status": by_status,
    }).to_response()


@agents_bp.route("/sandboxes/violation-trend", methods=["GET"])
@unified_auth_required
def sandbox_violation_trend():
    """Daily sandbox violation counts + by-type breakdown for the current user.

    Buckets by calendar day (UTC) using ``blocked_at``. Also returns a
    by-violation-type aggregate over the window. Useful for spotting whether
    a policy tightening or Agent change is producing more violations.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    sandbox_ids = [s.id for s in AgentSandbox.query.filter_by(owner_id=user.id).with_entities(AgentSandbox.id).all()]
    if not sandbox_ids:
        return ApiResponse.success({"days": days, "trend": [], "by_type": {}}).to_response()

    from sqlalchemy import func as sa_func
    daily = (
        db.session.query(
            sa_func.date(SandboxViolation.blocked_at).label("date"),
            sa_func.count(SandboxViolation.id).label("count"),
        )
        .filter(SandboxViolation.sandbox_id.in_(sandbox_ids), SandboxViolation.blocked_at >= since)
        .group_by(sa_func.date(SandboxViolation.blocked_at))
        .order_by(sa_func.date(SandboxViolation.blocked_at))
        .all()
    )
    trend = [{"date": str(d), "count": c} for d, c in daily]

    by_type = {}
    for vt in SandboxViolationType:
        by_type[vt.value] = SandboxViolation.query.filter(
            SandboxViolation.sandbox_id.in_(sandbox_ids),
            SandboxViolation.violation_type == vt,
            SandboxViolation.blocked_at >= since,
        ).count()

    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "by_type": by_type,
    }).to_response()


@agents_bp.route("/sandboxes/violations-by-agent", methods=["GET"])
@unified_auth_required
def sandbox_violations_by_agent():
    """Per-Agent sandbox violation counts for the current user.

    Aggregates SandboxViolation by ``agent_id`` over the lookback window,
    with a by-violation-type sub-count. Returns the top N by total, enriched
    with the Agent's name/kind. Reveals which Agents most frequently attempt
    disallowed actions.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    sandbox_ids = [s.id for s in AgentSandbox.query.filter_by(owner_id=user.id).with_entities(AgentSandbox.id).all()]
    if not sandbox_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    rows = SandboxViolation.query.filter(
        SandboxViolation.sandbox_id.in_(sandbox_ids),
        SandboxViolation.blocked_at >= since,
    ).with_entities(SandboxViolation.agent_id, SandboxViolation.violation_type).all()

    agg: dict = {}
    for aid, vt in rows:
        if aid is None:
            continue
        entry = agg.setdefault(aid, {"agent_id": aid, "total": 0, "by_type": {}})
        entry["total"] += 1
        key = vt.value if hasattr(vt, "value") else str(vt)
        entry["by_type"][key] = entry["by_type"].get(key, 0) + 1

    top = sorted(agg.values(), key=lambda x: x["total"], reverse=True)[:limit]
    agent_ids = [e["agent_id"] for e in top]
    agents = {a.id: a for a in Agent.query.filter(Agent.id.in_(agent_ids)).all()} if agent_ids else {}
    for e in top:
        a = agents.get(e["agent_id"])
        e["name"] = a.name if a else None
        e["kind"] = a.kind.value if a and a.kind else None
    return ApiResponse.success({"days": days, "items": top}).to_response()


@agents_bp.route("/sandboxes/template-usage", methods=["GET"])
@unified_auth_required
def sandbox_template_usage():
    """Sandbox policy template instantiation stats for the current user.

    Aggregates ``sandbox.template_instantiate`` audit events by template_key:
    how many times each preset template was instantiated, and how many of
    those instances were bound to an Agent (vs. left as a reusable policy).
    Reveals which templates are most popular in practice.
    """
    user = get_current_user()
    rows = AuditLog.query.filter(
        AuditLog.action == "sandbox.template_instantiate",
        AuditLog.actor_user_id == user.id,
    ).all()
    agg: dict = {}
    for r in rows:
        d = r.detail or {}
        key = d.get("template_key")
        if not key:
            continue
        entry = agg.setdefault(key, {"template_key": key, "uses": 0, "bound_to_agent": 0})
        entry["uses"] += 1
        if d.get("agent_id") is not None:
            entry["bound_to_agent"] += 1
    items = sorted(agg.values(), key=lambda x: x["uses"], reverse=True)
    return ApiResponse.success({"items": items}).to_response()




@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/sandbox-execution", methods=["GET"])
@unified_auth_required
def get_step_sandbox_execution(run_id, step_key):
    """Get the sandbox execution (if any) bound to a workflow step run.

    This surfaces the auto-started sandboxed execution created when the step's
    agent had an active sandbox policy.
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    execution = SandboxExecution.query.filter_by(step_run_id=sr.id).order_by(
        SandboxExecution.created_at.desc()
    ).first()
    if not execution:
        return ApiResponse.success({"execution": None}, "No sandboxed execution for this step").to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.not_found("Sandboxed execution not found").to_response()
    return ApiResponse.success({
        "execution": execution.to_dict(include_violations=True),
        "sandbox": sandbox.to_dict(),
        "policy": execution.policy_snapshot or sandbox.to_policy_dict(),
    }).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/sandbox-violation", methods=["POST"])
@unified_auth_required
def report_step_sandbox_violation(run_id, step_key):
    """Report a sandbox policy violation for a workflow step's execution.

    If terminate_step is true, the step is marked FAILED and the sandboxed
    execution is marked VIOLATED. Otherwise only the violation is recorded
    and the step continues.
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    execution = SandboxExecution.query.filter_by(step_run_id=sr.id).order_by(
        SandboxExecution.created_at.desc()
    ).first()
    if not execution:
        return ApiResponse.error("No sandboxed execution for this step").to_response()
    sandbox = AgentSandbox.query.get(execution.sandbox_id)
    if not sandbox or sandbox.owner_id != user.id:
        return ApiResponse.not_found("Sandboxed execution not found").to_response()
    body = validate_json_request()
    vtype = body.get("violation_type")
    try:
        vtype_enum = SandboxViolationType(vtype)
    except ValueError:
        return ApiResponse.error(f"Invalid violation_type: {vtype}").to_response()
    v = execution.record_violation(
        vtype_enum,
        detail=body.get("detail", ""),
        attempted_action=body.get("attempted_action"),
    )
    terminate = body.get("terminate_step", False)
    terminated = False
    if terminate and execution.status == SandboxExecutionStatus.RUNNING:
        execution.finish(
            SandboxExecutionStatus.VIOLATED,
            reason=f"Policy violation: {vtype_enum.value}",
        )
        # Mark the step as failed and cancel the bound run
        now = datetime.utcnow()
        sr.status = StepStatus.FAILED
        sr.error = f"Sandbox violation: {vtype_enum.value} — {body.get('detail', '')}"
        sr.finished_at = now
        if sr.assignment_id:
            old_assignment = TaskAssignment.query.get(sr.assignment_id)
            if old_assignment and old_assignment.state in LEASED_EXECUTION_STATES:
                old_assignment.state = TaskAssignmentState.CANCELLED
                old_assignment.completed_at = now
        bound_runs = AgentRun.query.filter_by(
            assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
        ).all()
        for r in bound_runs:
            r.status = AgentRunStatus.FAILED
            r.ended_at = now
            r.error = sr.error
        terminated = True
    AuditLog.record(
        action="sandbox.step_violation", resource_type="sandbox_violation", resource_id=v.id,
        actor_type="human", actor_user_id=user.id, project_id=wf_run.project_id,
        detail={"run_id": run_id, "step_key": step_key, "violation_type": vtype_enum.value,
                "agent_id": execution.agent_id, "terminated": terminated},
    )
    db.session.commit()
    if terminated:
        # Re-advance the workflow so downstream steps / failure handling proceed
        _advance_workflow(wf_run)
        db.session.commit()
    _queue_sse(user.id, "sandbox_step_violation", {
        "run_id": run_id, "step_key": step_key,
        "violation_type": vtype_enum.value, "terminated": terminated,
    })
    flush_sse_notifications()
    return ApiResponse.success({
        "violation": v.to_dict(),
        "execution": execution.to_dict(),
        "step_terminated": terminated,
    }, "Violation recorded" + (" and step terminated" if terminated else "")).to_response()


# ---------------------------------------------------------------------------
# Increment 88: Workflow step dynamic reconfiguration (runtime overrides)
# ---------------------------------------------------------------------------

