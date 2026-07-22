"""
Collaboration graph analytics endpoints: collaboration graph visualization,
timeline snapshots, and task handoff statistics.
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
    AgentReputation,
    AuditLog,
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
