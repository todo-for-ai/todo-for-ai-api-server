"""
Cross-cutting analytics endpoints: task allocation fairness and workload forecast.

Capability and skill analytics endpoints (capability gap analysis, skill matching,
specialization evolution, capability supply-demand) are in analytics_capability.py.

Workflow analysis endpoints (similarity matrix, step duration histogram,
step bottleneck timeline, structural complexity) are in analytics_workflow.py.
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
    AgentStatus,
    AgentRun,
    AgentRunStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Project,
)


# ---------------------------------------------------------------------------
# Task Allocation Fairness
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Workload Forecast
# ---------------------------------------------------------------------------

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