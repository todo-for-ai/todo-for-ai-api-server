"""
Agent collaboration API — workflow run management routes.

Handles workflow launch, run lifecycle (cancel/pause/resume/retry),
and step completion. Analytics routes are in workflow_analytics.py;
DAG advancement helpers are in _workflow_helpers.py.
"""

from datetime import datetime

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_request_args,
    paginate_query,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
    AgentRun,
    AuditLog,
    SandboxExecution,
    StepStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    AgentConflict,
    Project,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
)


def _workflow_owned_by_user(workflow_id, user):
    """Return the workflow if it belongs to *user*, else None."""
    return Workflow.query.filter_by(id=workflow_id, owner_id=user.id).first()
from ._workflow_helpers import (
    _RUNTIME_OVERRIDABLE_KEYS,
    _advance_workflow,
)
from .workflow_completion import complete_step_run


@agents_bp.route("/workflows/<int:workflow_id>/runs", methods=["POST"])
@unified_auth_required
def launch_workflow(workflow_id):
    """Launch a new run of a workflow.

    Creates a WorkflowRun, resolves the DAG, and starts all steps whose
    depends_on list is empty (i.e. entry-point steps).
    """
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()
    if not workflow.is_active:
        return ApiResponse.error("Workflow is not active", 400).to_response()

    data = validate_json_request()
    project_id = data.get("project_id")
    if not project_id:
        return ApiResponse.error("project_id is required", 400).to_response()
    project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
    if not project:
        return ApiResponse.error("Project not found", 404).to_response()

    root_task_id = data.get("root_task_id")

    # Create the run
    wf_run = WorkflowRun.create(
        workflow_id=workflow.id,
        root_task_id=root_task_id,
        project_id=project_id,
        owner_id=user.id,
        status=WorkflowStatus.PENDING,
        context=data.get("context", {}),
    )
    db.session.flush()  # 立即取 wf_run.id——下方 step run 创建依赖它

    # Create step runs for every step in the definition
    steps = WorkflowStep.query.filter_by(workflow_id=workflow.id).order_by(WorkflowStep.order).all()
    for step in steps:
        sr = WorkflowStepRun.create(
            run_id=wf_run.id,
            step_key=step.step_key,
            status=StepStatus.PENDING,
            attempt=1,
        )

    db.session.commit()

    # Now kick off entry-point steps (those with no dependencies)
    _advance_workflow(wf_run)

    db.session.commit()
    flush_sse_notifications()

    AuditLog.record(
        action="workflow.launched", resource_type="workflow_run", resource_id=wf_run.id,
        actor_type="human", actor_user_id=user.id,
        project_id=wf_run.project_id,
        detail={"workflow_id": workflow.id, "workflow_name": workflow.name},
        ip_address=_client_ip(),
    )
    db.session.commit()

    return ApiResponse.created(
        wf_run.to_dict(include_step_runs=True), "Workflow launched"
    ).to_response()


@agents_bp.route("/workflow-runs", methods=["GET"])
@unified_auth_required
def list_workflow_runs():
    """List workflow runs for the current user."""
    user = get_current_user()
    args = get_request_args()
    query = WorkflowRun.query.filter_by(owner_id=user.id)
    # get_request_args 返回普通 dict，带 type= 的过滤须走 request.args
    workflow_id = request.args.get("workflow_id", type=int)
    if workflow_id:
        query = query.filter_by(workflow_id=workflow_id)
    if args.get("status"):
        try:
            query = query.filter_by(status=WorkflowStatus(args.get("status")))
        except ValueError:
            pass
    query = query.order_by(WorkflowRun.created_at.desc())
    result = paginate_query(
        query, args["page"], args["per_page"],
        serializer=lambda r: r.to_dict(include_step_runs=True),
    )
    return ApiResponse.success(result).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>", methods=["GET"])
@unified_auth_required
def get_workflow_run(run_id):
    """Get a single workflow run with step details."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True)).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/console", methods=["GET"])
@unified_auth_required
def get_workflow_run_console(run_id):
    """Step-level real-time console: aggregates step runs with their sandbox
    executions, effective params, recent run logs, and any conflicts tied to
    the run — a single payload for monitoring/intervening on a running workflow.

    Query params:
      log_limit (default 5): max recent RunLog entries per step
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    try:
        log_limit = max(1, min(50, int(request.args.get("log_limit", 5))))
    except (TypeError, ValueError):
        log_limit = 5

    now = datetime.utcnow()
    steps_payload = []
    for sr in wf_run.step_runs:
        # Effective params (overrides merged with definition)
        effective = {}
        for k in _RUNTIME_OVERRIDABLE_KEYS:
            effective[k] = sr.get_effective_param(k)

        # Sandbox execution bound to this step (most recent)
        from ._shared import AgentSandbox, RunLog
        sandbox_exec = SandboxExecution.query.filter_by(step_run_id=sr.id).order_by(
            SandboxExecution.created_at.desc()
        ).first()
        sandbox_exec_dict = None
        sandbox_policy = None
        if sandbox_exec:
            sandbox_exec_dict = sandbox_exec.to_dict(include_violations=True)
            sb = AgentSandbox.query.get(sandbox_exec.sandbox_id)
            sandbox_policy = sb.to_dict() if sb else None

        # Recent run logs for the AgentRun bound to this step
        logs = []
        if sr.assignment_id:
            bound_run = AgentRun.query.filter_by(assignment_id=sr.assignment_id).order_by(
                AgentRun.started_at.desc()
            ).first()
            if bound_run:
                logs = [l.to_dict() for l in RunLog.query.filter_by(run_id=bound_run.id).order_by(
                    RunLog.created_at.desc()
                ).limit(log_limit).all()]
                logs.reverse()  # chronological order for display

        # Timing
        duration_seconds = None
        if sr.started_at:
            end = sr.finished_at or now
            duration_seconds = (end - sr.started_at).total_seconds()

        steps_payload.append({
            "step_run": sr.to_dict(),
            "effective_params": effective,
            "sandbox_execution": sandbox_exec_dict,
            "sandbox_policy": sandbox_policy,
            "recent_logs": logs,
            "duration_seconds": duration_seconds,
        })

    # Conflicts tied to this run
    run_conflicts = AgentConflict.query.filter_by(
        owner_id=user.id, workflow_run_id=run_id
    ).order_by(AgentConflict.created_at.desc()).all()

    # Overall progress summary
    status_counts = {}
    for sr in wf_run.step_runs:
        s = sr.status.value if sr.status else "unknown"
        status_counts[s] = status_counts.get(s, 0) + 1
    total_steps = len(wf_run.step_runs)
    done = status_counts.get("succeeded", 0) + status_counts.get("skipped", 0) + status_counts.get("cancelled", 0)
    progress_pct = round((done / total_steps) * 100, 1) if total_steps else 0.0

    return ApiResponse.success({
        "workflow_run": wf_run.to_dict(include_step_runs=False),
        "steps": steps_payload,
        "conflicts": [c.to_dict() for c in run_conflicts],
        "summary": {
            "total_steps": total_steps,
            "status_counts": status_counts,
            "progress_percent": progress_pct,
            "running_count": status_counts.get("running", 0),
            "failed_count": status_counts.get("failed", 0),
            "pending_count": status_counts.get("pending", 0) + status_counts.get("waiting", 0),
        },
    }).to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/cancel", methods=["POST"])
@unified_auth_required
def cancel_workflow_run(run_id):
    """Cancel a running workflow."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
        return ApiResponse.error("Workflow is not cancellable", 400).to_response()

    now = datetime.utcnow()
    wf_run.status = WorkflowStatus.CANCELLED
    wf_run.finished_at = now
    # Cancel all pending/waiting/running step runs
    for sr in wf_run.step_runs:
        if sr.status in (StepStatus.PENDING, StepStatus.WAITING, StepStatus.RUNNING):
            sr.status = StepStatus.CANCELLED
            sr.finished_at = now

    db.session.commit()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow cancelled").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/pause", methods=["POST"])
@unified_auth_required
def pause_workflow_run(run_id):
    """Pause a running workflow. Running steps will continue but no new steps will be started."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status != WorkflowStatus.RUNNING:
        return ApiResponse.error("Only running workflows can be paused", 400).to_response()

    now = datetime.utcnow()
    wf_run.status = WorkflowStatus.PAUSED
    # Mark any WAITING steps as PAUSED too so they don't get picked up on resume
    for sr in wf_run.step_runs:
        if sr.status == StepStatus.WAITING:
            sr.status = StepStatus.PENDING
    db.session.commit()
    AuditLog.record("workflow_pause", target_type="workflow_run", target_id=run_id, user_id=user.id,
                     details={"workflow_name": wf_run.workflow.name if wf_run.workflow else None})
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow paused").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/resume", methods=["POST"])
@unified_auth_required
def resume_workflow_run(run_id):
    """Resume a paused workflow. The DAG engine will re-evaluate which steps can start."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status != WorkflowStatus.PAUSED:
        return ApiResponse.error("Only paused workflows can be resumed", 400).to_response()

    wf_run.status = WorkflowStatus.RUNNING
    db.session.commit()
    AuditLog.record("workflow_resume", target_type="workflow_run", target_id=run_id, user_id=user.id,
                     details={"workflow_name": wf_run.workflow.name if wf_run.workflow else None})
    # Re-evaluate which steps can start now
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow resumed").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/retry", methods=["POST"])
@unified_auth_required
def retry_workflow_run(run_id):
    """Retry a failed workflow by resetting failed steps and re-advancing the DAG."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    if wf_run.status != WorkflowStatus.FAILED:
        return ApiResponse.error("Only failed workflows can be retried", 400).to_response()

    now = datetime.utcnow()
    # Reset failed steps back to PENDING so the DAG engine can re-evaluate
    retried_steps = []
    for sr in wf_run.step_runs:
        if sr.status == StepStatus.FAILED:
            sr.status = StepStatus.PENDING
            sr.error = None
            sr.finished_at = None
            sr.attempt = (sr.attempt or 1) + 1
            retried_steps.append(sr.step_key)
        elif sr.status == StepStatus.SKIPPED:
            # Also retry skipped steps — they may have been skipped due to a prior failure
            sr.status = StepStatus.PENDING
            sr.finished_at = None
            sr.attempt = (sr.attempt or 1) + 1
            retried_steps.append(sr.step_key)

    wf_run.status = WorkflowStatus.RUNNING
    wf_run.error = None
    wf_run.finished_at = None
    db.session.commit()

    AuditLog.record("workflow_retry", target_type="workflow_run", target_id=run_id, user_id=user.id,
                     details={"retried_steps": retried_steps})
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()
    return ApiResponse.success(wf_run.to_dict(include_step_runs=True), "Workflow retry started").to_response()


# --- Workflow step callback (called by Agent system when a step completes) ---


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/complete", methods=["POST"])
@unified_auth_required
def complete_workflow_step(run_id, step_key):
    """Mark a workflow step as completed (or failed) and advance the DAG.

    This is the human/MCP callback. Step tasks completed through the normal
    task lifecycle (agent commit / task update / review approval) are closed
    automatically via workflow_completion.maybe_autocomplete_for_task.
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()

    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()

    data = validate_json_request()
    success = data.get("success", True)

    try:
        complete_step_run(
            wf_run, step_key,
            success=success,
            result_summary=data.get("result_summary", "") or "",
            error=data.get("error", "") or "",
            source="human",
        )
    except ValueError as exc:
        return ApiResponse.error(str(exc), 400).to_response()

    return ApiResponse.success(
        wf_run.to_dict(include_step_runs=True),
        f"Step {step_key} {'succeeded' if success else 'failed'}",
    ).to_response()
