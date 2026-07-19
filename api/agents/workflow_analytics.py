"""
Workflow run analytics routes — stats, failure analysis, trends.

Extracted from workflow_runs.py to separate analytics from CRUD/lifecycle operations.
"""

import csv
import io
from datetime import datetime, timedelta

from flask import make_response, request
from sqlalchemy import false as sa_false

from ._shared import (
    agents_bp,
    ApiResponse,
    get_request_args,
    paginate_query,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentConflict,
    AuditLog,
    RunLog,
    StepStatus,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowStatus,
    SandboxViolation,
)


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
    from ._shared import Workflow
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
