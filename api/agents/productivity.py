"""
Agent productivity, failure analysis, resource usage, and idle ranking endpoints.
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
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    WorkflowRun,
    WorkflowStatus,
    get_request_args,
    paginate_query,
)

@agents_bp.route("/run-resource-usage", methods=["GET"])
@unified_auth_required
def agent_run_resource_usage():
    """Agent run resource usage ranking.

    Per-agent: total runs, total wall-clock hours (sum of ended_at -
    started_at for completed runs), average run duration in minutes.
    Sorted by total hours descending. Reveals which agents consume
    the most execution time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"items": [], "total_runs": 0}).to_response()

    rows = (
        AgentRun.query
        .filter(
            AgentRun.agent_id.in_(agent_ids),
            AgentRun.status == AgentRunStatus.COMPLETED,
            AgentRun.ended_at >= since,
            AgentRun.started_at.isnot(None),
            AgentRun.ended_at.isnot(None),
        )
        .with_entities(
            AgentRun.agent_id,
            AgentRun.started_at,
            AgentRun.ended_at,
        )
        .all()
    )

    from collections import defaultdict
    agent_data: dict = {}
    total_runs = 0
    for aid, started, ended in rows:
        dur = (ended - started).total_seconds()
        if dur < 0:
            continue
        if aid not in agent_data:
            agent_data[aid] = {"count": 0, "total_seconds": 0.0}
        agent_data[aid]["count"] += 1
        agent_data[aid]["total_seconds"] += dur
        total_runs += 1

    # Attach agent names
    id_name_map = dict(Agent.query.filter(Agent.id.in_(agent_data.keys())).with_entities(Agent.id, Agent.name).all())

    items = []
    for aid, d in agent_data.items():
        avg_min = round(d["total_seconds"] / d["count"] / 60, 1) if d["count"] else 0
        items.append({
            "agent_id": aid,
            "name": id_name_map.get(aid, f"Agent#{aid}"),
            "total_runs": d["count"],
            "total_hours": round(d["total_seconds"] / 3600, 1),
            "avg_run_minutes": avg_min,
        })
    items.sort(key=lambda x: x["total_hours"], reverse=True)

    return ApiResponse.success({"items": items[:limit], "total_runs": total_runs}).to_response()


@agents_bp.route("/productivity", methods=["GET"])
@unified_auth_required
def agent_productivity():
    """Per-Agent productivity stats for the current user.

    Aggregates TaskAssignment rows by agent: total assignments, completed
    (DONE), failed, cancelled, completion rate, and average completion
    duration (completed_at - claimed_at, in hours) for done assignments.
    Reveals each Agent's throughput and reliability.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        days = 30
        limit = 20

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id,
            TaskAssignment.state,
            TaskAssignment.claimed_at,
            TaskAssignment.completed_at,
        )
        .all()
    )

    agg: dict = {}
    durations = {}  # agent_id -> list of hours
    for aid, state, claimed_at, completed_at in rows:
        bucket = agg.setdefault(aid, {
            "agent_id": aid, "total": 0, "done": 0, "failed": 0,
            "cancelled": 0, "expired": 0, "in_progress": 0,
        })
        bucket["total"] += 1
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            if claimed_at and completed_at and completed_at > claimed_at:
                durations.setdefault(aid, []).append((completed_at - claimed_at).total_seconds() / 3600)
        elif s == "failed":
            bucket["failed"] += 1
        elif s == "cancelled":
            bucket["cancelled"] += 1
        elif s == "expired":
            bucket["expired"] += 1
        else:
            bucket["in_progress"] += 1

    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(list(agg.keys()))).with_entities(Agent.id, Agent.name).all()} if agg else {}
    items = []
    for aid, b in agg.items():
        done = b["done"]
        total = b["total"]
        ds = durations.get(aid, [])
        items.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "total": total,
            "done": done,
            "failed": b["failed"],
            "cancelled": b["cancelled"],
            "expired": b["expired"],
            "in_progress": b["in_progress"],
            "completion_rate": round(done / total * 100, 1) if total else 0,
            "avg_completion_hours": round(sum(ds) / len(ds), 2) if ds else None,
        })
    items.sort(key=lambda x: x["done"], reverse=True)

    return ApiResponse.success({"days": days, "items": items[:limit]}).to_response()


@agents_bp.route("/productivity/trend", methods=["GET"])
@unified_auth_required
def agent_productivity_trend():
    """Daily Agent assignment completion trend for the current user.

    Buckets done TaskAssignments (state=DONE, completed_at within window)
    by day, returning per-day done count and failed count (state=FAILED).
    Reveals whether throughput is rising or falling over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "trend": [], "total_done": 0, "total_failed": 0, "by_kind_totals": {}}).to_response()

    # 取 agent_id -> kind 映射，用于按 kind 分层趋势
    kind_map: dict = {}
    for aid, kind in (
        Agent.query
        .filter(Agent.id.in_(agent_ids))
        .with_entities(Agent.id, Agent.kind)
        .all()
    ):
        kind_map[aid] = kind or "unknown"

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
            TaskAssignment.state.in_([TaskAssignmentState.DONE, TaskAssignmentState.FAILED]),
        )
        .with_entities(
            TaskAssignment.agent_id,
            TaskAssignment.state,
            func.date(TaskAssignment.completed_at).label("d"),
        )
        .all()
    )

    by_day: dict = {}
    total_done = 0
    total_failed = 0
    by_kind_totals: dict = {}
    for aid, state, d in rows:
        if not d:
            continue
        key = str(d)
        bucket = by_day.setdefault(key, {"date": key, "done": 0, "failed": 0, "by_kind": {}})
        s = state.value if state else None
        k = kind_map.get(aid, "unknown")
        kind_bucket = bucket["by_kind"].setdefault(k, {"done": 0, "failed": 0})
        kind_total = by_kind_totals.setdefault(k, {"done": 0, "failed": 0})
        if s == "done":
            bucket["done"] += 1
            total_done += 1
            kind_bucket["done"] += 1
            kind_total["done"] += 1
        elif s == "failed":
            bucket["failed"] += 1
            total_failed += 1
            kind_bucket["failed"] += 1
            kind_total["failed"] += 1

    trend = sorted(by_day.values(), key=lambda x: x["date"])
    # 按 done 总数降序排列 by_kind_totals，便于前端取 top kind
    by_kind_totals_sorted = dict(sorted(by_kind_totals.items(), key=lambda kv: kv[1]["done"], reverse=True))
    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_done": total_done,
        "total_failed": total_failed,
        "by_kind_totals": by_kind_totals_sorted,
    }).to_response()


@agents_bp.route("/productivity/alerts", methods=["GET"])
@unified_auth_required
def agent_productivity_alerts():
    """Low-efficiency Agent alert list for the current user.

    Returns Agents whose assignment completion rate falls below
    ``min_completion_rate`` (default 50%) OR whose failure rate exceeds
    ``max_failure_rate`` (default 30%) within the window, provided they have
    at least ``min_assignments`` (default 3) assignments. Each entry includes
    the same productivity fields as ``/agents/productivity`` plus the
    triggering reason. Surfaces Agents needing attention.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        min_completion_rate = max(0, min(100, float(request.args.get("min_completion_rate", 50))))
        max_failure_rate = max(0, min(100, float(request.args.get("max_failure_rate", 30))))
        min_assignments = max(1, min(1000, int(request.args.get("min_assignments", 3))))
    except (TypeError, ValueError):
        days = 30
        min_completion_rate = 50
        max_failure_rate = 30
        min_assignments = 3

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id, TaskAssignment.state,
            TaskAssignment.claimed_at, TaskAssignment.completed_at,
        )
        .all()
    )

    agg: dict = {}
    durations = {}
    for aid, state, claimed_at, completed_at in rows:
        bucket = agg.setdefault(aid, {
            "agent_id": aid, "total": 0, "done": 0, "failed": 0,
            "cancelled": 0, "expired": 0, "in_progress": 0,
        })
        bucket["total"] += 1
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            if claimed_at and completed_at and completed_at > claimed_at:
                durations.setdefault(aid, []).append((completed_at - claimed_at).total_seconds() / 3600)
        elif s == "failed":
            bucket["failed"] += 1
        elif s == "cancelled":
            bucket["cancelled"] += 1
        elif s == "expired":
            bucket["expired"] += 1
        else:
            bucket["in_progress"] += 1

    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(list(agg.keys()))).with_entities(Agent.id, Agent.name).all()} if agg else {}
    items = []
    for aid, b in agg.items():
        total = b["total"]
        if total < min_assignments:
            continue
        done = b["done"]
        failed = b["failed"]
        completion_rate = round(done / total * 100, 1) if total else 0
        failure_rate = round(failed / total * 100, 1) if total else 0
        reasons = []
        if completion_rate < min_completion_rate:
            reasons.append(f"完成率 {completion_rate}% < {min_completion_rate}%")
        if failure_rate > max_failure_rate:
            reasons.append(f"失败率 {failure_rate}% > {max_failure_rate}%")
        if not reasons:
            continue
        ds = durations.get(aid, [])
        items.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "total": total,
            "done": done,
            "failed": failed,
            "cancelled": b["cancelled"],
            "expired": b["expired"],
            "in_progress": b["in_progress"],
            "completion_rate": completion_rate,
            "failure_rate": failure_rate,
            "avg_completion_hours": round(sum(ds) / len(ds), 2) if ds else None,
            "reasons": reasons,
        })
    # 最差优先：按完成率升序、失败率降序
    items.sort(key=lambda x: (x["completion_rate"], -x["failure_rate"]))

    return ApiResponse.success({
        "days": days,
        "min_completion_rate": min_completion_rate,
        "max_failure_rate": max_failure_rate,
        "min_assignments": min_assignments,
        "items": items,
    }).to_response()


@agents_bp.route("/productivity/by-kind", methods=["GET"])
@unified_auth_required
def agent_productivity_by_kind():
    """Productivity comparison grouped by Agent kind for the current user.

    Aggregates TaskAssignment rows by the owning Agent's ``kind`` field:
    per-kind totals, done, failed, cancelled, expired, in_progress,
    agent count, average completion rate, average failure rate, and average
    completion duration (hours). Surfaces how each Agent class performs
    relative to its peers of the same kind.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "items": []}).to_response()

    # kind per agent
    kind_map = {
        aid: (k.value if k else "unknown")
        for aid, k in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.kind).all()
    }

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
        )
        .with_entities(
            TaskAssignment.agent_id, TaskAssignment.state,
            TaskAssignment.claimed_at, TaskAssignment.completed_at,
        )
        .all()
    )

    agg: dict = {}  # kind -> bucket
    durations: dict = {}  # kind -> list of hours
    agents_seen: dict = {}  # kind -> set of agent_id
    for aid, state, claimed_at, completed_at in rows:
        kind = kind_map.get(aid, "unknown")
        bucket = agg.setdefault(kind, {
            "kind": kind, "total": 0, "done": 0, "failed": 0,
            "cancelled": 0, "expired": 0, "in_progress": 0,
        })
        bucket["total"] += 1
        agents_seen.setdefault(kind, set()).add(aid)
        s = state.value if state else None
        if s == "done":
            bucket["done"] += 1
            if claimed_at and completed_at and completed_at > claimed_at:
                durations.setdefault(kind, []).append((completed_at - claimed_at).total_seconds() / 3600)
        elif s == "failed":
            bucket["failed"] += 1
        elif s == "cancelled":
            bucket["cancelled"] += 1
        elif s == "expired":
            bucket["expired"] += 1
        else:
            bucket["in_progress"] += 1

    items = []
    for kind, b in agg.items():
        total = b["total"]
        done = b["done"]
        failed = b["failed"]
        ds = durations.get(kind, [])
        completion_rate = round(done / total * 100, 1) if total else 0
        failure_rate = round(failed / total * 100, 1) if total else 0
        items.append({
            "kind": kind,
            "agent_count": len(agents_seen.get(kind, set())),
            "total": total,
            "done": done,
            "failed": failed,
            "cancelled": b["cancelled"],
            "expired": b["expired"],
            "in_progress": b["in_progress"],
            "completion_rate": completion_rate,
            "failure_rate": failure_rate,
            "avg_completion_hours": round(sum(ds) / len(ds), 2) if ds else None,
        })
    # 完成率降序，失败率升序
    items.sort(key=lambda x: (-x["completion_rate"], x["failure_rate"]))

    return ApiResponse.success({"days": days, "items": items}).to_response()


@agents_bp.route("/productivity/hourly-heatmap", methods=["GET"])
@unified_auth_required
def agent_productivity_hourly_heatmap():
    """Hour-of-day × Agent completion heatmap for the current user.

    Buckets done TaskAssignments (state=DONE, completed_at within window) by
    the hour-of-day (0-23) of ``completed_at`` and the agent_id. Returns a
    matrix {agent_id: {hour: count}} plus per-agent totals, revealing when
    each Agent is most productive. Uses Python-side hour extraction for
    cross-DB compatibility (SQLite has no EXTRACT).
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days = 30
        limit = 15

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "agents": [], "matrix": {}, "max_cell": 0, "peak_hour": None}).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.state == TaskAssignmentState.DONE,
            TaskAssignment.completed_at.isnot(None),
            TaskAssignment.completed_at >= since,
        )
        .with_entities(TaskAssignment.agent_id, TaskAssignment.completed_at)
        .all()
    )

    matrix: dict = {}  # {agent_id: {hour: count}}
    totals: dict = {}  # {agent_id: total}
    hour_totals = [0] * 24
    max_cell = 0
    for aid, completed_at in rows:
        h = completed_at.hour
        bucket = matrix.setdefault(aid, {})
        bucket[h] = bucket.get(h, 0) + 1
        if bucket[h] > max_cell:
            max_cell = bucket[h]
        totals[aid] = totals.get(aid, 0) + 1
        hour_totals[h] += 1

    # 按 done 总数降序取 top N agents
    top_agents = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    peak_hour = max(range(24), key=lambda h: hour_totals[h]) if any(hour_totals) else None
    agents_out = [
        {"agent_id": aid, "name": name_map.get(aid, f"Agent#{aid}"), "done": totals.get(aid, 0)}
        for aid, _ in top_agents
    ]
    matrix_out = {str(aid): matrix.get(aid, {}) for aid, _ in top_agents}
    return ApiResponse.success({
        "days": days,
        "agents": agents_out,
        "matrix": matrix_out,
        "hour_totals": hour_totals,
        "max_cell": max_cell,
        "peak_hour": peak_hour,
    }).to_response()


@agents_bp.route("/productivity/calendar-heatmap", methods=["GET"])
@unified_auth_required
def agent_productivity_calendar_heatmap():
    """Date × Agent completion calendar heatmap for the current user.

    Buckets done TaskAssignments by calendar date (YYYY-MM-DD) and agent_id
    over the last N days. Returns a {agent_id: {date: count}} matrix plus
    per-agent totals and overall date range. Ideal for a GitHub-style
    contribution calendar per agent.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 90))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 90
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({
            "days": days, "agents": [], "matrix": {},
            "max_cell": 0, "date_range": [],
        }).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.state == TaskAssignmentState.DONE,
            TaskAssignment.completed_at.isnot(None),
            TaskAssignment.completed_at >= since,
        )
        .with_entities(TaskAssignment.agent_id, TaskAssignment.completed_at)
        .all()
    )

    matrix: dict = {}  # {agent_id: {date_str: count}}
    totals: dict = {}  # {agent_id: total}
    max_cell = 0
    for aid, completed_at in rows:
        ds = completed_at.strftime("%Y-%m-%d")
        bucket = matrix.setdefault(aid, {})
        bucket[ds] = bucket.get(ds, 0) + 1
        if bucket[ds] > max_cell:
            max_cell = bucket[ds]
        totals[aid] = totals.get(aid, 0) + 1

    top_agents = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    agents_out = [
        {"agent_id": aid, "name": name_map.get(aid, f"Agent#{aid}"), "done": totals.get(aid, 0)}
        for aid, _ in top_agents
    ]
    matrix_out = {str(aid): matrix.get(aid, {}) for aid, _ in top_agents}

    # Build full date range
    date_range = []
    d = since.date() + timedelta(days=1)
    end = datetime.utcnow().date()
    while d <= end:
        date_range.append(d.isoformat())
        d += timedelta(days=1)

    return ApiResponse.success({
        "days": days,
        "agents": agents_out,
        "matrix": matrix_out,
        "max_cell": max_cell,
        "date_range": date_range,
    }).to_response()


@agents_bp.route("/productivity/weekly-comparison", methods=["GET"])
@unified_auth_required
def agent_productivity_weekly_comparison():
    """Week-over-week Agent productivity comparison for the current user.

    Buckets done TaskAssignments by ISO week of completed_at and agent_id.
    Returns per-agent current_week / previous_week done counts and change
    percentage, sorted by current week descending. Reveals which agents
    are ramping up or slowing down.
    """
    user = get_current_user()
    try:
        limit = max(1, min(30, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"agents": [], "total_this_week": 0, "total_last_week": 0}).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    # Compute current and previous ISO week boundaries
    now = datetime.utcnow()
    # Monday of current week
    current_week_start = now - timedelta(days=now.weekday())
    current_week_start = current_week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_week_start = current_week_start - timedelta(weeks=1)

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.state == TaskAssignmentState.DONE,
            TaskAssignment.completed_at.isnot(None),
            TaskAssignment.completed_at >= prev_week_start,
        )
        .with_entities(TaskAssignment.agent_id, TaskAssignment.completed_at)
        .all()
    )

    agent_week: dict = {}  # {agent_id: {"this_week": n, "last_week": m}}
    total_this = 0
    total_last = 0
    for aid, completed_at in rows:
        if aid not in agent_week:
            agent_week[aid] = {"this_week": 0, "last_week": 0}
        if completed_at >= current_week_start:
            agent_week[aid]["this_week"] += 1
            total_this += 1
        else:
            agent_week[aid]["last_week"] += 1
            total_last += 1

    agents_out = []
    for aid, wk in sorted(agent_week.items(), key=lambda kv: kv[1]["this_week"], reverse=True)[:limit]:
        this_w = wk["this_week"]
        last_w = wk["last_week"]
        change = round((this_w - last_w) / last_w * 100, 1) if last_w > 0 else (100.0 if this_w > 0 else 0.0)
        agents_out.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"Agent#{aid}"),
            "this_week": this_w,
            "last_week": last_w,
            "change_pct": change,
        })

    return ApiResponse.success({
        "agents": agents_out,
        "total_this_week": total_this,
        "total_last_week": total_last,
    }).to_response()


@agents_bp.route("/failure-reasons", methods=["GET"])
@unified_auth_required
def agent_failure_reasons():
    """Distribution of Agent run failure reasons for the current user.

    Looks at FAILED AgentRun rows for the user's Agents within the window and
    buckets them by a normalized error type derived from the ``error`` text
    (first line, lowercased, truncated). Returns per-reason counts and the
    affected agents, sorted by count descending. Surfaces the most common
    failure causes across the fleet.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days = 30
        limit = 15

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "total_failed_runs": 0, "items": []}).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    rows = (
        AgentRun.query
        .filter(
            AgentRun.agent_id.in_(agent_ids),
            AgentRun.status == AgentRunStatus.FAILED,
            AgentRun.started_at >= since,
        )
        .with_entities(AgentRun.agent_id, AgentRun.error)
        .all()
    )

    # 归一化错误类型：首行小写、去标点、截断 80 字
    def normalize(err):
        if not err or not str(err).strip():
            return "(无错误信息)"
        first = str(err).strip().splitlines()[0].strip()
        # 去除常见前缀冒号前缀（如 "ValueError: ..." 取冒号前）
        lowered = first.lower()
        # 截断
        return lowered[:80]

    by_reason: dict = {}  # {reason: {count, agents: set}}
    total = 0
    for aid, err in rows:
        reason = normalize(err)
        entry = by_reason.setdefault(reason, {"count": 0, "agents": set()})
        entry["count"] += 1
        entry["agents"].add(aid)
        total += 1

    items = [
        {
            "reason": reason,
            "count": e["count"],
            "affected_agents": sorted(e["agents"]),
            "affected_agent_names": [name_map.get(a, f"Agent#{a}") for a in sorted(e["agents"])],
        }
        for reason, e in by_reason.items()
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    return ApiResponse.success({
        "days": days,
        "total_failed_runs": total,
        "items": items[:limit],
    }).to_response()


@agents_bp.route("/failure-error-patterns", methods=["GET"])
@unified_auth_required
def agent_failure_error_patterns():
    """Agent failure error pattern clustering for the current user.

    Groups FAILED AgentRun rows by error text prefix (first N chars),
    then clusters similar prefixes. Returns pattern clusters with
    count, representative error, affected agents, and time distribution
    (by hour-of-day). Reveals systemic failure patterns.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
        prefix_len = max(10, min(120, int(request.args.get("prefix_len", 40))))
    except (TypeError, ValueError):
        days = 30
        limit = 10
        prefix_len = 40

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "patterns": [], "total_failed": 0}).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    rows = (
        AgentRun.query
        .filter(
            AgentRun.agent_id.in_(agent_ids),
            AgentRun.status == AgentRunStatus.FAILED,
            AgentRun.started_at >= since,
        )
        .with_entities(AgentRun.agent_id, AgentRun.error, AgentRun.started_at)
        .all()
    )

    # Group by error prefix
    pattern_data: dict = {}  # {prefix: {count, agents: set, hours: [h...], sample: str}}
    for aid, err, started_at in rows:
        if not err or not str(err).strip():
            prefix = "(无错误信息)"
        else:
            prefix = str(err).strip().splitlines()[0].strip()[:prefix_len]
        d = pattern_data.setdefault(prefix, {"count": 0, "agents": set(), "hours": [], "sample": err or ""})
        d["count"] += 1
        d["agents"].add(aid)
        if started_at:
            d["hours"].append(started_at.hour)

    # Sort by count desc, limit
    sorted_patterns = sorted(pattern_data.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]
    total_failed = sum(d["count"] for _, d in sorted_patterns)

    patterns_out = []
    for prefix, d in sorted_patterns:
        hour_dist: dict = {}
        for h in d["hours"]:
            hour_dist[h] = hour_dist.get(h, 0) + 1
        peak_hour = max(hour_dist, key=hour_dist.get) if hour_dist else None
        patterns_out.append({
            "pattern": prefix,
            "count": d["count"],
            "affected_agents": [{"agent_id": aid, "name": name_map.get(aid, f"Agent#{aid}")} for aid in sorted(d["agents"])],
            "peak_hour": peak_hour,
            "hour_distribution": dict(sorted(hour_dist.items())),
        })

    return ApiResponse.success({
        "days": days,
        "patterns": patterns_out,
        "total_failed": total_failed,
    }).to_response()
@agents_bp.route("/run-resource-trend", methods=["GET"])
@unified_auth_required
def agent_run_resource_trend():
    """Per-agent daily run count and average duration trend.

    Groups AgentRun by (agent_id, date) over the lookback window.
    Returns per-agent sparkline-friendly daily series for run count
    and average duration (seconds).

    Query params:
    - days: lookback window (1-90, default 14)
    - limit: max agents returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 14))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 14
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    # Aggregate per (agent_id, date)
    rows = (
        AgentRun.query
        .join(Agent, AgentRun.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            AgentRun.started_at >= since,
        )
        .with_entities(
            AgentRun.agent_id,
            Agent.name,
            func.date(AgentRun.started_at).label("run_date"),
            func.count(AgentRun.id).label("run_count"),
            func.avg(
                func.extract("epoch", AgentRun.ended_at - AgentRun.started_at)
            ).label("avg_duration"),
        )
        .group_by(AgentRun.agent_id, Agent.name, func.date(AgentRun.started_at))
        .all()
    )

    # Build per-agent daily series
    agent_data = {}  # agent_id -> {name, days: {date: {count, avg_dur}}}
    for aid, aname, rdate, cnt, avg_dur in rows:
        if aid not in agent_data:
            agent_data[aid] = {"name": aname or f"Agent#{aid}", "days": {}, "total_runs": 0}
        date_str = rdate.isoformat() if hasattr(rdate, 'isoformat') else str(rdate)
        agent_data[aid]["days"][date_str] = {
            "count": cnt,
            "avg_duration": round(float(avg_dur), 1) if avg_dur else 0.0,
        }
        agent_data[aid]["total_runs"] += cnt

    # Generate full date range
    date_range = []
    for i in range(days):
        d = (datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        date_range.append(d)

    # Build output sorted by total runs descending
    results = []
    for aid, data in sorted(agent_data.items(), key=lambda kv: kv[1]["total_runs"], reverse=True)[:limit]:
        count_series = []
        duration_series = []
        for d in date_range:
            day_data = data["days"].get(d, {"count": 0, "avg_duration": 0.0})
            count_series.append(day_data["count"])
            duration_series.append(day_data["avg_duration"])

        results.append({
            "agent_id": aid,
            "agent_name": data["name"],
            "total_runs": data["total_runs"],
            "count_series": count_series,
            "duration_series": duration_series,
        })

    return ApiResponse.success({
        "agents": results,
        "days": days,
        "date_range": date_range,
    }).to_response()

@agents_bp.route("/idle-ranking", methods=["GET"])
@unified_auth_required
def agent_idle_ranking():
    """Rank Agents by how long they have been idle.

    Idle duration is measured from the most recent of Agent.last_seen_at
    and the latest TaskAssignment activity (completed_at /
    last_heartbeat_at). Each Agent is classified as active (<24h), idle
    (1-7d), stale (7-30d), dormant (>30d), or never, surfacing
    stale/dormant Agents for cleanup or reassignment.
    """
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    rows = (
        TaskAssignment.query
        .join(Agent, TaskAssignment.agent_id == Agent.id)
        .filter(Agent.owner_id == user.id)
        .with_entities(
            TaskAssignment.agent_id,
            TaskAssignment.completed_at,
            TaskAssignment.last_heartbeat_at,
        )
        .all()
    )
    last_assign = {}
    for aid, completed_at, heartbeat in rows:
        cand = max([t for t in (completed_at, heartbeat) if t], default=None)
        if cand is None:
            continue
        cur = last_assign.get(aid)
        if cur is None or cand > cur:
            last_assign[aid] = cand

    agents = (
        Agent.query
        .filter_by(owner_id=user.id)
        .with_entities(Agent.id, Agent.name, Agent.status, Agent.last_seen_at)
        .all()
    )
    now = datetime.utcnow()
    results = []
    for aid, aname, status, last_seen in agents:
        candidates = [t for t in (last_seen, last_assign.get(aid)) if t]
        last_activity = max(candidates) if candidates else None
        if last_activity is None:
            idle_hours = None
            stage = "never"
        else:
            idle_hours = (now - last_activity).total_seconds() / 3600
            if idle_hours < 24:
                stage = "active"
            elif idle_hours < 24 * 7:
                stage = "idle"
            elif idle_hours < 24 * 30:
                stage = "stale"
            else:
                stage = "dormant"
        results.append({
            "agent_id": aid,
            "agent_name": aname or f"Agent#{aid}",
            "status": status.value if status else None,
            "last_seen_at": last_seen.isoformat() if last_seen else None,
            "last_activity_at": last_activity.isoformat() if last_activity else None,
            "idle_hours": round(idle_hours, 1) if idle_hours is not None else None,
            "stage": stage,
        })

    results.sort(key=lambda r: -(r["idle_hours"] if r["idle_hours"] is not None else float("inf")))

    stage_counts = {}
    for r in results:
        stage_counts[r["stage"]] = stage_counts.get(r["stage"], 0) + 1
    return ApiResponse.success({
        "agents": results[:limit],
        "total_agents": len(results),
        "stage_counts": stage_counts,
    }).to_response()
