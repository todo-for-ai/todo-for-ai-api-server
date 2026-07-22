"""
Tasks API - completion analysis and forecast routes.
"""

from ._shared import (
    tasks_bp,
    datetime,
    timedelta,
    request,
    func,
    db,
    Task,
    TaskStatus,
    TaskPriority,
    Project,
    TaskHistory,
    ActionType,
    UserActivity,
    ApiResponse,
    paginate_query,
    validate_json_request,
    get_request_args,
    unified_auth_required,
    get_current_user,
)


@tasks_bp.route('/completion-by-project', methods=['GET'])
@unified_auth_required
def task_completion_by_project():
    """Daily task completion trend grouped by project for the current user.

    Buckets done tasks (state=DONE, completed_at within window) by calendar
    day of completed_at and project_id. Returns a per-project series plus
    per-project totals, sorted by total completed descending. Reveals which
    projects are actively delivering over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days = 30
        limit = 8

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        Task.query
        .join(Project)
        .filter(
            Project.owner_id == user.id,
            Task.status == TaskStatus.DONE,
            Task.completed_at.isnot(None),
            Task.completed_at >= since,
        )
        .with_entities(
            func.date(Task.completed_at).label("d"),
            Task.project_id,
            Project.name,
            func.count(Task.id),
        )
        .group_by(func.date(Task.completed_at), Task.project_id, Project.name)
        .all()
    )

    proj_meta: dict = {}  # {project_id: name}
    proj_totals: dict = {}  # {project_id: total}
    by_day_proj: dict = {}  # {date: {project_id: count}}
    for d, pid, pname, c in rows:
        if not d:
            continue
        key = str(d)
        proj_meta[pid] = pname or f"Project#{pid}"
        proj_totals[pid] = proj_totals.get(pid, 0) + c
        by_day_proj.setdefault(key, {})[pid] = c

    # 按 total 降序取 top N
    top = sorted(proj_totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    top_ids = [pid for pid, _ in top]

    # 构建每个 top 项目的每日序列
    all_days = sorted(by_day_proj.keys())
    series = []
    for pid, total in top:
        daily = [{"date": d, "done": (by_day_proj.get(d, {}) or {}).get(pid, 0)} for d in all_days]
        series.append({
            "project_id": pid,
            "name": proj_meta.get(pid, f"Project#{pid}"),
            "total": total,
            "daily": daily,
        })

    return ApiResponse.success({
        "days": days,
        "total_done": sum(proj_totals.values()),
        "all_days": all_days,
        "series": series,
    }).to_response()


@tasks_bp.route("/completion-by-assignee", methods=["GET"])
@unified_auth_required
def task_completion_by_assignee():
    """Daily task completion trend grouped by assignee (Agent) for the current user.

    Buckets done assignments (state=DONE, completed_at within window) by calendar
    day of completed_at and agent_id. Returns a per-agent series plus per-agent
    totals, sorted by total completed descending. Reveals which agents are
    actively delivering over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        days = 30
        limit = 8

    since = datetime.utcnow() - timedelta(days=days)

    from models.agent import TaskAssignment, TaskAssignmentState, Agent

    rows = (
        TaskAssignment.query
        .join(Agent)
        .filter(
            Agent.owner_id == user.id,
            TaskAssignment.state == TaskAssignmentState.DONE,
            TaskAssignment.completed_at.isnot(None),
            TaskAssignment.completed_at >= since,
        )
        .with_entities(
            func.date(TaskAssignment.completed_at).label("d"),
            TaskAssignment.agent_id,
            Agent.name,
            func.count(TaskAssignment.id),
        )
        .group_by(func.date(TaskAssignment.completed_at), TaskAssignment.agent_id, Agent.name)
        .all()
    )

    agent_meta: dict = {}   # {agent_id: name}
    agent_totals: dict = {}  # {agent_id: total}
    by_day_agent: dict = {}  # {date: {agent_id: count}}
    for d, aid, aname, c in rows:
        if not d:
            continue
        key = str(d)
        agent_meta[aid] = aname or f"Agent#{aid}"
        agent_totals[aid] = agent_totals.get(aid, 0) + c
        by_day_agent.setdefault(key, {})[aid] = c

    top = sorted(agent_totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    top_ids = [aid for aid, _ in top]

    all_days = sorted(by_day_agent.keys())
    series = []
    for aid, total in top:
        daily = [{"date": d, "done": (by_day_agent.get(d, {}) or {}).get(aid, 0)} for d in all_days]
        series.append({
            "agent_id": aid,
            "name": agent_meta.get(aid, f"Agent#{aid}"),
            "total": total,
            "daily": daily,
        })

    return ApiResponse.success({
        "days": days,
        "total_done": sum(agent_totals.values()),
        "all_days": all_days,
        "series": series,
    }).to_response()


@tasks_bp.route("/completion-by-priority", methods=["GET"])
@unified_auth_required
def task_completion_by_priority():
    """Task completion rate by priority for the current user's projects.

    Groups tasks by priority and reports total, done, cancelled, and
    completion rate. Reveals whether high-priority tasks are being
    delivered at a comparable rate to low-priority ones.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    project_ids = [p.id for p in Project.query.filter_by(owner_id=user.id).with_entities(Project.id).all()]
    if not project_ids:
        return ApiResponse.success({"priorities": [], "total": 0}).to_response()

    from models.agent import TaskAssignment, TaskAssignmentState

    # Get all tasks in window by priority
    tasks = (
        Task.query
        .filter(
            Task.project_id.in_(project_ids),
            Task.created_at >= since,
        )
        .with_entities(
            Task.priority,
            Task.status,
            func.count(Task.id),
        )
        .group_by(Task.priority, Task.status)
        .all()
    )

    priority_data: dict = {}  # {priority: {total, done, cancelled, ...}}
    total_count = 0
    for priority, status, count in tasks:
        p = priority.value if priority else "unknown"
        if p not in priority_data:
            priority_data[p] = {"total": 0, "done": 0, "cancelled": 0, "in_progress": 0, "other": 0}
        priority_data[p]["total"] += count
        total_count += count
        s = status.value if status else ""
        if s == "done":
            priority_data[p]["done"] += count
        elif s == "cancelled":
            priority_data[p]["cancelled"] += count
        elif s == "in_progress":
            priority_data[p]["in_progress"] += count
        else:
            priority_data[p]["other"] += count

    priorities = []
    for p, d in sorted(priority_data.items(), key=lambda kv: kv[1]["total"], reverse=True):
        total = d["total"]
        priorities.append({
            "priority": p,
            "total": total,
            "done": d["done"],
            "cancelled": d["cancelled"],
            "in_progress": d["in_progress"],
            "completion_rate": round(d["done"] / total * 100, 1) if total else 0.0,
        })

    return ApiResponse.success({"priorities": priorities, "total": total_count}).to_response()


@tasks_bp.route('/completion-rate-by-project', methods=['GET'])
@unified_auth_required
def task_completion_rate_by_project():
    """Task completion rate snapshot comparison across projects.

    Groups tasks by project and reports total, done, in_progress, cancelled,
    and completion_rate. Sorted by total descending, limited to top N projects.
    Reveals which projects have the best/worst delivery rates.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(30, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)
    project_ids = [p.id for p in Project.query.filter_by(owner_id=user.id).with_entities(Project.id).all()]
    if not project_ids:
        return ApiResponse.success({"projects": [], "total_tasks": 0, "total_done": 0}).to_response()

    rows = (
        Task.query
        .join(Project)
        .filter(
            Task.project_id.in_(project_ids),
            Task.created_at >= since,
        )
        .with_entities(
            Task.project_id,
            Project.name,
            Task.status,
            func.count(Task.id),
        )
        .group_by(Task.project_id, Project.name, Task.status)
        .all()
    )

    proj_data: dict = {}  # {project_id: {name, total, done, cancelled, in_progress, other}}
    total_tasks = 0
    total_done = 0
    for pid, pname, status, count in rows:
        if pid not in proj_data:
            proj_data[pid] = {"name": pname or f"Project#{pid}", "total": 0, "done": 0, "cancelled": 0, "in_progress": 0, "other": 0}
        proj_data[pid]["total"] += count
        total_tasks += count
        s = status.value if status else ""
        if s == "done":
            proj_data[pid]["done"] += count
            total_done += count
        elif s == "cancelled":
            proj_data[pid]["cancelled"] += count
        elif s == "in_progress":
            proj_data[pid]["in_progress"] += count
        else:
            proj_data[pid]["other"] += count

    projects = []
    for pid, d in sorted(proj_data.items(), key=lambda kv: kv[1]["total"], reverse=True)[:limit]:
        total = d["total"]
        projects.append({
            "project_id": pid,
            "name": d["name"],
            "total": total,
            "done": d["done"],
            "cancelled": d["cancelled"],
            "in_progress": d["in_progress"],
            "completion_rate": round(d["done"] / total * 100, 1) if total else 0.0,
        })

    return ApiResponse.success({"projects": projects, "total_tasks": total_tasks, "total_done": total_done}).to_response()


@tasks_bp.route('/completion-forecast', methods=['GET'])
@unified_auth_required
def task_completion_forecast():
    """Task completion forecast based on historical velocity.

    Computes daily completion velocity (done tasks per day) over the
    lookback window, then extrapolates to estimate when all remaining
    non-done tasks will be completed. Also provides per-priority
    breakdown of remaining counts and estimated completion dates.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    # Count done tasks per day in window
    done_rows = (
        Task.query
        .filter(Task.owner_id == user.id, Task.status == TaskStatus.DONE, Task.updated_at >= since)
        .with_entities(func.date(Task.updated_at).label("d"), func.count().label("cnt"))
        .group_by(func.date(Task.updated_at))
        .all()
    )

    # Calculate velocity
    total_done_in_window = sum(r.cnt for r in done_rows)
    velocity = total_done_in_window / days  # tasks/day

    # Count remaining tasks by status and priority
    remaining = (
        Task.query
        .filter(Task.owner_id == user.id, ~Task.status.in_([TaskStatus.DONE, TaskStatus.CANCELLED]))
        .with_entities(Task.status, Task.priority, func.count().label("cnt"))
        .group_by(Task.status, Task.priority)
        .all()
    )

    total_remaining = sum(r.cnt for r in remaining)
    priority_remaining: dict = {}
    for status, pri, cnt in remaining:
        p = pri.value if hasattr(pri, "value") else str(pri)
        priority_remaining.setdefault(p, {"remaining": 0})
        priority_remaining[p]["remaining"] += cnt

    # Estimate completion date
    if velocity > 0 and total_remaining > 0:
        days_to_complete = total_remaining / velocity
        estimated_date = (datetime.utcnow() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")
    else:
        days_to_complete = None
        estimated_date = None

    # Per-priority estimated dates (proportional share of velocity)
    priority_forecast = []
    priority_order = ["critical", "high", "medium", "low"]
    cum_days = 0.0
    for p in priority_order:
        pr = priority_remaining.get(p, {})
        rem = pr.get("remaining", 0)
        if rem > 0 and velocity > 0:
            days_for_p = rem / velocity
            cum_days += days_for_p
            est = (datetime.utcnow() + timedelta(days=cum_days)).strftime("%Y-%m-%d")
        else:
            days_for_p = 0
            est = None
        priority_forecast.append({
            "priority": p,
            "remaining": rem,
            "estimated_days": round(days_for_p, 1) if days_for_p else 0,
            "estimated_date": est,
        })

    return ApiResponse.success({
        "days": days,
        "velocity": round(velocity, 2),
        "total_done_in_window": total_done_in_window,
        "total_remaining": total_remaining,
        "days_to_complete": round(days_to_complete, 1) if days_to_complete else None,
        "estimated_completion_date": estimated_date,
        "priority_forecast": priority_forecast,
    }).to_response()


@tasks_bp.route("/comment-sentiment-trend", methods=["GET"])
@unified_auth_required
def task_comment_sentiment_trend():
    """Task comment sentiment trend.

    Aggregates comment events by day and classifies sentiment
    based on keyword matching.

    Positive: 完成/成功/好/赞/解决/通过
    Negative: 失败/问题/bug/错/崩溃/超时/拒绝

    Neutral: everything else

    Query params:
    - days: lookback window (1-90, default 30)
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    from models.agent import TaskEvent
    comments = (
        TaskEvent.query
        .filter(
            TaskEvent.owner_id == user.id,
            TaskEvent.event_type == "comment",
            TaskEvent.created_at >= since,
        )
        .with_entities(
            func.date(TaskEvent.created_at).label("event_date"),
            TaskEvent.content,
        )
        .all()
    )

    positive_words = {"完成", "成功", "好", "赞", "解决", "通过", "修复", "合并", "上线", "搞定"}
    negative_words = {"失败", "问题", "bug", "错", "崩溃", "超时", "拒绝", "阻塞", "错误", "异常", "报错"}

    day_data = {}  # date -> {positive, negative, neutral}
    for event_date, content in comments:
        date_str = event_date.isoformat() if hasattr(event_date, 'isoformat') else str(event_date)
        if date_str not in day_data:
            day_data[date_str] = {"positive": 0, "negative": 0, "neutral": 0}

        if not content:
            day_data[date_str]["neutral"] += 1
            continue

        text_lower = content.lower()
        has_pos = any(w in text_lower for w in positive_words)
        has_neg = any(w in text_lower for w in negative_words)

        if has_neg and not has_pos:
            day_data[date_str]["negative"] += 1
        elif has_pos and not has_neg:
            day_data[date_str]["positive"] += 1
        else:
            day_data[date_str]["neutral"] += 1

    # Build full date range
    date_range = []
    for i in range(days):
        d = (datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        date_range.append(d)

    trend = []
    for d in date_range:
        data = day_data.get(d, {"positive": 0, "negative": 0, "neutral": 0})
        trend.append({
            "date": d,
            "positive": data["positive"],
            "negative": data["negative"],
            "neutral": data["neutral"],
        })

    return ApiResponse.success({"trend": trend, "days": days}).to_response()
