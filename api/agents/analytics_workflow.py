"""
Workflow analysis endpoints: similarity matrix, step duration histogram,
step bottleneck timeline, and structural complexity.
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
    AgentRun,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
)


@agents_bp.route("/workflows/similarity-matrix", methods=["GET"])
@unified_auth_required
def workflow_similarity_matrix():
    """Compute pairwise Jaccard similarity between workflow step sequences.

    For each pair of completed workflow runs (within the same workflow
    definition), computes Jaccard similarity of their step_key sets.
    Returns a matrix suitable for heatmap visualization, plus a list
    of most-similar and least-similar pairs.

    Query params:
    - days: lookback window (1-365, default 30)
    - limit: max workflow definitions to analyze (1-10, default 5)
    - max_runs: max runs per workflow to compare (2-50, default 20)
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(10, int(request.args.get("limit", 5))))
        max_runs = max(2, min(50, int(request.args.get("max_runs", 20))))
    except (TypeError, ValueError):
        days = 30
        limit = 5
        max_runs = 20

    since = datetime.utcnow() - timedelta(days=days)

    # Find workflows with completed runs
    workflows = (
        Workflow.query
        .filter(Workflow.owner_id == user.id)
        .all()
    )

    results = []
    for wf in workflows:
        # Get completed runs with their step_keys
        runs = (
            WorkflowRun.query
            .filter(
                WorkflowRun.workflow_id == wf.id,
                WorkflowRun.owner_id == user.id,
                WorkflowRun.finished_at.isnot(None),
                WorkflowRun.finished_at >= since,
            )
            .order_by(WorkflowRun.finished_at.desc())
            .limit(max_runs)
            .all()
        )

        if len(runs) < 2:
            continue

        # Collect step_key sets per run
        run_data = []
        for r in runs:
            step_keys = set()
            for sr in (r.step_runs or []):
                if sr.step_key:
                    step_keys.add(sr.step_key)
            run_data.append({
                "run_id": r.id,
                "step_keys": step_keys,
                "status": r.status.value if r.status else "unknown",
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            })

        # Compute pairwise Jaccard similarity
        n = len(run_data)
        matrix = []
        similar_pairs = []
        dissimilar_pairs = []

        for i in range(n):
            row = []
            for j in range(n):
                if i == j:
                    sim = 1.0
                else:
                    a = run_data[i]["step_keys"]
                    b = run_data[j]["step_keys"]
                    intersection = len(a & b)
                    union = len(a | b)
                    sim = round(intersection / union, 3) if union > 0 else 0.0
                row.append(sim)

                if i < j:
                    pair_info = {
                        "run_a": run_data[i]["run_id"],
                        "run_b": run_data[j]["run_id"],
                        "similarity": sim,
                        "shared_steps": len(run_data[i]["step_keys"] & run_data[j]["step_keys"]),
                        "unique_a": len(run_data[i]["step_keys"] - run_data[j]["step_keys"]),
                        "unique_b": len(run_data[j]["step_keys"] - run_data[i]["step_keys"]),
                    }
                    similar_pairs.append(pair_info)
                    dissimilar_pairs.append(pair_info)

            matrix.append(row)

        similar_pairs.sort(key=lambda p: p["similarity"], reverse=True)
        dissimilar_pairs.sort(key=lambda p: p["similarity"])

        results.append({
            "workflow_id": wf.id,
            "workflow_name": wf.name or f"Workflow#{wf.id}",
            "run_count": n,
            "matrix": matrix,
            "run_ids": [rd["run_id"] for rd in run_data],
            "most_similar": similar_pairs[:3],
            "least_similar": dissimilar_pairs[:3],
        })

        if len(results) >= limit:
            break

    return ApiResponse.success({
        "workflows": results,
        "days": days,
    }).to_response()


@agents_bp.route("/workflows/step-duration-histogram", methods=["GET"])
@unified_auth_required
def workflow_step_duration_histogram():
    """Workflow step duration histogram.

    Buckets completed step durations into time ranges per step_key.
    Reveals whether step durations are normal or long-tailed.

    Buckets: 0-10s, 10-30s, 30-60s, 60-120s, 120-300s, 300s+

    Query params:
    - days: lookback window (1-90, default 30)
    - limit: max step keys returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    # Get completed steps with duration
    from models.agent import WorkflowRunStep
    steps = (
        WorkflowRunStep.query
        .join(AgentRun, WorkflowRunStep.run_id == AgentRun.id)
        .join(Agent, AgentRun.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            WorkflowRunStep.completed_at >= since,
            WorkflowRunStep.completed_at != None,
            WorkflowRunStep.started_at != None,
        )
        .with_entities(
            WorkflowRunStep.step_key,
            WorkflowRunStep.started_at,
            WorkflowRunStep.completed_at,
        )
        .all()
    )

    bucket_ranges = ["0-10s", "10-30s", "30-60s", "60-120s", "120-300s", "300s+"]
    bucket_thresholds = [0, 10, 30, 60, 120, 300]

    step_data = {}  # step_key -> [count per bucket]
    for step_key, started_at, completed_at in steps:
        if not step_key or not started_at or not completed_at:
            continue
        dur = (completed_at - started_at).total_seconds()
        if dur < 0:
            continue

        if step_key not in step_data:
            step_data[step_key] = [0] * 6

        # Find bucket
        for i in range(len(bucket_thresholds) - 1, -1, -1):
            if dur >= bucket_thresholds[i]:
                step_data[step_key][i] += 1
                break

    # Sort by total count descending
    sorted_steps = sorted(step_data.items(), key=lambda kv: sum(kv[1]), reverse=True)[:limit]

    results = []
    for step_key, counts in sorted_steps:
        buckets = [{"range": bucket_ranges[i], "count": counts[i]} for i in range(6)]
        results.append({
            "step_key": step_key,
            "buckets": buckets,
            "total": sum(counts),
        })

    return ApiResponse.success({"steps": results, "days": days}).to_response()


@agents_bp.route("/workflows/step-bottleneck-timeline", methods=["GET"])
@unified_auth_required
def workflow_step_bottleneck_timeline():
    """Per-step daily average duration timeline.

    Tracks how each workflow step's average duration changes over time
    to spot regressions (slower) or improvements. Duration is computed
    Python-side (finished - started) for cross-DB compatibility.
    """
    user = get_current_user()
    try:
        days = max(7, min(90, int(request.args.get("days", 30))))
        limit = max(1, min(15, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days, limit = 30, 8

    from models.agent import WorkflowStepRun, WorkflowRun
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        WorkflowStepRun.query
        .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
        .filter(
            WorkflowRun.owner_id == user.id,
            WorkflowStepRun.started_at >= since,
            WorkflowStepRun.started_at.isnot(None),
            WorkflowStepRun.finished_at.isnot(None),
        )
        .with_entities(
            WorkflowStepRun.step_key,
            WorkflowStepRun.started_at,
            WorkflowStepRun.finished_at,
        )
        .all()
    )

    date_range = [(datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d") for i in range(days)]
    step_data = {}
    for step_key, started, finished in rows:
        if not step_key or not started or not finished:
            continue
        dur = (finished - started).total_seconds()
        if dur < 0:
            continue
        d_str = finished.strftime("%Y-%m-%d")
        sd = step_data.setdefault(step_key, {"days": {}, "total": 0})
        sd["days"].setdefault(d_str, []).append(dur)
        sd["total"] += 1

    sorted_steps = sorted(step_data.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]
    results = []
    for step_key, sd in sorted_steps:
        series = []
        for d in date_range:
            durs = sd["days"].get(d, [])
            series.append(round(sum(durs) / len(durs), 1) if durs else 0.0)
        nonzero = [v for v in series if v > 0]
        avg_overall = round(sum(nonzero) / len(nonzero), 1) if nonzero else 0.0
        last_val = next((v for v in reversed(series) if v > 0), 0.0)
        first_val = next((v for v in series if v > 0), 0.0)
        change_pct = round((last_val - first_val) / first_val * 100, 1) if first_val > 0 else 0.0
        results.append({
            "step_key": step_key,
            "series": series,
            "avg_duration": avg_overall,
            "sample_count": sd["total"],
            "change_pct": change_pct,
        })

    return ApiResponse.success({
        "steps": results,
        "days": days,
        "date_range": date_range,
    }).to_response()


@agents_bp.route("/workflows/structural-complexity", methods=["GET"])
@unified_auth_required
def workflow_structural_complexity():
    """Analyze the structural (design-time) complexity of workflows.

    For each active workflow owned by the user, computes DAG metrics from
    WorkflowStep.depends_on: step count, maximum dependency depth (longest
    chain, with cycle guard), total edges, average fan-in/fan-out, and
    counts of root (no dependencies) and leaf (nothing depends on them)
    steps. Reveals overly deep or tangled workflow designs.
    """
    import json
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    from models.agent import Workflow, WorkflowStep

    def _as_list(raw):
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return []
        return [str(x) for x in raw] if isinstance(raw, list) else []

    workflows = Workflow.query.filter_by(owner_id=user.id, is_active=True).all()

    results = []
    for wf in workflows:
        steps = WorkflowStep.query.filter_by(workflow_id=wf.id).all()
        if not steps:
            continue
        keys = {s.step_key for s in steps}
        deps = {}
        for s in steps:
            d = {k for k in _as_list(s.depends_on) if k in keys and k != s.step_key}
            deps[s.step_key] = d
        fan_out = {k: 0 for k in keys}
        for dset in deps.values():
            for dep in dset:
                fan_out[dep] = fan_out.get(dep, 0) + 1

        state = {k: 0 for k in keys}   # 0 unvisited, 1 visiting, 2 done
        chain = {k: 0 for k in keys}   # longest dependency chain below k (in edges)
        def depth(k):
            if state[k] == 2:
                return chain[k]
            if state[k] == 1:
                return 0  # cycle guard: break recursion
            state[k] = 1
            best = 0
            for d in deps.get(k, ()):
                best = max(best, depth(d) + 1)
            state[k] = 2
            chain[k] = best
            return best
        for k in keys:
            depth(k)

        n = len(steps)
        total_edges = sum(len(d) for d in deps.values())
        max_depth = (max(chain.values()) + 1) if chain else 1  # nodes
        results.append({
            "workflow_id": wf.id,
            "workflow_name": wf.name,
            "version": wf.version,
            "step_count": n,
            "max_depth": max_depth,
            "total_edges": total_edges,
            "avg_fan_in": round(total_edges / n, 2) if n else 0.0,
            "avg_fan_out": round(total_edges / n, 2) if n else 0.0,
            "root_count": sum(1 for k in keys if not deps.get(k)),
            "leaf_count": sum(1 for k in keys if fan_out.get(k, 0) == 0),
            "parallelism_budget": wf.max_parallel_steps,
        })

    results.sort(key=lambda r: (r["max_depth"], r["total_edges"]), reverse=True)
    return ApiResponse.success({
        "workflows": results[:limit],
        "total_workflows": len(results),
        "avg_steps": round(sum(r["step_count"] for r in results) / len(results), 1) if results else 0,
        "avg_depth": round(sum(r["max_depth"] for r in results) / len(results), 1) if results else 0,
    }).to_response()
