"""
Agent collaboration API — workflow run management routes.

Handles workflow launch, run lifecycle (cancel/pause/resume/retry),
step completion, and workflow run analytics.
"""

import csv
import io
from datetime import datetime, timedelta

from flask import make_response, request

from ._shared import (  # noqa: E402
    agents_bp,
    ApiResponse,
    get_request_args,
    paginate_query,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    AuditLog,
    Notification,
    Project,
    RunLog,
    SharedContext,
    StepStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskEvent,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    WorkflowTrigger,
    AgentChannel,
    AgentChannelMessage,
    CollaborationTemplate,
    KnowledgeEntry,
    WorkflowVersion,
    AgentReputation,
    AgentExperience,
    CrossProjectAgent,
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxExecutionStatus,
    AgentConflict,
    OrchestrationRun,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    notify_sse,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    POSTABLE_EVENT_TYPES,
    POSTABLE_EVENT_CONTENT_MAX,
    ACTIVE_ASSIGNMENT_STATES,
    parse_enum,
    get_owned_agent_or_response,
    get_owned_task_or_response,
    record_task_event,
    apply_assignment_update,
    _expand_capabilities,
    expire_stale_assignments_for_task,
    find_active_assignment,
    _CAPABILITY_HIERARCHY,
)
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
    if args.get("workflow_id", type=int):
        query = query.filter_by(workflow_id=args.get("workflow_id", type=int))
    if args.get("status"):
        try:
            query = query.filter_by(status=WorkflowStatus(args.get("status")))
        except ValueError:
            pass
    query = query.order_by(WorkflowRun.created_at.desc())
    result = paginate_query(query, args)
    items = [r.to_dict(include_step_runs=True) for r in result["items"]]
    return ApiResponse.paginated(items, result["pagination"]).to_response()


@agents_bp.route("/workflows/step-stats", methods=["GET"])
@unified_auth_required
def workflow_step_stats():
    """Per-step-key execution stats across the current user's workflow runs.

    For each ``step_key``: total runs, succeeded, failed, skipped, success
    rate, and average duration (finished_at - started_at, in seconds) for
    completed steps. Reveals which steps are bottlenecks or chronic failure
    points across all workflows the user has launched.
    """
    user = get_current_user()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 30))))
    except (TypeError, ValueError):
        limit = 30

    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(WorkflowRun.owner_id == user.id)
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.status,
            WorkflowStepRun.started_at,
            WorkflowStepRun.finished_at,
            WorkflowStepRun.attempt,
        )
        .all()
    )
    agg: dict = {}
    for step_key, status, started, finished, attempt in rows:
        entry = agg.setdefault(step_key, {
            "step_key": step_key, "total": 0, "succeeded": 0,
            "failed": 0, "skipped": 0, "durations": [], "retry_count": 0,
        })
        entry["total"] += 1
        # attempt defaults to 1 on first try; >1 means a retry happened
        if attempt and attempt > 1:
            entry["retry_count"] += attempt - 1
        if status == StepStatus.SUCCEEDED:
            entry["succeeded"] += 1
        elif status == StepStatus.FAILED:
            entry["failed"] += 1
        elif status == StepStatus.SKIPPED:
            entry["skipped"] += 1
        if started and finished and finished > started:
            entry["durations"].append((finished - started).total_seconds())

    items = []
    for step_key, e in agg.items():
        durations = e["durations"]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else None
        denom = e["total"] - e["skipped"] or 1
        items.append({
            "step_key": step_key,
            "total": e["total"],
            "succeeded": e["succeeded"],
            "failed": e["failed"],
            "skipped": e["skipped"],
            "success_rate": round(e["succeeded"] / denom, 3),
            "avg_duration_seconds": avg_dur,
            "sample_size_duration": len(durations),
            "retries": e["retry_count"],
            "avg_retries": round(e["retry_count"] / denom, 2),
        })
    items.sort(key=lambda x: x["total"], reverse=True)
    return ApiResponse.success({"items": items[:limit]}).to_response()


@agents_bp.route("/workflows/run-duration-percentiles", methods=["GET"])
@unified_auth_required
def workflow_run_duration_percentiles():
    """Daily trend of workflow run duration percentiles (P50/P90/P95).

    For each day, aggregates completed WorkflowRun durations (finished_at -
    started_at in seconds) and returns P50, P90, P95. Useful for spotting
    regressions in workflow execution time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    cutoff = datetime.utcnow() - timedelta(days=days)
    runs = (
        db.session.query(
            func.date(WorkflowRun.finished_at).label("day"),
            WorkflowRun.finished_at,
            WorkflowRun.started_at,
        )
        .filter(
            WorkflowRun.status == WorkflowStatus.COMPLETED,
            WorkflowRun.finished_at >= cutoff,
            WorkflowRun.started_at.isnot(None),
            WorkflowRun.finished_at.isnot(None),
        )
        .order_by(func.date(WorkflowRun.finished_at))
        .all()
    )

    # Group by day
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in runs:
        dur = (r.finished_at - r.started_at).total_seconds()
        if dur >= 0:
            by_day[str(r.day)].append(dur)

    def percentile(sorted_vals, pct):
        n = len(sorted_vals)
        if n == 0:
            return 0
        idx = int(pct * (n - 1))
        return round(sorted_vals[idx], 1)

    buckets = []
    total_runs = 0
    total_duration = 0.0
    for day in sorted(by_day):
        vals = sorted(by_day[day])
        n = len(vals)
        total_runs += n
        total_duration += sum(vals)
        buckets.append({
            "date": day,
            "count": n,
            "p50": percentile(vals, 0.50),
            "p90": percentile(vals, 0.90),
            "p95": percentile(vals, 0.95),
            "median": percentile(vals, 0.50),
            "avg": round(sum(vals) / n, 1) if n else 0,
        })

    return ApiResponse.success({
        "buckets": buckets,
        "total_runs": total_runs,
        "total_avg_duration": round(total_duration / total_runs, 1) if total_runs else 0,
    }).to_response()


@agents_bp.route("/workflows/step-failure-rate", methods=["GET"])
@unified_auth_required
def workflow_step_failure_rate():
    """Per-step-key failure rate ranking.

    For each step_key, counts total step runs and failed ones,
    computing the failure rate percentage. Sorted by failure rate
    descending. Reveals which workflow steps are the least reliable.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days = 30
        limit = 15

    cutoff = datetime.utcnow() - timedelta(days=days)

    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(WorkflowRun.owner_id == user.id, WorkflowStepRun.created_at >= cutoff)
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.status,
        )
        .all()
    )

    from collections import defaultdict
    step_data: dict = defaultdict(lambda: {"total": 0, "failed": 0})
    for step_key, status in rows:
        if not step_key:
            continue
        step_data[step_key]["total"] += 1
        if status == StepStatus.FAILED:
            step_data[step_key]["failed"] += 1

    items = []
    total_steps = 0
    total_failed = 0
    for step_key, d in step_data.items():
        total_steps += d["total"]
        total_failed += d["failed"]
        items.append({
            "step_key": step_key,
            "total": d["total"],
            "failed": d["failed"],
            "failure_rate": round(d["failed"] / d["total"] * 100, 1) if d["total"] else 0.0,
        })
    items.sort(key=lambda x: x["failure_rate"], reverse=True)

    return ApiResponse.success({
        "items": items[:limit],
        "total_steps": total_steps,
        "total_failed": total_failed,
    }).to_response()


@agents_bp.route("/workflows/failed-steps/by-duration", methods=["GET"])
@unified_auth_required
def workflow_failed_steps_by_duration():
    """Rank failed workflow steps by average duration (finished - started).

    Only FAILED WorkflowStepRun rows with both timestamps are considered.
    For each ``step_key`` reports total failures, average/median/max duration
    in seconds, sorted by average duration descending. Reveals which failing
    steps burn the most wall-clock time before giving up.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        days = 30
        limit = 20

    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.started_at.isnot(None),
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.started_at,
            WorkflowStepRun.finished_at,
        )
        .all()
    )

    agg: dict = {}
    for step_key, started, finished in rows:
        if not (started and finished and finished > started):
            continue
        dur = (finished - started).total_seconds()
        entry = agg.setdefault(step_key, {"step_key": step_key, "durations": []})
        entry["durations"].append(dur)

    items = []
    for step_key, e in agg.items():
        ds = sorted(e["durations"])
        n = len(ds)
        avg = round(sum(ds) / n, 1)
        median = round(ds[n // 2], 1) if n % 2 == 1 else round((ds[n // 2 - 1] + ds[n // 2]) / 2, 1)
        items.append({
            "step_key": step_key,
            "failures": n,
            "avg_duration_seconds": avg,
            "median_duration_seconds": median,
            "max_duration_seconds": round(ds[-1], 1),
        })
    items.sort(key=lambda x: x["avg_duration_seconds"], reverse=True)
    return ApiResponse.success({
        "days": days,
        "total_failed_steps": sum(i["failures"] for i in items),
        "items": items[:limit],
    }).to_response()


@agents_bp.route("/workflows/failure-correlation", methods=["GET"])
@unified_auth_required
def workflow_failure_correlation():
    """Cross-dimension correlation between failed workflow steps and
    collaboration conflicts / sandbox violations.

    For every failed step (status=failed) within the window, checks whether a
    conflict (AgentConflict) or sandbox violation (SandboxViolation) involving
    the same Agent occurred within ±window_hours of the step's finished_at.
    Reports totals and co-occurrence rates, plus the top agents whose failures
    most often coincide with conflicts/violations. Reveals whether failures
    cluster with coordination breakdowns or sandbox escapes.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        window_hours = max(0, min(168, int(request.args.get("window_hours", 2))))
    except (TypeError, ValueError):
        days = 30
        window_hours = 2

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]

    failed_steps = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.agent_id.in_(agent_ids) if agent_ids else sa_false(),
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(
            WorkflowStepRun.id, WorkflowStepRun.step_key, WorkflowStepRun.agent_id,
            WorkflowStepRun.finished_at, WorkflowStepRun.task_id, WorkflowStepRun.run_id,
        )
        .all()
    )

    total_failed = len(failed_steps)
    if total_failed == 0:
        return ApiResponse.success({
            "days": days,
            "window_hours": window_hours,
            "total_failed_steps": 0,
            "with_conflict": 0,
            "with_violation": 0,
            "with_both": 0,
            "conflict_rate": 0,
            "violation_rate": 0,
            "both_rate": 0,
            "top_agents": [],
        }).to_response()

    # Pre-fetch conflicts and violations in the window for these agents
    conflicts = (
        AgentConflict.query
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.created_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(AgentConflict.created_at, AgentConflict.agent_ids)
        .all()
    ) if agent_ids else []
    violations = (
        SandboxViolation.query
        .filter(
            SandboxViolation.agent_id.in_(agent_ids),
            SandboxViolation.blocked_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(SandboxViolation.agent_id, SandboxViolation.blocked_at)
        .all()
    ) if agent_ids else []

    def _near(times, target, agent_id, hours):
        lo = target - timedelta(hours=hours)
        hi = target + timedelta(hours=hours)
        return any(lo <= t <= hi for t in times)

    # Index violations by agent for speed
    violations_by_agent: dict = {}
    for aid, blocked_at in violations:
        violations_by_agent.setdefault(aid, []).append(blocked_at)

    per_agent = {}  # agent_id -> {failed, conflict, violation}
    with_conflict = 0
    with_violation = 0
    with_both = 0
    for _id, step_key, aid, finished_at, task_id, run_id in failed_steps:
        aid_int = aid
        v_times = violations_by_agent.get(aid_int, [])
        has_v = _near(v_times, finished_at, aid_int, window_hours) if v_times else False
        # conflicts store agent_ids list; check membership + time
        has_c = False
        for created_at, agent_ids_json in conflicts:
            if agent_ids_json and aid_int in (agent_ids_json or []):
                if abs((created_at - finished_at).total_seconds()) <= window_hours * 3600:
                    has_c = True
                    break
        if has_c:
            with_conflict += 1
        if has_v:
            with_violation += 1
        if has_c and has_v:
            with_both += 1
        bucket = per_agent.setdefault(aid_int, {"failed": 0, "conflict": 0, "violation": 0, "agent_id": aid_int})
        bucket["failed"] += 1
        if has_c:
            bucket["conflict"] += 1
        if has_v:
            bucket["violation"] += 1

    # Enrich top agents with name
    top_agent_ids = sorted(per_agent.keys(), key=lambda k: per_agent[k]["conflict"] + per_agent[k]["violation"], reverse=True)[:8]
    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(top_agent_ids)).with_entities(Agent.id, Agent.name).all()} if top_agent_ids else {}
    top_agents = []
    for aid in top_agent_ids:
        b = per_agent[aid]
        top_agents.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "failed_steps": b["failed"],
            "with_conflict": b["conflict"],
            "with_violation": b["violation"],
        })

    return ApiResponse.success({
        "days": days,
        "window_hours": window_hours,
        "total_failed_steps": total_failed,
        "with_conflict": with_conflict,
        "with_violation": with_violation,
        "with_both": with_both,
        "conflict_rate": round(with_conflict / total_failed * 100, 1),
        "violation_rate": round(with_violation / total_failed * 100, 1),
        "both_rate": round(with_both / total_failed * 100, 1),
        "top_agents": top_agents,
    }).to_response()


@agents_bp.route("/workflows/failure-correlation-by-step", methods=["GET"])
@unified_auth_required
def workflow_failure_correlation_by_step():
    """Per-step-key failure correlation with conflicts / sandbox violations.

    Like ``workflow_failure_correlation`` but aggregated by ``step_key``:
    for each step key, how many of its failures coincided (±window_hours,
    same Agent) with a conflict or sandbox violation. Reveals which steps
    are most prone to triggering coordination breakdowns or sandbox escapes.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        window_hours = max(0, min(168, int(request.args.get("window_hours", 2))))
    except (TypeError, ValueError):
        days = 30
        window_hours = 2

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]

    failed_steps = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.agent_id.in_(agent_ids) if agent_ids else sa_false(),
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(
            WorkflowStepRun.step_key, WorkflowStepRun.agent_id,
            WorkflowStepRun.finished_at,
        )
        .all()
    )

    if not failed_steps:
        return ApiResponse.success({
            "days": days,
            "window_hours": window_hours,
            "items": [],
            "step_conflict_type_matrix": {},
        }).to_response()

    conflicts = (
        AgentConflict.query
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.created_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(AgentConflict.created_at, AgentConflict.agent_ids, AgentConflict.conflict_type)
        .all()
    ) if agent_ids else []
    violations = (
        SandboxViolation.query
        .filter(
            SandboxViolation.agent_id.in_(agent_ids),
            SandboxViolation.blocked_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(SandboxViolation.agent_id, SandboxViolation.blocked_at)
        .all()
    ) if agent_ids else []

    violations_by_agent: dict = {}
    for aid, blocked_at in violations:
        violations_by_agent.setdefault(aid, []).append(blocked_at)

    per_step: dict = {}
    for step_key, aid, finished_at in failed_steps:
        aid_int = aid
        v_times = violations_by_agent.get(aid_int, [])
        has_v = any(abs((t - finished_at).total_seconds()) <= window_hours * 3600 for t in v_times) if v_times else False
        has_c = False
        matched_conflict_types: set = set()
        for created_at, agent_ids_json, ctype in conflicts:
            if agent_ids_json and aid_int in (agent_ids_json or []):
                if abs((created_at - finished_at).total_seconds()) <= window_hours * 3600:
                    has_c = True
                    if ctype is not None:
                        matched_conflict_types.add(ctype.value if hasattr(ctype, 'value') else str(ctype))
        bucket = per_step.setdefault(step_key, {"step_key": step_key, "failed": 0, "with_conflict": 0, "with_violation": 0, "conflict_types": {}})
        bucket["failed"] += 1
        if has_c:
            bucket["with_conflict"] += 1
            for ct in matched_conflict_types:
                bucket["conflict_types"][ct] = bucket["conflict_types"].get(ct, 0) + 1
        if has_v:
            bucket["with_violation"] += 1

    items = []
    for b in per_step.values():
        f = b["failed"]
        items.append({
            "step_key": b["step_key"],
            "failed": f,
            "with_conflict": b["with_conflict"],
            "with_violation": b["with_violation"],
            "conflict_rate": round(b["with_conflict"] / f * 100, 1) if f else 0,
            "violation_rate": round(b["with_violation"] / f * 100, 1) if f else 0,
            "conflict_types": b.get("conflict_types", {}),
        })
    items.sort(key=lambda x: (x["with_conflict"] + x["with_violation"], x["failed"]), reverse=True)

    # 步骤 × 冲突类型矩阵：{step_key: {conflict_type: count}}
    step_conflict_type_matrix: dict = {}
    for it in items:
        for ct, c in (it.get("conflict_types") or {}).items():
            step_conflict_type_matrix.setdefault(it["step_key"], {})[ct] = c

    return ApiResponse.success({
        "days": days,
        "window_hours": window_hours,
        "items": items[:30],
        "step_conflict_type_matrix": step_conflict_type_matrix,
    }).to_response()


@agents_bp.route("/workflows/step-cofailure-matrix", methods=["GET"])
@unified_auth_required
def workflow_step_cofailure_matrix():
    """Step-key co-failure matrix for the current user.

    For each failed workflow run, collects the set of failed step_keys.
    Builds a symmetric co-occurrence matrix: for each pair (step_a, step_b),
    counts how many runs both failed. Returns top N step_keys by failure
    count with the N×N matrix. Reveals which steps tend to fail together,
    indicating shared failure causes or cascading failures.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(2, min(15, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days = 30
        limit = 8

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]

    # Get all failed step runs in window, grouped by run_id
    failed_steps = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.agent_id.in_(agent_ids) if agent_ids else sa_false(),
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .with_entities(WorkflowStepRun.run_id, WorkflowStepRun.step_key)
        .all()
    )

    # Group failed step_keys by run_id
    run_failed: dict = {}  # {run_id: set(step_keys)}
    for run_id, step_key in failed_steps:
        if run_id not in run_failed:
            run_failed[run_id] = set()
        if step_key:
            run_failed[run_id].add(step_key)

    # Count per-step failures and co-failure pairs
    step_fail_count: dict = {}  # {step_key: count}
    pair_count: dict = {}  # {(a, b): count} where a < b
    for step_keys in run_failed.values():
        keys = sorted(step_keys)
        for k in keys:
            step_fail_count[k] = step_fail_count.get(k, 0) + 1
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pair = (keys[i], keys[j])
                pair_count[pair] = pair_count.get(pair, 0) + 1

    if not step_fail_count:
        return ApiResponse.success({"step_keys": [], "matrix": {}, "max_cofailure": 0, "total_runs_with_multi_failure": 0}).to_response()

    # Top N step_keys by failure count
    top_keys = sorted(step_fail_count.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    top_key_list = [k for k, _ in top_keys]
    top_key_set = set(top_key_list)

    # Build matrix
    matrix: dict = {}  # {step_a: {step_b: count}}
    max_cofailure = 0
    for (a, b), c in pair_count.items():
        if a in top_key_set and b in top_key_set:
            matrix.setdefault(a, {})[b] = c
            matrix.setdefault(b, {})[a] = c
            if c > max_cofailure:
                max_cofailure = c

    total_multi = sum(1 for ks in run_failed.values() if len(ks) >= 2)

    return ApiResponse.success({
        "step_keys": [{"step_key": k, "failures": step_fail_count[k]} for k in top_key_list],
        "matrix": matrix,
        "max_cofailure": max_cofailure,
        "total_runs_with_multi_failure": total_multi,
    }).to_response()


@agents_bp.route("/workflows/step-retry-topology", methods=["GET"])
@unified_auth_required
def workflow_step_retry_topology():
    """Step retry topology for the current user's workflows.

    Groups WorkflowStepRun by (workflow_id, step_key) and counts attempts
    (attempt > 1). Returns per-step: total_runs, retry_count, retry_rate,
    first_attempt_success_rate, retry_success_rate. Shows whether retries
    actually recover failures.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(30, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days = 30
        limit = 15

    since = datetime.utcnow() - timedelta(days=days)

    # Get workflow IDs owned by user
    wf_ids = [wid for wid, in WorkflowRun.query.filter(
        WorkflowRun.owner_id == user.id,
        WorkflowRun.finished_at.isnot(None),
        WorkflowRun.finished_at >= since,
    ).with_entities(WorkflowRun.workflow_id).all()]

    if not wf_ids:
        return ApiResponse.success({"days": days, "steps": [], "total_retries": 0}).to_response()

    # Get step runs for those runs
    rows = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.run_id.in_(
                WorkflowRun.query.filter(
                    WorkflowRun.owner_id == user.id,
                    WorkflowRun.finished_at >= since,
                ).with_entities(WorkflowRun.id)
            ),
        )
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.attempt,
            WorkflowStepRun.status,
        )
        .all()
    )

    # Group by step_key
    step_data: dict = {}  # {step_key: {total_runs, retries, first_success, retry_success}}
    for step_key, attempt, status in rows:
        if not step_key:
            continue
        d = step_data.setdefault(step_key, {"total_runs": 0, "retries": 0, "first_success": 0, "retry_success": 0, "first_attempts": 0, "retry_attempts": 0})
        d["total_runs"] += 1
        s = status.value if status else ""
        if attempt == 1 or attempt is None:
            d["first_attempts"] += 1
            if s == "succeeded":
                d["first_success"] += 1
        else:
            d["retries"] += 1
            d["retry_attempts"] += 1
            if s == "succeeded":
                d["retry_success"] += 1

    # Sort by retry count desc, limit
    sorted_steps = sorted(step_data.items(), key=lambda kv: kv[1]["retries"], reverse=True)[:limit]
    total_retries = sum(d["retries"] for _, d in sorted_steps)
    steps_out = []
    for sk, d in sorted_steps:
        first_total = max(d["first_attempts"], 1)
        retry_total = max(d["retry_attempts"], 1)
        steps_out.append({
            "step_key": sk,
            "total_runs": d["total_runs"],
            "retries": d["retries"],
            "retry_rate": round(d["retries"] / max(d["total_runs"], 1) * 100, 1),
            "first_attempt_success_rate": round(d["first_success"] / first_total * 100, 1),
            "retry_success_rate": round(d["retry_success"] / retry_total * 100, 1) if d["retry_attempts"] else 0.0,
        })

    return ApiResponse.success({
        "days": days,
        "steps": steps_out,
        "total_retries": total_retries,
    }).to_response()


@agents_bp.route("/workflows/step-hourly-distribution", methods=["GET"])
@unified_auth_required
def workflow_step_hourly_distribution():
    """Step execution hour-of-day distribution for the current user.

    Groups WorkflowStepRun by (step_key, hour_of_day) based on
    started_at. Returns per-step hourly distribution revealing which
    steps run during business hours vs overnight.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        WorkflowStepRun.query
        .filter(
            WorkflowStepRun.run_id.in_(
                WorkflowRun.query.filter(
                    WorkflowRun.owner_id == user.id,
                    WorkflowRun.finished_at >= since,
                ).with_entities(WorkflowRun.id)
            ),
            WorkflowStepRun.started_at.isnot(None),
        )
        .with_entities(WorkflowStepRun.step_key, WorkflowStepRun.started_at)
        .all()
    )

    step_hours: dict = {}  # {step_key: {hour: count}}
    step_total: dict = {}  # {step_key: total}
    for step_key, started_at in rows:
        if not step_key:
            continue
        h = started_at.hour
        bucket = step_hours.setdefault(step_key, {})
        bucket[h] = bucket.get(h, 0) + 1
        step_total[step_key] = step_total.get(step_key, 0) + 1

    # Top N by total executions
    top = sorted(step_total.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    steps_out = []
    for sk, total in top:
        hours = step_hours.get(sk, {})
        peak_hour = max(hours, key=hours.get) if hours else None
        # Business hours ratio (8-18)
        biz = sum(hours.get(h, 0) for h in range(8, 18))
        steps_out.append({
            "step_key": sk,
            "total": total,
            "hours": hours,
            "peak_hour": peak_hour,
            "business_hours_ratio": round(biz / total * 100, 1) if total else 0.0,
        })

    return ApiResponse.success({
        "days": days,
        "steps": steps_out,
    }).to_response()





@agents_bp.route("/workflows/run-trend", methods=["GET"])
@unified_auth_required
def workflow_run_trend():
    """Daily workflow run outcome trend for the current user.

    Buckets by calendar day (UTC) using ``finished_at`` (when the run reached
    a terminal state). Each bucket has ``succeeded`` and ``failed`` counts.
    Runs still pending/running/paused/cancelled are excluded (no finish time
    or non-terminal). Useful for charting workflow reliability over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    from sqlalchemy import func as sa_func
    daily = (
        db.session.query(
            sa_func.date(WorkflowRun.finished_at).label("date"),
            WorkflowRun.status,
            sa_func.count(WorkflowRun.id).label("count"),
        )
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowRun.finished_at.isnot(None),
            WorkflowRun.finished_at >= since,
        )
        .group_by(sa_func.date(WorkflowRun.finished_at), WorkflowRun.status)
        .all()
    )
    trend_map: dict = {}
    for d, status, c in daily:
        key = str(d)
        bucket = trend_map.setdefault(key, {"date": key, "succeeded": 0, "failed": 0})
        if status == WorkflowStatus.SUCCEEDED:
            bucket["succeeded"] = c
        elif status == WorkflowStatus.FAILED:
            bucket["failed"] = c
    trend = sorted(trend_map.values(), key=lambda x: x["date"])
    total_succeeded = sum(b["succeeded"] for b in trend)
    total_failed = sum(b["failed"] for b in trend)

    # 按日失败步骤数（WorkflowStepRun status=FAILED，按 finished_at 分桶）
    step_daily = (
        db.session.query(
            sa_func.date(WorkflowStepRun.finished_at).label("date"),
            sa_func.count(WorkflowStepRun.id).label("count"),
        )
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowStepRun.status == StepStatus.FAILED,
            WorkflowStepRun.finished_at.isnot(None),
            WorkflowStepRun.finished_at >= since,
        )
        .group_by(sa_func.date(WorkflowStepRun.finished_at))
        .all()
    )
    step_failed_by_day = {str(d): c for d, c in step_daily if d}
    for b in trend:
        b["failed_steps"] = step_failed_by_day.get(b["date"], 0)
    total_failed_steps = sum(step_failed_by_day.values())

    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_succeeded": total_succeeded,
        "total_failed": total_failed,
        "total_failed_steps": total_failed_steps,
    }).to_response()


@agents_bp.route("/workflows/success-rate-by-workflow", methods=["GET"])
@unified_auth_required
def workflow_success_rate_by_workflow():
    """Per-workflow run success rate comparison for the current user.

    Groups finished workflow runs by workflow_id. Per workflow: total runs,
    succeeded, failed, cancelled, success_rate, avg duration (seconds).
    Sorted by total runs descending, limited to top N. Reveals which
    workflows are the most/least reliable.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    # Get finished runs in window
    rows = (
        WorkflowRun.query
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowRun.finished_at.isnot(None),
            WorkflowRun.finished_at >= since,
        )
        .with_entities(
            WorkflowRun.workflow_id,
            WorkflowRun.status,
            WorkflowRun.started_at,
            WorkflowRun.finished_at,
        )
        .all()
    )

    # Resolve workflow names
    wf_ids = list(set(r.workflow_id for r in rows))
    name_map = {}
    if wf_ids:
        for wid, wname in db.session.query(Workflow.id, Workflow.name).filter(Workflow.id.in_(wf_ids)).all():
            name_map[wid] = wname or f"Workflow#{wid}"

    wf_data: dict = {}  # {wf_id: {total, succeeded, failed, cancelled, dur_sum, dur_n}}
    for wid, status, started, finished in rows:
        if wid not in wf_data:
            wf_data[wid] = {"total": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "dur_sum": 0.0, "dur_n": 0}
        wf_data[wid]["total"] += 1
        s = status.value if status else ""
        if s == "succeeded":
            wf_data[wid]["succeeded"] += 1
        elif s == "failed":
            wf_data[wid]["failed"] += 1
        elif s == "cancelled":
            wf_data[wid]["cancelled"] += 1
        if started and finished:
            dur = (finished - started).total_seconds()
            wf_data[wid]["dur_sum"] += dur
            wf_data[wid]["dur_n"] += 1

    items = []
    for wid, d in sorted(wf_data.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]:
        total = d["total"]
        items.append({
            "workflow_id": wid,
            "name": name_map.get(wid, f"Workflow#{wid}"),
            "total": total,
            "succeeded": d["succeeded"],
            "failed": d["failed"],
            "cancelled": d["cancelled"],
            "success_rate": round(d["succeeded"] / total * 100, 1) if total else 0.0,
            "avg_duration": round(d["dur_sum"] / d["dur_n"], 1) if d["dur_n"] else 0.0,
        })

    return ApiResponse.success({"workflows": items}).to_response()


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

    This is the callback that the Agent system calls when a step finishes.
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
    now = datetime.utcnow()

    if success:
        sr.status = StepStatus.SUCCEEDED
        # Auto-save step result to SharedContext for downstream steps
        if sr.task_id:
            result_summary = data.get("result_summary", "")
            if result_summary:
                existing = SharedContext.query.filter_by(task_id=sr.task_id, key=f"step_result_{step_key}").first()
                if existing:
                    existing.value = result_summary
                    if sr.agent_id:
                        existing.author_agent_id = sr.agent_id
                else:
                    SharedContext.create(
                        task_id=sr.task_id,
                        key=f"step_result_{step_key}",
                        value=result_summary,
                        author_agent_id=sr.agent_id,
                    )
    else:
        sr.status = StepStatus.FAILED
        sr.error = data.get("error", "")

        # Auto-retry: if the step definition has retry_count and we haven't exhausted attempts
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=step_key
        ).first()
        # Apply runtime overrides so a dynamically-reconfigured retry_count takes effect
        step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
        if step_def and step_def.retry_count > 0:
            current_attempt = sr.attempt or 1
            if current_attempt <= step_def.retry_count:
                # Reset step for retry
                sr.status = StepStatus.PENDING
                sr.error = None
                sr.finished_at = None
                sr.attempt = current_attempt + 1
                # Cancel the old assignment/run
                if sr.assignment_id:
                    old_assignment = TaskAssignment.query.get(sr.assignment_id)
                    if old_assignment and old_assignment.state in LEASED_EXECUTION_STATES:
                        old_assignment.state = TaskAssignmentState.CANCELLED
                        old_assignment.completed_at = now
                if sr.task_id:
                    old_runs = AgentRun.query.filter_by(
                        assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                    ).all()
                    for r in old_runs:
                        r.status = AgentRunStatus.CANCELLED
                        r.ended_at = now
                sr.assignment_id = None
                sr.agent_id = None
                sr.task_id = None
                # Record retry event
                if sr.task_id:
                    record_task_event(
                        task_id=sr.task_id,
                        event_type="workflow_step_auto_retry",
                        actor_type="system",
                        payload={"step_key": step_key, "attempt": current_attempt + 1, "max_retries": step_def.retry_count},
                    )

    sr.finished_at = now

    # Update Agent reputation based on step outcome
    if sr.agent_id and sr.status != StepStatus.PENDING:  # Don't update on auto-retry
        completion_time = None
        if sr.started_at and sr.finished_at:
            completion_time = (sr.finished_at - sr.started_at).total_seconds()
        AgentReputation.record_outcome(
            agent_id=sr.agent_id,
            success=(sr.status == StepStatus.SUCCEEDED),
            completion_time=completion_time,
            context={
                "task_id": sr.task_id,
                "step_key": sr.step_key,
                "workflow_run_id": sr.run_id,
                "duration_sec": round(completion_time, 1) if completion_time else None,
            },
        )

        # Auto-extract experience from step outcome
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=step_key
        ).first()
        task = Task.query.get(sr.task_id) if sr.task_id else None
        try:
            AgentExperience.extract_from_step_outcome(
                agent_id=sr.agent_id,
                step_run=sr,
                step_def=step_def,
                task=task,
            )
        except Exception:
            pass  # Don't fail the step completion if experience extraction fails

        # Complete any sandboxed execution bound to this step's AgentRun
        try:
            if sr.assignment_id:
                bound_run = AgentRun.query.filter_by(
                    assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                ).first()
                if bound_run:
                    sandbox_status = (
                        SandboxExecutionStatus.COMPLETED
                        if sr.status == StepStatus.SUCCEEDED
                        else SandboxExecutionStatus.FAILED
                    )
                    _maybe_finish_sandboxed_execution(
                        bound_run,
                        sandbox_status,
                        summary=data.get("result_summary"),
                        error=data.get("error"),
                    )
        except Exception:
            pass  # Don't fail step completion if sandbox finalization fails

    db.session.commit()

    # Notify clients that a step reached a terminal/intermediate state so the
    # real-time console can refresh without polling.
    _queue_sse(user.id, "workflow_step_finished", {
        "run_id": run_id,
        "step_key": step_key,
        "status": sr.status.value if sr.status else None,
        "agent_id": sr.agent_id,
        "attempt": sr.attempt,
    })

    # Advance the workflow
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()

    return ApiResponse.success(
        wf_run.to_dict(include_step_runs=True),
        f"Step {step_key} {'succeeded' if success else 'failed'}",
    ).to_response()


# --- Internal: DAG advancement logic -------------------------------------


def _evaluate_step_condition(condition, step_runs):
    """Evaluate a step's condition against the current step run states.

    Condition format:
      - Simple: {"step_key": "review", "operator": "succeeded", "value": true}
      - Negation: {"step_key": "review", "operator": "failed"}
      - Output match: {"step_key": "review", "operator": "output_contains", "value": "approved"}
      - Composite (AND): {"all": [cond1, cond2]}
      - Composite (OR): {"any": [cond1, cond2]}

    Returns True if the step should execute, False to skip.
    """
    if not condition:
        return True

    # Composite conditions
    if "all" in condition:
        return all(_evaluate_step_condition(c, step_runs) for c in condition["all"])
    if "any" in condition:
        return any(_evaluate_step_condition(c, step_runs) for c in condition["any"])

    # Simple condition
    step_key = condition.get("step_key")
    operator = condition.get("operator", "succeeded")
    value = condition.get("value")

    if not step_key:
        return True  # No step_key means no condition

    sr = step_runs.get(step_key)
    if not sr:
        return False  # Dependency step hasn't started yet

    if operator == "succeeded":
        return sr.status == StepStatus.SUCCEEDED
    elif operator == "failed":
        return sr.status == StepStatus.FAILED
    elif operator == "skipped":
        return sr.status == StepStatus.SKIPPED
    elif operator == "completed":
        return sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED)
    elif operator == "output_equals":
        return (sr.result_summary or "") == str(value)
    elif operator == "output_contains":
        return str(value) in (sr.result_summary or "")
    elif operator == "output_not_contains":
        return str(value) not in (sr.result_summary or "")
    elif operator == "status_equals":
        return sr.status.value == str(value) if hasattr(sr.status, 'value') else str(sr.status) == str(value)
    else:
        # Unknown operator — default to True (don't block execution)
        return True


def _propagate_sub_workflow_completion(sub_wf_run):
    """When a sub-workflow completes, find and advance the parent step that launched it.

    Looks for step runs whose result_summary contains "sub_workflow_run:{id}".
    When found, marks the parent step as succeeded/failed and advances the parent workflow.
    """
    if not sub_wf_run or sub_wf_run.status not in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED):
        return

    # Search for step runs that reference this sub-workflow
    parent_step_runs = WorkflowStepRun.query.filter(
        WorkflowStepRun.result_summary.like(f"%sub_workflow_run:{sub_wf_run.id}%"),
        WorkflowStepRun.status == StepStatus.RUNNING,
    ).all()

    now = datetime.utcnow()
    for psr in parent_step_runs:
        if sub_wf_run.status == WorkflowStatus.SUCCEEDED:
            psr.status = StepStatus.SUCCEEDED
            psr.result_summary = f"Sub-workflow #{sub_wf_run.id} completed successfully"
        else:
            psr.status = StepStatus.FAILED
            psr.error = f"Sub-workflow #{sub_wf_run.id} failed"
            psr.result_summary = f"Sub-workflow #{sub_wf_run.id} failed"
        psr.finished_at = now

        # Update reputation for the agent that managed the sub-workflow
        if psr.agent_id:
            AgentReputation.record_outcome(
                agent_id=psr.agent_id,
                success=(sub_wf_run.status == WorkflowStatus.SUCCEEDED),
                context={
                    "parent_workflow_run_id": psr.run_id,
                    "sub_workflow_run_id": sub_wf_run.id,
                    "step_key": psr.step_key,
                },
            )

        # Advance the parent workflow
        parent_run = WorkflowRun.query.get(psr.run_id)
        if parent_run:
            _advance_workflow(parent_run)


# Keys that may be dynamically overridden on a step run without touching the
# workflow definition. Validated against this allowlist when an override is set.
_RUNTIME_OVERRIDABLE_KEYS = {
    "agent_id",
    "required_capabilities",
    "timeout_seconds",
    "retry_count",
    "on_failure",
    "condition",
    "task_template_id",
    "sub_workflow_id",
}


def _apply_runtime_overrides(step_def, step_run):
    """Return a view of step_def with any runtime overrides from step_run applied.

    Uses a SimpleNamespace so downstream code (which reads attributes like
    step_def.agent_id, step_def.required_capabilities, etc.) works unchanged.
    The original WorkflowStep definition is never mutated.
    """
    overrides = (step_run.runtime_overrides if step_run else None) or {}
    if not overrides:
        return step_def
    from types import SimpleNamespace
    merged = SimpleNamespace(
        step_key=step_def.step_key,
        name=step_def.name,
        description=step_def.description,
        order=step_def.order,
        required_capabilities=step_def.required_capabilities,
        agent_id=step_def.agent_id,
        task_template_id=step_def.task_template_id,
        depends_on=step_def.depends_on,
        condition=step_def.condition,
        sub_workflow_id=step_def.sub_workflow_id,
        timeout_seconds=step_def.timeout_seconds,
        retry_count=step_def.retry_count,
        on_failure=step_def.on_failure,
    )
    for k, v in overrides.items():
        if k in _RUNTIME_OVERRIDABLE_KEYS and v is not None:
            setattr(merged, k, v)
    return merged


def _advance_workflow(wf_run):
    """Examine step runs and start any whose dependencies are all satisfied.

    If all steps are terminal, mark the workflow as finished.
    """
    now = datetime.utcnow()
    step_runs = {sr.step_key: sr for sr in wf_run.step_runs}
    steps = {
        s.step_key: s
        for s in WorkflowStep.query.filter_by(workflow_id=wf_run.workflow_id).all()
    }

    # Check if the overall workflow is already terminal
    if wf_run.status in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
        return

    any_running = False
    all_terminal = True

    # If workflow is paused, don't start new steps (already-running steps continue)
    is_paused = wf_run.status == WorkflowStatus.PAUSED

    # Check max parallelism from workflow definition
    max_parallel = 0
    if wf_run.workflow:
        max_parallel = wf_run.workflow.max_parallel_steps or 0
    # Also support per-run override from definition
    if not max_parallel and wf_run.workflow and wf_run.workflow.definition:
        max_parallel = wf_run.workflow.definition.get("max_parallel_steps", 0)

    # Count currently running steps
    running_count = sum(1 for sr in step_runs.values() if sr.status == StepStatus.RUNNING)

    for step_key, sr in step_runs.items():
        if sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
            continue  # already terminal
        all_terminal = False

        if sr.status == StepStatus.RUNNING:
            any_running = True
            continue

        # PENDING or WAITING — check dependencies
        step_def = steps.get(step_key)
        if not step_def:
            continue
        # Apply runtime overrides (dynamic reconfiguration) on top of the
        # step definition. Only affects this run; the definition is unchanged.
        step_def = _apply_runtime_overrides(step_def, sr)

        # Don't start new steps if paused
        if is_paused:
            continue

        # Check max parallelism: skip starting if we've hit the limit
        if max_parallel > 0 and running_count >= max_parallel:
            continue

        deps = step_def.depends_on or []
        deps_met = all(
            step_runs.get(dep_key) and step_runs[dep_key].status == StepStatus.SUCCEEDED
            for dep_key in deps
        )

        # Check if any dependency failed
        any_dep_failed = any(
            step_runs.get(dep_key) and step_runs[dep_key].status in (StepStatus.FAILED, StepStatus.CANCELLED)
            for dep_key in deps
        )

        if any_dep_failed:
            # Handle based on on_failure policy
            if step_def.on_failure == "skip":
                sr.status = StepStatus.SKIPPED
                sr.finished_at = now
            elif step_def.on_failure == "continue":
                # Treat as if deps are met — start the step anyway
                _start_step(wf_run, sr, step_def, now)
                any_running = True
                running_count += 1
            else:
                # abort — mark step as waiting (will never start) and fail the workflow
                sr.status = StepStatus.WAITING
            continue

        if deps_met:
            # Check conditional execution
            if step_def.condition:
                if not _evaluate_step_condition(step_def.condition, step_runs):
                    # Condition not met — skip this step
                    sr.status = StepStatus.SKIPPED
                    sr.finished_at = now
                    continue

            _start_step(wf_run, sr, step_def, now)
            any_running = True
            running_count += 1

    # If nothing is running and all are terminal, the workflow is done
    if all_terminal:
        # Determine overall status
        has_failure = any(
            sr.status in (StepStatus.FAILED, StepStatus.CANCELLED)
            for sr in step_runs.values()
        )
        wf_run.status = WorkflowStatus.FAILED if has_failure else WorkflowStatus.SUCCEEDED
        wf_run.finished_at = now

        # Check if this is a sub-workflow — advance the parent step
        _propagate_sub_workflow_completion(wf_run)
    elif any_running and wf_run.status == WorkflowStatus.PENDING:
        wf_run.status = WorkflowStatus.RUNNING
        wf_run.started_at = now


def _pick_agent_for_step(wf_run, step_def):
    """Pick the best Agent for a workflow step based on capabilities, role, workload, and reputation.

    Searches the workflow owner's own agents first, then extends to cross-project
    authorized agents if no suitable match is found.
    """
    agent = None
    if step_def.agent_id:
        agent = Agent.query.get(step_def.agent_id)
    if not agent and step_def.required_capabilities:
        required = set(step_def.required_capabilities)
        is_coordination_step = "coordination" in required or "management" in required

        # Phase 1: Search own agents
        candidates = Agent.query.filter(
            Agent.status == AgentStatus.ACTIVE,
            Agent.owner_id == wf_run.owner_id,
        ).all()

        scored = []
        for c in candidates:
            expanded = _expand_capabilities(set(c.capabilities or []))
            match_count = len(required.intersection(expanded))
            if match_count == 0:
                continue
            role = c.collaboration_role or "standalone"
            role_bonus = 0
            if is_coordination_step and role == "leader":
                role_bonus = 100
            elif not is_coordination_step and role == "follower":
                role_bonus = 50
            active_count = TaskAssignment.query.filter(
                TaskAssignment.agent_id == c.id,
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
            ).count()
            workload_penalty = active_count * 15
            # Reputation bonus
            rep = AgentReputation.query.filter_by(agent_id=c.id).first()
            rep_bonus = int((rep.score - 50) * 0.3) if rep and rep.score > 50 else 0
            scored.append((c, match_count * 10 + role_bonus - workload_penalty + rep_bonus, active_count))

        # Phase 2: If no good match, search cross-project agents
        if not scored or scored[0][1] < 20:
            # Get the workflow's project
            wf = Workflow.query.get(wf_run.workflow_id)
            if wf and wf.project_id:
                cross_auths = CrossProjectAgent.get_active_for_project(wf.project_id)
                for auth in cross_auths:
                    c = Agent.query.get(auth.agent_id)
                    if not c or c.status != AgentStatus.ACTIVE:
                        continue
                    # Use effective capabilities (may be overridden for this project)
                    caps = CrossProjectAgent.get_effective_capabilities(c.id, wf.project_id)
                    expanded = _expand_capabilities(set(caps or []))
                    match_count = len(required.intersection(expanded))
                    if match_count == 0:
                        continue
                    # Check concurrent task limit
                    active_count = TaskAssignment.query.filter(
                        TaskAssignment.agent_id == c.id,
                        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                    ).count()
                    if active_count >= (auth.max_concurrent_tasks or 3):
                        continue
                    # Cross-project agents get a small penalty (prefer own agents)
                    cross_penalty = -5
                    role = c.collaboration_role or "standalone"
                    role_bonus = 0
                    if is_coordination_step and role == "leader":
                        role_bonus = 100
                    elif not is_coordination_step and role == "follower":
                        role_bonus = 50
                    rep = AgentReputation.query.filter_by(agent_id=c.id).first()
                    rep_bonus = int((rep.score - 50) * 0.3) if rep and rep.score > 50 else 0
                    scored.append((c, match_count * 10 + role_bonus - active_count * 15 + rep_bonus + cross_penalty, active_count))

        if scored:
            scored.sort(key=lambda x: -x[1])
            agent = scored[0][0]

    if not agent:
        leader = Agent.query.filter_by(
            status=AgentStatus.ACTIVE, owner_id=wf_run.owner_id,
            collaboration_role="leader",
        ).first()
        if leader:
            agent = leader
        else:
            agent = Agent.query.filter_by(
                status=AgentStatus.ACTIVE, owner_id=wf_run.owner_id,
            ).first()
    return agent


def _start_step(wf_run, step_run, step_def, now):
    """Start a single step: find a matching Agent, create a task, and claim it.

    If the step has a sub_workflow_id, launch that workflow instead of creating
    a single task. The sub-workflow's completion will be tracked as this step's
    outcome.
    """
    step_run.status = StepStatus.RUNNING
    step_run.started_at = now

    # --- Sub-workflow handling ---
    if step_def.sub_workflow_id:
        sub_wf = Workflow.query.filter_by(id=step_def.sub_workflow_id, owner_id=wf_run.owner_id).first()
        if not sub_wf:
            step_run.status = StepStatus.FAILED
            step_run.error = f"Sub-workflow {step_def.sub_workflow_id} not found"
            step_run.finished_at = now
            return

        # Find an agent to own the sub-workflow (prefer coordinator/leader)
        agent = _pick_agent_for_step(wf_run, step_def)

        # Launch the sub-workflow
        sub_run = WorkflowRun.create(
            workflow_id=sub_wf.id,
            root_task_id=wf_run.root_task_id,
            project_id=wf_run.project_id,
            owner_id=wf_run.owner_id,
            status=WorkflowStatus.PENDING,
        )
        # Create step runs for sub-workflow
        for sub_step in sub_wf.steps:
            WorkflowStepRun.create(
                run_id=sub_run.id,
                step_key=sub_step.step_key,
                status=StepStatus.PENDING,
            )
        db.session.commit()

        # Store the sub-run reference on the step_run
        step_run.result_summary = f"sub_workflow_run:{sub_run.id}"
        if agent:
            step_run.agent_id = agent.id

        # Advance the sub-workflow
        _advance_workflow(sub_run)
        db.session.commit()

        record_task_event(
            task_id=wf_run.root_task_id,
            event_type="sub_workflow_launched",
            actor_type="system",
            payload={
                "parent_run_id": wf_run.id,
                "parent_step_key": step_def.step_key,
                "sub_workflow_id": sub_wf.id,
                "sub_run_id": sub_run.id,
            },
        )
        return

    # --- Normal step handling ---
    # Find a matching Agent
    agent = _pick_agent_for_step(wf_run, step_def)

    if not agent:
        step_run.status = StepStatus.FAILED
        step_run.error = "No available Agent"
        step_run.finished_at = now
        return

    step_run.agent_id = agent.id

    # Create a task for this step
    task_title = f"[Workflow:{wf_run.id}] {step_def.name}"
    task_content = step_def.description or ""

    # Inject predecessor step outputs as context
    deps = step_def.depends_on or []
    if deps:
        predecessor_context_parts = []
        for dep_key in deps:
            dep_sr = WorkflowStepRun.query.filter_by(run_id=wf_run.id, step_key=dep_key).first()
            if dep_sr and dep_sr.task_id:
                ctx_entries = SharedContext.query.filter_by(task_id=dep_sr.task_id).order_by(SharedContext.key.asc()).all()
                if ctx_entries:
                    parts = [f"--- 前置步骤 [{dep_key}] 上下文 ---"]
                    for entry in ctx_entries:
                        parts.append(f"**{entry.key}** (by {entry.author_agent.name if entry.author_agent else 'user'}):\n{entry.value}")
                    predecessor_context_parts.append("\n".join(parts))
        if predecessor_context_parts:
            task_content = (task_content + "\n\n" if task_content else "") + "\n\n".join(predecessor_context_parts)
    task_kwargs = dict(
        project_id=wf_run.project_id,
        title=task_title,
        content=task_content,
        status=TaskStatus.TODO,
        is_ai_task=True,
        creator_id=wf_run.owner_id,
        parent_task_id=wf_run.root_task_id,
    )
    # If there's a task template, use its defaults
    if step_def.task_template_id:
        tmpl = TaskTemplate.query.get(step_def.task_template_id)
        if tmpl:
            task_kwargs["title"] = task_title + f" (from: {tmpl.name})"
            if tmpl.content_template:
                task_kwargs["content"] = tmpl.content_template
            if tmpl.priority:
                try:
                    from models.task import TaskPriority
                    task_kwargs["priority"] = TaskPriority(tmpl.priority)
                except ValueError:
                    pass
            if tmpl.tags:
                task_kwargs["tags"] = tmpl.tags
            if tmpl.is_ai_task is not None:
                task_kwargs["is_ai_task"] = tmpl.is_ai_task

    task = Task.create(**task_kwargs)
    step_run.task_id = task.id

    # Claim the task for the agent
    assignment = TaskAssignment.create(
        task_id=task.id,
        agent_id=agent.id,
        assigned_by_user_id=wf_run.owner_id,
        state=TaskAssignmentState.ASSIGNED,
    )
    run = AgentRun.create(
        task_id=task.id,
        agent_id=agent.id,
        assignment_id=assignment.id,
        status=AgentRunStatus.RUNNING,
        started_at=now,
    )
    step_run.assignment_id = assignment.id

    # --- Sandbox integration: auto-start a sandboxed execution if the agent
    # has an active sandbox policy bound to it. The policy snapshot is frozen
    # so audits remain valid even if the policy changes later. ---
    _maybe_start_sandboxed_execution(agent, run, step_run)

    # Record event
    record_task_event(
        task_id=task.id,
        event_type="workflow_step_started",
        actor_type="system",
        payload={
            "workflow_run_id": wf_run.id,
            "step_key": step_def.step_key,
            "agent_id": agent.id,
            "agent_name": agent.name,
        },
    )
    # SSE so the real-time console reflects step start/assignment immediately
    _queue_sse(wf_run.owner_id, "workflow_step_started", {
        "run_id": wf_run.id,
        "step_key": step_def.step_key,
        "agent_id": agent.id,
        "agent_name": agent.name,
    })


def _maybe_start_sandboxed_execution(agent, run, step_run):
    """If the agent has an active sandbox, create a RUNNING SandboxExecution
    bound to this AgentRun + WorkflowStepRun, freezing the policy snapshot.

    Returns the created SandboxExecution or None.
    """
    sandbox = AgentSandbox.get_for_agent(agent.id)
    if not sandbox:
        return None
    execution = SandboxExecution(
        sandbox_id=sandbox.id,
        agent_id=agent.id,
        run_id=run.id,
        step_run_id=step_run.id if step_run else None,
        status=SandboxExecutionStatus.RUNNING,
        policy_snapshot=sandbox.to_policy_dict(),
        started_at=datetime.utcnow(),
        tool_calls=0,
        network_calls=0,
    )
    db.session.add(execution)
    db.session.flush()
    return execution


def _maybe_finish_sandboxed_execution(run, status, summary=None, error=None):
    """Complete any RUNNING SandboxExecution bound to an AgentRun.

    Called when a workflow step / agent run completes (success or failure).
    """
    if not run:
        return None
    execution = SandboxExecution.query.filter_by(
        run_id=run.id, status=SandboxExecutionStatus.RUNNING
    ).first()
    if not execution:
        return None
    execution.finish(status, summary=summary, error=error)
    return execution


# =========================================================================
# Priority auto-escalation
# =========================================================================

_PRIORITY_LADDER = {
    "low": "medium",
    "medium": "high",
    "high": "urgent",
}


def _escalate_overdue_tasks(owner_id=None, overdue_after_days=1):
    """Auto-escalate the priority of overdue tasks that are not yet urgent.

    Tasks whose due_date is in the past and whose status is not in a terminal
    state (done / cancelled) will have their priority bumped one level.
    Returns the list of escalated task IDs.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=overdue_after_days)

    query = Task.query.filter(
        Task.due_date.isnot(None),
        Task.due_date < cutoff,
        Task.status.notin_([TaskStatus.DONE, TaskStatus.CANCELLED]),
        Task.priority != TaskPriority.URGENT,
    )
    if owner_id:
        project_ids = [p.id for p in Project.query.filter_by(owner_id=owner_id).all()]
        query = query.filter(Task.project_id.in_(project_ids))

    escalated = []
    for task in query.all():
        current = task.priority.value if task.priority else "medium"
        next_level = _PRIORITY_LADDER.get(current)
        if next_level:
            try:
                task.priority = TaskPriority(next_level)
                db.session.add(task)
                escalated.append(task.id)
                Notification.create_notification(
                    user_id=task.project.owner_id if task.project and task.project.owner_id else None,
                    event_type="task_priority_escalated",
                    task_id=task.id,
                    payload={
                        "old_priority": current,
                        "new_priority": next_level,
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                    },
                )
            except (ValueError, AttributeError):
                pass

    if escalated:
        db.session.commit()

    return escalated



