"""
Maintenance, orchestration, health-check, and auto-recovery endpoints.
"""

import json as _json
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
    AgentStatus,
    AgentRun,
    AgentRunStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowStatus,
    WorkflowTrigger,
    OrchestrationRun,
    AuditLog,
    Project,
    get_request_args,
    paginate_query,
    parse_enum,
    record_task_event,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    expire_stale_assignments,
    mark_stale_agents_offline,
    ACTIVE_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
)

@agents_bp.route("/maintenance/escalate-overdue", methods=["POST"])
@unified_auth_required
def escalate_overdue():
    """Manually trigger priority escalation for overdue tasks.

    Can be called by a cron job or manually. Only escalates tasks owned by the
    current user unless the user is an admin.
    """
    try:
        current_user = get_current_user()
        data = request.get_json(silent=True) or {}
        overdue_after_days = data.get("overdue_after_days", 1)
        escalated = _escalate_overdue_tasks(
            owner_id=current_user.id,
            overdue_after_days=overdue_after_days,
        )
        return ApiResponse.success(
            {"escalated_count": len(escalated), "task_ids": escalated},
            f"Escalated {len(escalated)} overdue task(s)",
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to escalate: {str(e)}", 500).to_response()


# =========================================================================
# Audit Log API
# =========================================================================


@agents_bp.route("/audit-logs", methods=["GET"])
@unified_auth_required
def list_audit_logs():
    """Query the immutable audit trail for platform operations."""
    user = get_current_user()
    args = get_request_args()

    query = AuditLog.query.filter(
        or_(
            AuditLog.actor_user_id == user.id,
            AuditLog.project_id.in_([p.id for p in Project.query.filter_by(owner_id=user.id).all()]),
        )
    )

    # Filters
    if args.get("action"):
        query = query.filter(AuditLog.action == args.get("action"))
    if args.get("resource_type"):
        query = query.filter(AuditLog.resource_type == args.get("resource_type"))
    if args.get("resource_id", type=int):
        query = query.filter(AuditLog.resource_id == args.get("resource_id", type=int))
    if args.get("actor_type"):
        query = query.filter(AuditLog.actor_type == args.get("actor_type"))
    if args.get("actor_agent_id", type=int):
        query = query.filter(AuditLog.actor_agent_id == args.get("actor_agent_id", type=int))
    if args.get("project_id", type=int):
        query = query.filter(AuditLog.project_id == args.get("project_id", type=int))

    query = query.order_by(AuditLog.created_at.desc())
    result = paginate_query(query, args)
    items = [log.to_dict() for log in result["items"]]
    return ApiResponse.paginated(items, result["pagination"]).to_response()




@agents_bp.route("/maintenance/health-check", methods=["POST"])
@unified_auth_required
def health_check():
    """Run a full health check: expire stale agents, expire stale leases,
    and escalate overdue tasks. Returns a summary of what was done.

    Designed to be called by a cron job every few minutes.
    """
    try:
        current_user = get_current_user()
        now = datetime.utcnow()

        # 1. Mark stale agents offline (expires their assignments & runs)
        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        stale_agent_ids = [a.id for a in stale_agents]

        # 2. Expire stale leases
        expired_count = 0
        expired_assignments = TaskAssignment.query.filter(
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            TaskAssignment.lease_expires_at.isnot(None),
            TaskAssignment.lease_expires_at < now,
        ).join(Task).join(Project).filter(Project.owner_id == current_user.id).all()

        for assignment in expired_assignments:
            assignment.state = TaskAssignmentState.EXPIRED
            assignment.completed_at = now
            for run in AgentRun.query.filter_by(
                assignment_id=assignment.id, status=AgentRunStatus.RUNNING
            ).all():
                run.status = AgentRunStatus.EXPIRED
                run.ended_at = now
            expired_count += 1

        # 3. Escalate overdue tasks
        escalated_ids = _escalate_overdue_tasks(owner_id=current_user.id)

        db.session.commit()

        # 4. Audit log
        if stale_agents or expired_assignments or escalated_ids:
            AuditLog.record(
                action="maintenance.health_check",
                resource_type="system",
                resource_id=0,
                actor_type="human",
                actor_user_id=current_user.id,
                detail={
                    "stale_agents": stale_agent_ids,
                    "expired_leases": expired_count,
                    "escalated_tasks": escalated_ids,
                },
                ip_address=_client_ip(),
            )
            db.session.commit()

        return ApiResponse.success(
            {
                "stale_agents": len(stale_agent_ids),
                "stale_agent_ids": stale_agent_ids,
                "expired_leases": expired_count,
                "escalated_tasks": len(escalated_ids),
                "escalated_task_ids": escalated_ids,
            },
            f"Health check complete: {len(stale_agent_ids)} stale agent(s), {expired_count} expired lease(s), {len(escalated_ids)} escalated task(s)",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Health check failed: {str(e)}", 500).to_response()


@agents_bp.route("/maintenance/mark-offline-agents", methods=["POST"])
@unified_auth_required
def mark_offline_agents():
    """Scan all active/paused agents and mark those whose last_seen_at exceeds
    AGENT_OFFLINE_AFTER_SECONDS as OFFLINE. Also cancels their running assignments.

    Can be called by a cron scheduler or manually.
    """
    user = get_current_user()
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=AGENT_OFFLINE_AFTER_SECONDS)

    stale = Agent.query.filter(
        Agent.owner_id == user.id,
        Agent.status.in_((AgentStatus.ACTIVE, AgentStatus.PAUSED)),
        db.or_(
            Agent.last_seen_at.is_(None),
            Agent.last_seen_at < cutoff,
        ),
    ).all()

    stale_ids = []
    for agent in stale:
        agent.status = AgentStatus.OFFLINE
        # Cancel running assignments for this agent
        for assignment in TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent.id,
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
        ).all():
            assignment.state = TaskAssignmentState.CANCELLED
            assignment.completed_at = now
        stale_ids.append(agent.id)

    if stale_ids:
        AuditLog.record(
            action="maintenance.mark_offline_agents",
            resource_type="system",
            resource_id=0,
            actor_type="human",
            actor_user_id=user.id,
            detail={"offline_agent_ids": stale_ids, "threshold_seconds": AGENT_OFFLINE_AFTER_SECONDS},
            ip_address=_client_ip(),
        )
    db.session.commit()

    return ApiResponse.success({
        "marked_offline": len(stale_ids),
        "agent_ids": stale_ids,
    }, f"{len(stale_ids)} agent(s) marked offline").to_response()


@agents_bp.route("/maintenance/timeout-workflow-steps", methods=["POST"])
@unified_auth_required
def timeout_workflow_steps():
    """Scan all running workflow steps and mark those that have exceeded their
    timeout_seconds as FAILED, then advance the affected workflows.

    Designed to be called by a cron scheduler periodically.
    """
    user = get_current_user()
    now = datetime.utcnow()
    timed_out = []

    # Find all running step runs owned by this user
    running_steps = WorkflowStepRun.query.filter(
        WorkflowStepRun.status == StepStatus.RUNNING,
    ).join(WorkflowRun).filter(
        WorkflowRun.owner_id == user.id,
        WorkflowRun.status == WorkflowStatus.RUNNING,
    ).all()

    for sr in running_steps:
        # Get the step definition for timeout_seconds
        wf_run = sr.run
        if not wf_run or not wf_run.workflow:
            continue
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=sr.step_key
        ).first()
        # Apply runtime overrides so a dynamically-adjusted timeout takes effect
        step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
        if not step_def or not step_def.timeout_seconds or step_def.timeout_seconds <= 0:
            continue  # no timeout configured

        if sr.started_at:
            elapsed = (now - sr.started_at).total_seconds()
            if elapsed > step_def.timeout_seconds:
                sr.status = StepStatus.FAILED
                sr.error = f"Step timed out after {int(elapsed)}s (limit: {step_def.timeout_seconds}s)"
                sr.finished_at = now
                # Finalize any sandboxed execution as TIMEOUT + record violation
                try:
                    if sr.assignment_id:
                        bound_run = AgentRun.query.filter_by(
                            assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                        ).first()
                        if bound_run:
                            execution = _maybe_finish_sandboxed_execution(
                                bound_run, SandboxExecutionStatus.TIMEOUT,
                                error=sr.error, reason="Step timeout",
                            )
                            if execution:
                                execution.record_violation(
                                    SandboxViolationType.TIMEOUT,
                                    detail=sr.error,
                                    attempted_action=f"step {sr.step_key} exceeded timeout",
                                )
                except Exception:
                    pass
                timed_out.append({
                    "step_key": sr.step_key,
                    "run_id": wf_run.id,
                    "elapsed_seconds": int(elapsed),
                    "timeout_seconds": step_def.timeout_seconds,
                })

    if timed_out:
        AuditLog.record(
            action="maintenance.timeout_workflow_steps",
            resource_type="system",
            resource_id=0,
            actor_type="human",
            actor_user_id=user.id,
            detail={"timed_out_steps": timed_out},
            ip_address=_client_ip(),
        )
    db.session.commit()

    # Re-advance affected workflows
    affected_run_ids = set(t["run_id"] for t in timed_out)
    for run_id in affected_run_ids:
        wf_run = WorkflowRun.query.get(run_id)
        if wf_run:
            _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()

    return ApiResponse.success({
        "timed_out": len(timed_out),
        "steps": timed_out,
    }, f"{len(timed_out)} step(s) timed out").to_response()

@agents_bp.route("/maintenance/fire-triggers", methods=["POST"])
@unified_auth_required
def fire_due_triggers():
    """Check all active triggers and fire those that are due.

    This endpoint is designed to be called periodically (e.g. via cron or
    external scheduler) to drive the workflow trigger system.
    """
    now = datetime.utcnow()
    due_triggers = WorkflowTrigger.query.filter(
        WorkflowTrigger.is_active == True,
        WorkflowTrigger.next_fire_at != None,
        WorkflowTrigger.next_fire_at <= now,
    ).all()

    fired = []
    for trigger in due_triggers:
        workflow = Workflow.query.get(trigger.workflow_id)
        if not workflow or not workflow.is_active:
            trigger.is_active = False
            continue

        # Create a WorkflowRun
        wf_run = WorkflowRun.create(
            workflow_id=workflow.id,
            owner_id=trigger.owner_id,
            project_id=trigger.project_id,
            root_task_id=trigger.root_task_id,
            status=WorkflowStatus.PENDING,
            context=trigger.context_override or {},
        )
        db.session.flush()

        # Create step runs from definition
        definition = workflow.definition or {}
        for step_def in definition.get("steps", []):
            WorkflowStepRun.create(
                run_id=wf_run.id,
                step_key=step_def.get("step_key", ""),
                status=StepStatus.PENDING,
            )

        # Advance the workflow
        wf_run.status = WorkflowStatus.RUNNING
        _advance_workflow(wf_run)

        trigger.fire_count = (trigger.fire_count or 0) + 1
        trigger.last_fired_at = now

        # Compute next fire time
        if trigger.cron_expr:
            trigger.next_fire_at = _compute_next_fire(trigger.cron_expr, now)
        elif trigger.one_shot_at:
            # One-shot: deactivate after firing
            trigger.is_active = False
            trigger.next_fire_at = None

        fired.append({
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "workflow_run_id": wf_run.id,
        })

        AuditLog.record(
            action="workflow_trigger.fired", resource_type="workflow_trigger", resource_id=trigger.id,
            actor_type="system",
            detail={"workflow_run_id": wf_run.id, "fire_count": trigger.fire_count},
        )

    db.session.commit()
    flush_sse_notifications()

    return ApiResponse.success(
        {"fired_count": len(fired), "fired": fired},
        f"Fired {len(fired)} trigger(s)",
    ).to_response()


# ---------------------------------------------------------------------------
# Agent Direct Messaging (peer-to-peer)
# ---------------------------------------------------------------------------

@agents_bp.route("/maintenance/orchestrate", methods=["POST"])
@unified_auth_required
def orchestrate():
    """Global collaboration orchestrator: runs the full collaboration
    maintenance cycle in a single call. Designed to be invoked by an external
    scheduler (cron) every few minutes to drive the multi-Agent platform
    without manual per-endpoint triggering.

    Stages (executed in order, each isolated so a failure in one stage does
    not abort the others):
      1. Health: mark stale agents offline, expire stale leases, escalate
         overdue tasks.
      2. Workflow timeout: mark timed-out steps FAILED and re-advance
         affected workflows.
      3. Trigger firing: launch workflow runs for any due triggers.
      4. Conflict resolution: detect new conflicts and auto-resolve
         low-severity ones with safe strategies.

    Returns a per-stage summary plus an overall duration.
    """
    user = get_current_user()
    report, duration, message = _run_orchestration(user, actor_type="human")
    try:
        from core.orchestrator_scheduler import record_last_run
        record_last_run(report, duration, message)
    except Exception:
        pass  # best-effort: status endpoint is non-critical
    return ApiResponse.success(
        {**report, "duration_seconds": round(duration, 3)},
        message,
    ).to_response()


@agents_bp.route("/maintenance/orchestrator/status", methods=["GET"])
@unified_auth_required
def orchestrator_status():
    """Return the built-in scheduler state and the last orchestration cycle
    summary (if the scheduler is enabled)."""
    try:
        from core.orchestrator_scheduler import scheduler_status
        status = scheduler_status()
    except Exception as e:
        return ApiResponse.error(f"Scheduler status unavailable: {str(e)}").to_response()
    return ApiResponse.success(status, "Orchestrator status").to_response()


@agents_bp.route("/maintenance/orchestrator/history", methods=["GET"])
@unified_auth_required
def orchestrator_history():
    """Return recent orchestration run records for trend analysis.

    Query params: limit (default 20, max 100), triggered_by (manual|scheduler).
    """
    user = get_current_user()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    q = OrchestrationRun.query.filter_by(owner_id=user.id)
    tb = request.args.get("triggered_by")
    if tb in ("manual", "scheduler"):
        q = q.filter(OrchestrationRun.triggered_by == tb)
    runs = q.order_by(OrchestrationRun.created_at.desc()).limit(limit).all()
    # Trend aggregates
    items = [r.to_dict() for r in runs]
    return ApiResponse.success({
        "items": items,
        "count": len(items),
        "trend": {
            "total_runs": len(items),
            "manual_runs": sum(1 for i in items if i.get("triggered_by") == "manual"),
            "scheduler_runs": sum(1 for i in items if i.get("triggered_by") == "scheduler"),
            "avg_duration": round(sum(i.get("duration_seconds", 0) for i in items) / len(items), 3) if items else 0,
            "total_errors": sum(i.get("error_count", 0) for i in items),
            "total_conflicts_resolved": sum(i.get("conflicts_auto_resolved", 0) for i in items),
            "total_triggers_fired": sum(i.get("triggers_fired", 0) for i in items),
        },
    }, "Orchestrator history").to_response()


@agents_bp.route("/maintenance/orchestrator/daily-trend", methods=["GET"])
@unified_auth_required
def orchestrator_daily_trend():
    """Daily aggregation of orchestration runs for trend visualization.

    Buckets OrchestrationRun records by the date portion of created_at,
    aligned with the security events daily-trend time dimension so the two
    can be rendered on a unified timeline. Optional filters: triggered_by
    (manual|scheduler), since, until (ISO date/datetime, inclusive).
    Returns:
      {
        days: [{date, runs, manual_runs, scheduler_runs, triggers_fired,
                conflicts_resolved, errors, avg_duration}],
        totals: {runs, manual_runs, scheduler_runs, triggers_fired,
                 conflicts_resolved, errors}
      }
    """
    user = get_current_user()
    q = OrchestrationRun.query.filter_by(owner_id=user.id)
    tb = request.args.get("triggered_by")
    if tb in ("manual", "scheduler"):
        q = q.filter(OrchestrationRun.triggered_by == tb)
    since = request.args.get("since")
    if since:
        q = q.filter(OrchestrationRun.created_at >= since)
    until = request.args.get("until")
    if until:
        q = q.filter(OrchestrationRun.created_at <= until)
    runs = q.order_by(OrchestrationRun.created_at.desc()).limit(1000).all()

    buckets = {}  # date -> accumulators
    for r in runs:
        ts = (r.created_at.isoformat() if r.created_at else "")
        day = ts[:10] if len(ts) >= 10 else None
        if not day:
            continue
        b = buckets.setdefault(day, {
            "runs": 0, "manual_runs": 0, "scheduler_runs": 0,
            "triggers_fired": 0, "conflicts_resolved": 0, "errors": 0,
            "duration_sum": 0.0,
        })
        b["runs"] += 1
        if r.triggered_by == "manual":
            b["manual_runs"] += 1
        elif r.triggered_by == "scheduler":
            b["scheduler_runs"] += 1
        b["triggers_fired"] += r.triggers_fired or 0
        b["conflicts_resolved"] += r.conflicts_auto_resolved or 0
        b["errors"] += r.error_count or 0
        b["duration_sum"] += r.duration_seconds or 0.0

    sorted_days = sorted(buckets.items(), key=lambda kv: kv[0])
    days = []
    for d, b in sorted_days:
        days.append({
            "date": d,
            "runs": b["runs"],
            "manual_runs": b["manual_runs"],
            "scheduler_runs": b["scheduler_runs"],
            "triggers_fired": b["triggers_fired"],
            "conflicts_resolved": b["conflicts_resolved"],
            "errors": b["errors"],
            "avg_duration": round(b["duration_sum"] / b["runs"], 3) if b["runs"] else 0,
        })
    totals = {
        "runs": sum(d["runs"] for d in days),
        "manual_runs": sum(d["manual_runs"] for d in days),
        "scheduler_runs": sum(d["scheduler_runs"] for d in days),
        "triggers_fired": sum(d["triggers_fired"] for d in days),
        "conflicts_resolved": sum(d["conflicts_resolved"] for d in days),
        "errors": sum(d["errors"] for d in days),
    }
    return ApiResponse.success(
        data={"days": days, "totals": totals},
        message="Orchestrator daily trend",
    ).to_response()


def _run_orchestration(user, actor_type="human"):
    """Core orchestration logic, reusable by both the HTTP endpoint and the
    built-in background scheduler. Returns (report_dict, duration_seconds, message).
    Does NOT call get_current_user(); the caller supplies the user scope.
    """
    start = datetime.utcnow()
    report = {
        "stale_agents": 0,
        "stale_agent_ids": [],
        "expired_leases": 0,
        "escalated_tasks": 0,
        "escalated_task_ids": [],
        "timed_out_steps": 0,
        "triggers_fired": 0,
        "trigger_run_ids": [],
        "conflicts_detected": 0,
        "conflicts_auto_resolved": 0,
        "conflicts_skipped": 0,
        "errors": [],
    }

    # --- Stage 1: health (stale agents, expired leases, overdue escalation) ---
    try:
        now = datetime.utcnow()
        stale_agents = mark_stale_agents_offline(owner_id=user.id)
        report["stale_agents"] = len(stale_agents)
        report["stale_agent_ids"] = [a.id for a in stale_agents]

        expired_assignments = TaskAssignment.query.filter(
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            TaskAssignment.lease_expires_at.isnot(None),
            TaskAssignment.lease_expires_at < now,
        ).join(Task).join(Project).filter(Project.owner_id == user.id).all()
        for assignment in expired_assignments:
            assignment.state = TaskAssignmentState.EXPIRED
            assignment.completed_at = now
            for run in AgentRun.query.filter_by(
                assignment_id=assignment.id, status=AgentRunStatus.RUNNING
            ).all():
                run.status = AgentRunStatus.EXPIRED
                run.ended_at = now
        report["expired_leases"] = len(expired_assignments)

        escalated_ids = _escalate_overdue_tasks(owner_id=user.id)
        report["escalated_tasks"] = len(escalated_ids)
        report["escalated_task_ids"] = escalated_ids
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"health: {str(e)}")

    # --- Stage 2: workflow step timeouts + re-advance ---
    try:
        now = datetime.utcnow()
        running_steps = WorkflowStepRun.query.filter(
            WorkflowStepRun.status == StepStatus.RUNNING,
        ).join(WorkflowRun).filter(
            WorkflowRun.owner_id == user.id,
            WorkflowRun.status == WorkflowStatus.RUNNING,
        ).all()
        timed_out = []
        for sr in running_steps:
            wf_run = sr.run
            if not wf_run or not wf_run.workflow:
                continue
            step_def = WorkflowStep.query.filter_by(
                workflow_id=wf_run.workflow_id, step_key=sr.step_key
            ).first()
            step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
            if not step_def or not step_def.timeout_seconds or step_def.timeout_seconds <= 0:
                continue
            if sr.started_at:
                elapsed = (now - sr.started_at).total_seconds()
                if elapsed > step_def.timeout_seconds:
                    sr.status = StepStatus.FAILED
                    sr.error = f"Step timed out after {int(elapsed)}s (limit: {step_def.timeout_seconds}s)"
                    sr.finished_at = now
                    try:
                        if sr.assignment_id:
                            bound_run = AgentRun.query.filter_by(
                                assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                            ).first()
                            if bound_run:
                                _maybe_finish_sandboxed_execution(
                                    bound_run, SandboxExecutionStatus.TIMEOUT,
                                    error=sr.error,
                                )
                    except Exception:
                        pass
                    timed_out.append({"run_id": wf_run.id, "step_key": sr.step_key})
        db.session.commit()
        for run_id in set(t["run_id"] for t in timed_out):
            wf_run = WorkflowRun.query.get(run_id)
            if wf_run:
                _advance_workflow(wf_run)
        db.session.commit()
        report["timed_out_steps"] = len(timed_out)
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"workflow_timeout: {str(e)}")

    # --- Stage 3: fire due triggers (system-wide, same as fire_due_triggers) ---
    try:
        now = datetime.utcnow()
        due_triggers = WorkflowTrigger.query.filter(
            WorkflowTrigger.is_active == True,
            WorkflowTrigger.next_fire_at != None,
            WorkflowTrigger.next_fire_at <= now,
        ).all()
        fired_run_ids = []
        for trigger in due_triggers:
            workflow = Workflow.query.get(trigger.workflow_id)
            if not workflow or not workflow.is_active:
                trigger.is_active = False
                continue
            wf_run = WorkflowRun.create(
                workflow_id=workflow.id,
                owner_id=trigger.owner_id,
                project_id=trigger.project_id,
                root_task_id=trigger.root_task_id,
                status=WorkflowStatus.PENDING,
                context=trigger.context_override or {},
            )
            db.session.flush()
            definition = workflow.definition or {}
            for step_def in definition.get("steps", []):
                WorkflowStepRun.create(
                    run_id=wf_run.id,
                    step_key=step_def.get("step_key", ""),
                    status=StepStatus.PENDING,
                )
            wf_run.status = WorkflowStatus.RUNNING
            _advance_workflow(wf_run)
            trigger.fire_count = (trigger.fire_count or 0) + 1
            trigger.last_fired_at = now
            if trigger.cron_expr:
                trigger.next_fire_at = _compute_next_fire(trigger.cron_expr, now)
            elif trigger.one_shot_at:
                trigger.is_active = False
                trigger.next_fire_at = None
            fired_run_ids.append(wf_run.id)
            AuditLog.record(
                action="workflow_trigger.fired", resource_type="workflow_trigger", resource_id=trigger.id,
                actor_type="system",
                detail={"workflow_run_id": wf_run.id, "fire_count": trigger.fire_count, "via": "orchestrator"},
            )
        db.session.commit()
        report["triggers_fired"] = len(fired_run_ids)
        report["trigger_run_ids"] = fired_run_ids
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"triggers: {str(e)}")

    # --- Stage 4: conflict detection + auto-resolution ---
    try:
        now = datetime.utcnow()
        detected = []
        detected.extend(_detect_duplicate_claims(user, now))
        detected.extend(_detect_assignment_stale(user, now))
        detected.extend(_detect_protocol_deadlock(user, now))
        for c in detected:
            db.session.add(c)
        db.session.flush()

        candidates = AgentConflict.query.filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.status == ConflictStatus.DETECTED,
            AgentConflict.severity != ConflictSeverity.CRITICAL,
        ).all()
        auto_resolved = 0
        skipped = 0
        for c in candidates:
            strategy = c.suggested_strategy
            if strategy is None or strategy not in _AUTO_SAFE_STRATEGIES:
                skipped += 1
                continue
            c.resolve(strategy, f"Auto-resolved by orchestrator via {strategy.value}",
                      resolved_by_user_id=user.id)
            auto_resolved += 1
        report["conflicts_detected"] = len(detected)
        report["conflicts_auto_resolved"] = auto_resolved
        report["conflicts_skipped"] = skipped
        if detected or auto_resolved:
            AuditLog.record(
                action="conflicts.auto_resolve", resource_type="system", resource_id=0,
                actor_type="system",
                detail={"detected": len(detected), "auto_resolved": auto_resolved,
                        "skipped": skipped, "via": "orchestrator"},
                ip_address=_client_ip(),
            )
            _queue_sse(user.id, "conflicts_auto_resolved", {
                "count": auto_resolved, "via": "orchestrator",
            })
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        report["errors"].append(f"conflicts: {str(e)}")

    flush_sse_notifications()
    duration = (datetime.utcnow() - start).total_seconds()
    message = (f"Orchestration complete: {report['stale_agents']} stale agent(s), "
               f"{report['timed_out_steps']} timed-out step(s), {report['triggers_fired']} trigger(s) fired, "
               f"{report['conflicts_auto_resolved']} conflict(s) auto-resolved"
               + (f", {len(report['errors'])} error(s)" if report["errors"] else ""))
    AuditLog.record(
        action="maintenance.orchestrate", resource_type="system", resource_id=0,
        actor_type=actor_type, actor_user_id=user.id,
        detail={**{k: v for k, v in report.items() if k != "errors"},
                "error_count": len(report["errors"]), "duration_seconds": duration},
        ip_address=_client_ip() if actor_type == "human" else None,
    )
    # Persist a historical record for trend analysis
    try:
        OrchestrationRun.record(
            owner_id=user.id,
            triggered_by="scheduler" if actor_type == "system" else "manual",
            report=report, duration=duration, summary=message,
        )
    except Exception:
        pass  # never fail the cycle on history-write error
    db.session.commit()
    return report, duration, message
