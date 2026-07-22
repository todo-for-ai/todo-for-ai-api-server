"""
Agent failure analysis, resource usage, and error pattern endpoints.
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
    AgentRunStatus,
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
