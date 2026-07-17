"""
Cross-cutting analytics endpoints: collaboration graph, capability gap,
task allocation fairness, skill matching, workload forecast, specialization
evolution, capability supply-demand, workflow similarity, and structural
complexity.
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
    AgentKind,
    AgentStatus,
    AgentRun,
    AgentRunStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    Project,
    CrossProjectAgent,
    AgentReputation,
    AgentExperience,
    get_request_args,
    paginate_query,
    parse_enum,
    ACTIVE_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
)

@agents_bp.route("/collaboration-graph", methods=["GET"])
@unified_auth_required
def collaboration_graph():
    """Platform-wide Agent collaboration graph derived from direct-message
    audit logs. Returns nodes (agents) and edges (message pairs with counts
    and directional breakdown), suitable for a force/radial graph
    visualization.

    Query params: limit (default 50, max 200) caps edges returned (top by
    count); since/until (ISO date/datetime, inclusive) filter the audit
    window. Nodes include only Agents that appear in the top edges.
    Returns: { nodes: [{id, name, kind, messages}],
               edges: [{source, target, count, source_to_target, target_to_source}],
               total_edges }
    """
    user = get_current_user()
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50

    q = AuditLog.query.filter(
        AuditLog.action == "agent.direct_message",
        AuditLog.resource_type == "agent",
        AuditLog.actor_user_id == user.id,
        AuditLog.actor_agent_id.isnot(None),
    )
    since = request.args.get("since")
    if since:
        q = q.filter(AuditLog.created_at >= since)
    until = request.args.get("until")
    if until:
        q = q.filter(AuditLog.created_at <= until)
    rows = q.all()

    # Undirected edge counts with directional breakdown.
    # edge_map[key] = {"total": N, "fwd": count(min->max), "rev": count(max->min)}
    edge_map = {}
    for r in rows:
        a, b = r.actor_agent_id, r.resource_id
        if a is None or b is None or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        entry = edge_map.setdefault(key, {"total": 0, "fwd": 0, "rev": 0})
        entry["total"] += 1
        # 正向：actor 是较小 id；反向：actor 是较大 id
        if r.actor_agent_id == key[0]:
            entry["fwd"] += 1
        else:
            entry["rev"] += 1

    edges_sorted = sorted(edge_map.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]
    node_ids = set()
    edges = []
    for (a, b), entry in edges_sorted:
        node_ids.add(a)
        node_ids.add(b)
        edges.append({
            "source": a,
            "target": b,
            "count": entry["total"],
            "source_to_target": entry["fwd"],
            "target_to_source": entry["rev"],
        })

    agents = Agent.query.filter(Agent.id.in_(list(node_ids))).all() if node_ids else []
    # 批量查 reputation，避免 N+1
    rep_map = {}
    if node_ids:
        reps = AgentReputation.query.filter(AgentReputation.agent_id.in_(list(node_ids))).all()
        rep_map = {r.agent_id: r.score for r in reps}
    # Per-node total messages (degree sum)
    degree = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + e["count"]
        degree[e["target"]] = degree.get(e["target"], 0) + e["count"]
    nodes = [
        {
            "id": a.id,
            "name": a.name,
            "kind": a.kind.value if a.kind else None,
            "messages": degree.get(a.id, 0),
            "reputation": rep_map.get(a.id),
        }
        for a in agents
    ]

    return ApiResponse.success(
        data={"nodes": nodes, "edges": edges, "total_edges": len(edge_map)},
        message="Collaboration graph",
    ).to_response()


# ---------------------------------------------------------------------------
# Workflow Template Marketplace
# ---------------------------------------------------------------------------

@agents_bp.route("/capability-gap-analysis", methods=["GET"])
@unified_auth_required
def agent_capability_gap_analysis():
    """Analyze capability gaps for each agent.

    Compares each agent's declared capabilities against their actual experience
    domains. Identifies:
    - Gaps: domains with successful experiences but NOT in declared capabilities
    - Overclaims: declared capabilities with NO supporting successful experiences
    - Coverage score: ratio of experience-backed capabilities to total declared

    Returns per-agent gap analysis with recommendations.
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
        min_confidence = max(0.0, min(1.0, float(request.args.get("min_confidence", 0.5))))
    except (TypeError, ValueError):
        limit = 10
        min_confidence = 0.5

    # Get all user's agents
    agents = Agent.query.filter_by(owner_id=user.id).all()

    results = []
    for agent in agents:
        caps = set(agent.capabilities or []) if agent.capabilities else set()
        if not caps:
            continue

        # Get successful experience domains for this agent
        success_domains = (
            db.session.query(
                AgentExperience.domain,
                func.count(AgentExperience.id),
                func.avg(AgentExperience.confidence),
            )
            .filter(
                AgentExperience.agent_id == agent.id,
                AgentExperience.experience_type == "success_pattern",
                AgentExperience.confidence >= min_confidence,
                AgentExperience.is_valid == True,
                AgentExperience.domain.isnot(None),
            )
            .group_by(AgentExperience.domain)
            .all()
        )

        # Also check failure domains
        failure_domains = (
            db.session.query(
                AgentExperience.domain,
                func.count(AgentExperience.id),
            )
            .filter(
                AgentExperience.agent_id == agent.id,
                AgentExperience.experience_type == "failure_pattern",
                AgentExperience.is_valid == True,
                AgentExperience.domain.isnot(None),
            )
            .group_by(AgentExperience.domain)
            .all()
        )

        exp_domain_set = {d[0] for d in success_domains if d[0]}
        fail_domain_map = {d[0]: d[1] for d in failure_domains if d[0]}

        # Normalize: lowercase, strip for comparison
        def normalize(s):
            return s.strip().lower() if s else ""

        norm_caps = {normalize(c): c for c in caps}
        norm_exp = {normalize(d) for d in exp_domain_set}

        # Gaps: experience domains not in capabilities
        gap_domains = norm_exp - set(norm_caps.keys())
        gaps = []
        for d in success_domains:
            if normalize(d[0]) in gap_domains:
                gaps.append({
                    "domain": d[0],
                    "success_count": d[1],
                    "avg_confidence": round(float(d[2]), 2) if d[2] else 0.0,
                    "failure_count": fail_domain_map.get(d[0], 0),
                })

        # Overclaims: capabilities with no successful experience
        overclaim_domains = set(norm_caps.keys()) - norm_exp
        overclaims = []
        for nc, oc in norm_caps.items():
            if nc in overclaim_domains:
                fail_count = sum(v for k, v in fail_domain_map.items() if normalize(k) == nc)
                overclaims.append({
                    "capability": oc,
                    "failure_count": fail_count,
                    "risk": "high" if fail_count > 3 else ("medium" if fail_count > 0 else "low"),
                })

        # Coverage score
        backed_caps = set(norm_caps.keys()) & norm_exp
        coverage_score = round(len(backed_caps) / len(norm_caps) * 100, 1) if norm_caps else 0.0

        # Experience strength per matched capability
        matched = []
        for nc, oc in norm_caps.items():
            if nc in norm_exp:
                for d in success_domains:
                    if normalize(d[0]) == nc:
                        matched.append({
                            "capability": oc,
                            "domain": d[0],
                            "success_count": d[1],
                            "avg_confidence": round(float(d[2]), 2) if d[2] else 0.0,
                        })
                        break

        if gaps or overclaims:
            results.append({
                "agent_id": agent.id,
                "agent_name": agent.name or f"Agent#{agent.id}",
                "total_capabilities": len(caps),
                "coverage_score": coverage_score,
                "gaps": gaps,
                "overclaims": overclaims,
                "matched": matched,
            })

    # Sort by coverage score ascending (most gaps first), limit
    results.sort(key=lambda r: r["coverage_score"])
    return ApiResponse.success({"agents": results[:limit]}).to_response()

@agents_bp.route("/collaboration-graph-timeline", methods=["GET"])
@unified_auth_required
def collaboration_graph_timeline():
    """Day-by-day collaboration graph snapshots for timeline replay.

    Returns a sequence of daily snapshots showing active collaboration edges
    for each day in the lookback window. Each snapshot contains only the
    edges active on that day (at least one message between agent pair).

    Query params:
    - days: lookback window (1-90, default 14)
    - bucket: 'day' or 'week' (default 'day')
    - limit: max edges per snapshot (1-200, default 50)

    Returns: { bucket_type, days, snapshots: [{ date, edges: [...] }] }
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 14))))
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        days = 14
        limit = 50
    bucket = request.args.get("bucket", "day")
    if bucket not in ("day", "week"):
        bucket = "day"

    since = datetime.utcnow() - timedelta(days=days)

    # Get all agent-message audit logs in window
    rows = (
        AuditLog.query.filter(
            AuditLog.action == "agent.direct_message",
            AuditLog.resource_type == "agent",
            AuditLog.actor_user_id == user.id,
            AuditLog.actor_agent_id.isnot(None),
            AuditLog.created_at >= since,
        )
        .with_entities(
            AuditLog.actor_agent_id,
            AuditLog.resource_id,
            AuditLog.created_at,
        )
        .all()
    )

    # Bucket by date (or week)
    bucket_map = {}  # {bucket_key: {(a,b): {"total", "fwd", "rev"}}}
    for actor_id, resource_id, created_at in rows:
        if actor_id is None or resource_id is None or actor_id == resource_id:
            continue
        if bucket == "week":
            # ISO week start (Monday)
            week_start = created_at - timedelta(days=created_at.weekday())
            bk = week_start.strftime("%Y-%m-%d")
        else:
            bk = created_at.strftime("%Y-%m-%d")

        key = (actor_id, resource_id) if actor_id < resource_id else (resource_id, actor_id)
        if bk not in bucket_map:
            bucket_map[bk] = {}
        entry = bucket_map[bk].setdefault(key, {"total": 0, "fwd": 0, "rev": 0})
        entry["total"] += 1
        if actor_id == key[0]:
            entry["fwd"] += 1
        else:
            entry["rev"] += 1

    # Resolve agent names
    all_agent_ids = set()
    for bk_edges in bucket_map.values():
        for (a, b) in bk_edges.keys():
            all_agent_ids.add(a)
            all_agent_ids.add(b)

    name_map = {}
    if all_agent_ids:
        for aid, aname in db.session.query(Agent.id, Agent.name).filter(Agent.id.in_(all_agent_ids)).all():
            name_map[aid] = aname or f"Agent#{aid}"

    # Build snapshots sorted by date
    snapshots = []
    for bk in sorted(bucket_map.keys()):
        edges_data = bucket_map[bk]
        edges_sorted = sorted(edges_data.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]
        edges = []
        node_ids_in_snapshot = set()
        for (a, b), entry in edges_sorted:
            node_ids_in_snapshot.add(a)
            node_ids_in_snapshot.add(b)
            edges.append({
                "source": a,
                "target": b,
                "source_name": name_map.get(a, f"Agent#{a}"),
                "target_name": name_map.get(b, f"Agent#{b}"),
                "count": entry["total"],
                "source_to_target": entry["fwd"],
                "target_to_source": entry["rev"],
            })
        snapshots.append({
            "date": bk,
            "edges": edges,
            "total_edges": len(edges_data),
            "active_agents": len(node_ids_in_snapshot),
        })

    return ApiResponse.success({
        "bucket_type": bucket,
        "days": days,
        "snapshots": snapshots,
    }).to_response()

@agents_bp.route("/task-allocation-fairness", methods=["GET"])
@unified_auth_required
def task_allocation_fairness():
    """Analyze task allocation fairness across agents using Gini coefficient.

    Computes per-agent task assignment counts (total, completed, in-progress),
    then calculates the Gini coefficient of the distribution. A Gini of 0
    means perfectly equal distribution; 1 means all tasks go to one agent.

    Also includes per-agent stats and a Lorenz curve data series.

    Query params:
    - days: lookback window (1-365, default 30)
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    # Count assignments per agent
    rows = (
        TaskAssignment.query
        .join(Agent, TaskAssignment.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id,
            Agent.name,
            TaskAssignment.state,
            func.count(TaskAssignment.id),
        )
        .group_by(TaskAssignment.agent_id, Agent.name, TaskAssignment.state)
        .all()
    )

    # Aggregate per agent
    agent_map = {}  # agent_id -> {name, total, completed, in_progress, assigned}
    for aid, aname, state, cnt in rows:
        if aid not in agent_map:
            agent_map[aid] = {"name": aname or f"Agent#{aid}", "total": 0, "completed": 0, "in_progress": 0, "assigned": 0}
        agent_map[aid]["total"] += cnt
        s = state.value if state else ""
        if s == "completed":
            agent_map[aid]["completed"] += cnt
        elif s in ("claimed", "in_progress"):
            agent_map[aid]["in_progress"] += cnt
        else:
            agent_map[aid]["assigned"] += cnt

    if not agent_map:
        return ApiResponse.success({
            "gini": 0.0,
            "agents": [],
            "lorenz_curve": [],
            "days": days,
            "total_tasks": 0,
        }).to_response()

    # Compute Gini coefficient
    totals = sorted(agent_map[aid]["total"] for aid in agent_map)
    n = len(totals)
    total_sum = sum(totals)

    if total_sum == 0 or n == 0:
        gini = 0.0
    else:
        # Gini = (2 * sum(i * x_i)) / (n * sum(x_i)) - (n+1)/n
        weighted_sum = sum((i + 1) * x for i, x in enumerate(totals))
        gini = (2 * weighted_sum) / (n * total_sum) - (n + 1) / n
        gini = max(0.0, min(1.0, round(gini, 3)))

    # Lorenz curve: cumulative share of agents vs cumulative share of tasks
    lorenz = []
    cum_tasks = 0
    for i, x in enumerate(totals):
        cum_tasks += x
        lorenz.append({
            "agent_percent": round((i + 1) / n * 100, 1),
            "task_percent": round(cum_tasks / total_sum * 100, 1),
        })

    # Build agent list sorted by total descending
    agents_list = sorted(agent_map.values(), key=lambda a: a["total"], reverse=True)

    return ApiResponse.success({
        "gini": gini,
        "fairness_level": "equal" if gini < 0.2 else ("moderate" if gini < 0.4 else "unequal"),
        "agents": agents_list,
        "lorenz_curve": lorenz,
        "days": days,
        "total_tasks": total_sum,
    }).to_response()

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


@agents_bp.route("/skill-matching", methods=["GET"])
@unified_auth_required
def agent_skill_matching():
    """Agent skill matching recommendation.

    For unassigned in-progress tasks, match task title/description keywords
    to agent capabilities and experience domains. Returns per-task
    recommended agents with match scores.

    Query params:
    - limit: max tasks returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    from models.agent import Task, TaskAssignment
    # Find unassigned in-progress tasks
    unassigned_tasks = (
        Task.query
        .filter(
            Task.owner_id == user.id,
            Task.status == "in_progress",
            ~Task.id.in_(
                TaskAssignment.query
                .filter(TaskAssignment.status == "active")
                .with_entities(TaskAssignment.task_id)
            ),
        )
        .order_by(Task.created_at.desc())
        .limit(limit * 2)
        .all()
    )

    # Get all active agents with capabilities
    agents = Agent.query.filter(Agent.owner_id == user.id, Agent.status == "active").all()
    agent_caps = {}
    for a in agents:
        caps = set()
        if a.capabilities:
            for c in (a.capabilities if isinstance(a.capabilities, list) else []):
                caps.add(c.lower())
        # Add experience domains
        if hasattr(a, 'experiences') and a.experiences:
            for exp in a.experiences:
                if hasattr(exp, 'domain') and exp.domain:
                    caps.add(exp.domain.lower())
        agent_caps[a.id] = {"name": a.name or f"Agent#{a.id}", "caps": caps}

    results = []
    for task in unassigned_tasks:
        # Extract keywords from task title and description
        text = (task.title or "") + " " + (task.description or "")
        keywords = set(w.lower() for w in text.split() if len(w) > 2)
        if not keywords:
            continue

        recommendations = []
        for aid, info in agent_caps.items():
            if not info["caps"]:
                continue
            matched = keywords & info["caps"]
            if matched:
                score = round(len(matched) / len(keywords) * 100, 1)
                recommendations.append({
                    "agent_id": aid,
                    "agent_name": info["name"],
                    "match_score": score,
                    "matched_capabilities": sorted(matched),
                })

        recommendations.sort(key=lambda r: r["match_score"], reverse=True)
        if recommendations:
            results.append({
                "task_id": task.id,
                "task_title": task.title or f"Task#{task.id}",
                "recommendations": recommendations[:3],
            })

    return ApiResponse.success({"tasks": results[:limit]}).to_response()

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

@agents_bp.route("/task-handoff-stats", methods=["GET"])
@unified_auth_required
def agent_task_handoff_stats():
    """Agent task handoff statistics.

    Aggregates handoff events by (from_agent, to_agent) pairs,
    counts frequency and average handoff duration.

    Query params:
    - days: lookback window (1-90, default 30)
    - limit: max handoff pairs returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    from models.agent import AuditLog
    handoffs = (
        AuditLog.query
        .filter(
            AuditLog.owner_id == user.id,
            AuditLog.action == "agent.handoff",
            AuditLog.created_at >= since,
        )
        .order_by(AuditLog.created_at.desc())
        .all()
    )

    pair_data = {}  # (from, to) -> {count, durations}
    for h in handoffs:
        details = h.details if isinstance(h.details, dict) else {}
        from_agent = details.get("from_agent", "unknown")
        to_agent = details.get("to_agent", "unknown")
        dur = details.get("duration_seconds")

        key = (from_agent, to_agent)
        if key not in pair_data:
            pair_data[key] = {"count": 0, "durations": []}
        pair_data[key]["count"] += 1
        if dur is not None:
            pair_data[key]["durations"].append(float(dur))

    sorted_pairs = sorted(pair_data.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]

    results = []
    for (from_a, to_a), data in sorted_pairs:
        avg_dur = None
        if data["durations"]:
            avg_dur = round(sum(data["durations"]) / len(data["durations"]), 1)
        results.append({
            "from_agent": from_a,
            "to_agent": to_a,
            "count": data["count"],
            "avg_duration_seconds": avg_dur,
        })

    return ApiResponse.success({"handoffs": results, "days": days}).to_response()


@agents_bp.route("/workload-forecast", methods=["GET"])
@unified_auth_required
def agent_workload_forecast():
    """Forecast each Agent's near-future task load via linear regression.

    Uses the daily assignment count over the lookback window as the
    regression signal. Returns per-agent slope (trend direction),
    forecast for the next few days, and recent average.
    """
    user = get_current_user()
    try:
        days = max(7, min(90, int(request.args.get("days", 30))))
        horizon = max(1, min(14, int(request.args.get("horizon", 3))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days, horizon, limit = 30, 3, 10

    from models.agent import TaskAssignment
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        TaskAssignment.query
        .join(Agent, TaskAssignment.agent_id == Agent.id)
        .filter(Agent.owner_id == user.id, TaskAssignment.assigned_at >= since)
        .with_entities(
            TaskAssignment.agent_id,
            Agent.name,
            func.date(TaskAssignment.assigned_at).label("d"),
            func.count(TaskAssignment.id).label("c"),
        )
        .group_by(TaskAssignment.agent_id, Agent.name, func.date(TaskAssignment.assigned_at))
        .all()
    )

    date_range = [(datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d") for i in range(days)]
    agent_days = {}
    for aid, aname, d, c in rows:
        d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        agent_days.setdefault(aid, {"name": aname or f"Agent#{aid}", "counts": {}})["counts"][d_str] = c

    results = []
    for aid, info in agent_days.items():
        series = [info["counts"].get(d, 0) for d in date_range]
        n = len(series)
        total = sum(series)
        if total == 0:
            continue
        x_mean = (n - 1) / 2.0
        y_mean = total / n
        num = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope = num / den if den else 0.0
        intercept = y_mean - slope * x_mean
        forecast = [max(0, round(intercept + slope * (n + k))) for k in range(horizon)]
        recent_avg = round(sum(series[-7:]) / min(7, n), 2)
        results.append({
            "agent_id": aid,
            "agent_name": info["name"],
            "total": total,
            "recent_avg": recent_avg,
            "slope": round(slope, 3),
            "trend": "up" if slope > 0.1 else ("down" if slope < -0.1 else "flat"),
            "series": series,
            "forecast": forecast,
            "forecast_total": sum(forecast),
        })

    results.sort(key=lambda r: r["forecast_total"], reverse=True)
    return ApiResponse.success({
        "agents": results[:limit],
        "days": days,
        "horizon": horizon,
        "date_range": date_range,
    }).to_response()


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


@agents_bp.route("/specialization-evolution", methods=["GET"])
@unified_auth_required
def agent_specialization_evolution():
    """Track how each Agent's domain coverage evolves over time.

    Buckets AgentExperience by (agent, week) and counts distinct domains
    per week. Returns per-agent weekly domain coverage series and the
    list of domains learned, revealing specialization vs generalization.
    """
    user = get_current_user()
    try:
        weeks = max(2, min(26, int(request.args.get("weeks", 12))))
        limit = max(1, min(15, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        weeks, limit = 12, 8

    from models.agent import AgentExperience
    days = weeks * 7
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        AgentExperience.query
        .join(Agent, AgentExperience.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            AgentExperience.created_at >= since,
            AgentExperience.domain.isnot(None),
        )
        .with_entities(
            AgentExperience.agent_id,
            Agent.name,
            AgentExperience.domain,
            AgentExperience.created_at,
        )
        .all()
    )

    now = datetime.utcnow()
    agent_weeks = {}
    for aid, aname, domain, created in rows:
        if not domain or not created:
            continue
        delta_days = (now - created).days
        week_idx = weeks - 1 - (delta_days // 7)
        if week_idx < 0 or week_idx >= weeks:
            continue
        info = agent_weeks.setdefault(aid, {"name": aname or f"Agent#{aid}", "weeks": {}})
        info["weeks"].setdefault(week_idx, set()).add(domain)

    results = []
    for aid, info in agent_weeks.items():
        series = [len(info["weeks"].get(w, set())) for w in range(weeks)]
        all_domains = set()
        for ds in info["weeks"].values():
            all_domains.update(ds)
        if sum(series) == 0:
            continue
        peak = max(series)
        peak_week = series.index(peak) if peak > 0 else 0
        results.append({
            "agent_id": aid,
            "agent_name": info["name"],
            "series": series,
            "peak_domains": peak,
            "peak_week_idx": peak_week,
            "total_domains": len(all_domains),
            "domains": sorted(all_domains)[:10],
        })

    results.sort(key=lambda r: r["total_domains"], reverse=True)
    week_labels = [f"W-{weeks - 1 - w}" for w in range(weeks)]
    return ApiResponse.success({
        "agents": results[:limit],
        "weeks": weeks,
        "week_labels": week_labels,
    }).to_response()



@agents_bp.route("/capability-supply-demand", methods=["GET"])
@unified_auth_required
def capability_supply_demand():
    """Analyze supply vs demand for each capability.

    Supply = number of the user's Agents declaring a capability.
    Demand = number of active (non-terminal) tasks requiring that
    capability, across projects owned by the user. Identifies
    bottleneck capabilities (demand exceeds supply) and surplus
    capabilities (supply with no demand) so owners can rebalance the
    fleet's declared skills against actual task requirements.
    """
    import json
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    from models.task import Task, TaskStatus
    from models.project import Project

    def _as_list(raw):
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return []
        if isinstance(raw, list):
            return [str(c) for c in raw if c]
        return []

    # supply: capabilities declared by the user's agents
    agents = Agent.query.filter_by(owner_id=user.id).with_entities(Agent.capabilities).all()
    supply = {}
    agent_total = 0
    for (caps,) in agents:
        unique = set(_as_list(caps))
        if not unique:
            continue
        agent_total += 1
        for c in unique:
            supply[c] = supply.get(c, 0) + 1

    # demand: capabilities required by active tasks in the user's projects
    tasks = (
        Task.query
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.owner_id == user.id,
            Task.status.in_([
                TaskStatus.TODO,
                TaskStatus.IN_PROGRESS,
                TaskStatus.REVIEW,
                TaskStatus.BLOCKED,
            ]),
        )
        .with_entities(Task.required_capabilities)
        .all()
    )
    demand = {}
    active_task_total = 0
    for (req,) in tasks:
        unique = set(_as_list(req))
        if not unique:
            continue
        active_task_total += 1
        for c in unique:
            demand[c] = demand.get(c, 0) + 1

    all_caps = sorted(set(supply) | set(demand))
    items = []
    for c in all_caps:
        s = supply.get(c, 0)
        d = demand.get(c, 0)
        if d > 0 and s == 0:
            status = "missing"
        elif d > s:
            status = "bottleneck"
        elif d == 0 and s > 0:
            status = "unused_supply"
        elif s > d:
            status = "surplus"
        else:
            status = "balanced"
        items.append({
            "capability": c,
            "supply": s,
            "demand": d,
            "gap": s - d,
            "ratio": round(d / s, 2) if s > 0 else None,
            "status": status,
        })

    items.sort(key=lambda x: (x["demand"], x["supply"]), reverse=True)
    bottleneck = [i for i in items if i["status"] in ("bottleneck", "missing")]
    return ApiResponse.success({
        "capabilities": items[:limit],
        "total_capabilities": len(items),
        "bottleneck_count": len(bottleneck),
        "agent_total": agent_total,
        "active_task_total": active_task_total,
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

