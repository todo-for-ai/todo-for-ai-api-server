"""
Tasks API - analytics and trend routes.
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


@tasks_bp.route('/stats', methods=['GET'])
@unified_auth_required
def task_stats():
    """Aggregate task lifecycle stats for the current user's projects.

    Reports status distribution, completion/cancellation rates, average
    lifecycle duration (done tasks: completed_at - created_at) bucketed
    into ranges, and per-priority counts. Reveals throughput bottlenecks
    and how often work is abandoned vs completed.
    """
    user = get_current_user()

    # 限定当前用户的项目
    base_query = Task.query.join(Project).filter(Project.owner_id == user.id)

    total = base_query.count()
    if total == 0:
        return ApiResponse.success({
            "total": 0,
            "by_status": {},
            "by_priority": {},
            "completion_rate": 0,
            "cancellation_rate": 0,
            "avg_lifecycle_hours": None,
            "lifecycle_buckets": {},
            "avg_completion_rate": 0,
            "by_project": [],
            "overdue_count": 0,
            "with_due_date": 0,
            "overdue_rate": 0,
            "by_priority_status": {},
        }).to_response()

    # 按状态分布
    status_rows = base_query.with_entities(Task.status, func.count(Task.id)).group_by(Task.status).all()
    by_status = {s.value if s else "(未知)": c for s, c in status_rows}

    # 按优先级分布
    priority_rows = base_query.with_entities(Task.priority, func.count(Task.id)).group_by(Task.priority).all()
    by_priority = {p.value if p else "(未知)": c for p, c in priority_rows}

    done_count = by_status.get("done", 0)
    cancelled_count = by_status.get("cancelled", 0)
    completion_rate = round(done_count / total * 100, 1)
    cancellation_rate = round(cancelled_count / total * 100, 1)

    # 生命周期耗时（仅已完成且有 completed_at）
    done_tasks = base_query.filter(
        Task.status == TaskStatus.DONE,
        Task.completed_at.isnot(None),
    ).with_entities(Task.created_at, Task.completed_at).all()

    lifecycle_hours = []
    for created_at, completed_at in done_tasks:
        if created_at and completed_at and completed_at > created_at:
            delta_hours = (completed_at - created_at).total_seconds() / 3600
            if delta_hours >= 0:
                lifecycle_hours.append(delta_hours)

    avg_lifecycle = round(sum(lifecycle_hours) / len(lifecycle_hours), 2) if lifecycle_hours else None

    # 分桶：0-1h, 1-4h, 4-12h, 12-24h, 1-3d, 3-7d, >7d
    buckets = {
        "0-1h": 0, "1-4h": 0, "4-12h": 0, "12-24h": 0,
        "1-3d": 0, "3-7d": 0, ">7d": 0,
    }
    for h in lifecycle_hours:
        if h < 1:
            buckets["0-1h"] += 1
        elif h < 4:
            buckets["1-4h"] += 1
        elif h < 12:
            buckets["4-12h"] += 1
        elif h < 24:
            buckets["12-24h"] += 1
        elif h < 72:
            buckets["1-3d"] += 1
        elif h < 168:
            buckets["3-7d"] += 1
        else:
            buckets[">7d"] += 1

    # 平均完成率（completion_rate 字段）
    cr_rows = base_query.with_entities(func.avg(Task.completion_rate)).scalar()
    avg_completion_rate = round(cr_rows, 1) if cr_rows is not None else 0

    # 按项目分布
    project_rows = (
        base_query.with_entities(Task.project_id, Project.name, func.count(Task.id))
        .group_by(Task.project_id, Project.name)
        .order_by(func.count(Task.id).desc())
        .limit(10)
        .all()
    )
    by_project = [{"project_id": pid, "name": pname or f"#{pid}", "count": c} for pid, pname, c in project_rows]

    # 逾期统计：有 due_date，未结束（非 done/cancelled），且 due_date < now
    now = datetime.utcnow()
    terminal_states = [TaskStatus.DONE, TaskStatus.CANCELLED]
    overdue_query = base_query.filter(
        Task.due_date.isnot(None),
        Task.due_date < now,
        ~Task.status.in_(terminal_states),
    )
    overdue_count = overdue_query.count()
    # 有 due_date 的任务总数（用于算逾期率分母）
    with_due = base_query.filter(Task.due_date.isnot(None)).count()
    overdue_rate = round(overdue_count / with_due * 100, 1) if with_due else 0

    # 优先级 × 状态矩阵：{priority: {status: count}}
    ps_rows = base_query.with_entities(Task.priority, Task.status, func.count(Task.id)).group_by(Task.priority, Task.status).all()
    by_priority_status: dict = {}
    for p, s, c in ps_rows:
        pk = p.value if p else "(未知)"
        sk = s.value if s else "(未知)"
        by_priority_status.setdefault(pk, {})[sk] = by_priority_status.get(pk, {}).get(sk, 0) + c

    return ApiResponse.success({
        "total": total,
        "by_status": by_status,
        "by_priority": by_priority,
        "completion_rate": completion_rate,
        "cancellation_rate": cancellation_rate,
        "done_count": done_count,
        "cancelled_count": cancelled_count,
        "avg_lifecycle_hours": avg_lifecycle,
        "lifecycle_buckets": buckets,
        "avg_completion_rate": avg_completion_rate,
        "by_project": by_project,
        "overdue_count": overdue_count,
        "with_due_date": with_due,
        "overdue_rate": overdue_rate,
        "by_priority_status": by_priority_status,
    }).to_response()


@tasks_bp.route('/overdue-trend', methods=['GET'])
@unified_auth_required
def task_overdue_trend():
    """Daily overdue task trend by due_date for the current user.

    Buckets overdue tasks (due_date < now, status not done/cancelled) by the
    calendar day of their due_date within the lookback window. Also reports
    per-priority overdue counts for the same set. Reveals whether overdue
    workload is accumulating over time and which priorities bear the brunt.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    base_query = (
        Task.query
        .join(Project)
        .filter(Project.owner_id == user.id)
    )
    now = datetime.utcnow()
    since = now - timedelta(days=days)
    terminal_states = [TaskStatus.DONE, TaskStatus.CANCELLED]

    # 逾期且 due_date 在窗口内的任务，按 due_date 日期分桶
    rows = (
        base_query
        .filter(
            Task.due_date.isnot(None),
            Task.due_date < now,
            Task.due_date >= since,
            ~Task.status.in_(terminal_states),
        )
        .with_entities(
            func.date(Task.due_date).label("d"),
            Task.priority,
            func.count(Task.id),
        )
        .group_by(func.date(Task.due_date), Task.priority)
        .all()
    )

    by_day: dict = {}
    by_priority_totals: dict = {}
    total_overdue = 0
    for d, priority, c in rows:
        if not d:
            continue
        key = str(d)
        bucket = by_day.setdefault(key, {"date": key, "overdue": 0, "by_priority": {}})
        bucket["overdue"] += c
        pk = priority.value if priority else "(未知)"
        bucket["by_priority"][pk] = bucket["by_priority"].get(pk, 0) + c
        by_priority_totals[pk] = by_priority_totals.get(pk, 0) + c
        total_overdue += c

    trend = sorted(by_day.values(), key=lambda x: x["date"])
    by_priority_sorted = dict(sorted(by_priority_totals.items(), key=lambda kv: kv[1], reverse=True))
    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_overdue": total_overdue,
        "by_priority_totals": by_priority_sorted,
    }).to_response()


@tasks_bp.route('/overdue-by-assignee', methods=['GET'])
@unified_auth_required
def task_overdue_by_assignee():
    """Overdue task count grouped by assignee (Agent) for the current user.

    Counts tasks that are overdue (due_date < now, status not done/cancelled)
    and have an active assignment. Per agent: overdue count, by-priority
    breakdown, and earliest overdue due_date. Sorted by overdue count
    descending. Reveals which agents bear the heaviest overdue burden.
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    from models.agent import TaskAssignment, TaskAssignmentState, Agent

    now = datetime.utcnow()
    non_terminal = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED]

    # Find overdue tasks with active assignments
    overdue_tasks = (
        Task.query
        .join(Project)
        .filter(
            Project.owner_id == user.id,
            Task.due_date.isnot(None),
            Task.due_date < now,
            Task.status.in_(non_terminal),
        )
        .with_entities(Task.id, Task.priority, Task.due_date)
        .all()
    )

    overdue_ids = [t.id for t in overdue_tasks]
    if not overdue_ids:
        return ApiResponse.success({"items": [], "total_overdue": 0}).to_response()

    # Map task_id -> (priority, due_date)
    task_meta = {t.id: (t.priority, t.due_date) for t in overdue_tasks}

    # Find active assignments for these overdue tasks
    assignments = (
        TaskAssignment.query
        .filter(
            TaskAssignment.task_id.in_(overdue_ids),
            TaskAssignment.state.in_([TaskAssignmentState.ASSIGNED, TaskAssignmentState.CLAIMED]),
        )
        .with_entities(TaskAssignment.task_id, TaskAssignment.agent_id)
        .all()
    )

    # Resolve agent names
    agent_ids = list(set(a.agent_id for a in assignments))
    agent_names = {}
    if agent_ids:
        for a in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all():
            agent_names[a.id] = a.name or f"Agent#{a.id}"

    buckets = {}  # {agent_id: {count, by_priority, earliest_due}}
    for task_id, agent_id in assignments:
        priority, due_date = task_meta.get(task_id, (None, None))
        b = buckets.get(agent_id)
        if b is None:
            b = {"count": 0, "by_priority": {}, "earliest_due": None}
            buckets[agent_id] = b
        b["count"] += 1
        p = priority or "unknown"
        b["by_priority"][p] = b["by_priority"].get(p, 0) + 1
        if due_date and (b["earliest_due"] is None or due_date < b["earliest_due"]):
            b["earliest_due"] = due_date

    items = []
    for aid, b in buckets.items():
        items.append({
            "agent_id": aid,
            "name": agent_names.get(aid, f"Agent#{aid}"),
            "overdue": b["count"],
            "by_priority": b["by_priority"],
            "earliest_due": b["earliest_due"].isoformat() if b["earliest_due"] else None,
        })
    items.sort(key=lambda x: x["overdue"], reverse=True)

    return ApiResponse.success({
        "items": items[:limit],
        "total_overdue": len(overdue_ids),
    }).to_response()


@tasks_bp.route('/overdue-clustering', methods=['GET'])
@unified_auth_required
def task_overdue_clustering():
    """Overdue task clustering analysis by project and priority for the current user.

    Groups overdue tasks (due_date < now, status not done/cancelled) by
    project_id and priority. Per cluster: project name, priority, count,
    avg days overdue, and representative task names. Sorted by count
    descending. Reveals where overdue tasks concentrate and why.
    """
    user = get_current_user()
    try:
        limit = max(1, min(30, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        limit = 15

    now = datetime.utcnow()
    non_terminal = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED]

    overdue_tasks = (
        Task.query
        .join(Project)
        .filter(
            Project.owner_id == user.id,
            Task.due_date.isnot(None),
            Task.due_date < now,
            Task.status.in_(non_terminal),
        )
        .with_entities(
            Task.id, Task.title, Task.priority, Task.due_date,
            Task.project_id, Project.name,
        )
        .all()
    )

    if not overdue_tasks:
        return ApiResponse.success({"clusters": [], "total_overdue": 0}).to_response()

    # Group by (project_id, priority)
    cluster_data: dict = {}  # {(pid, priority): {name, count, overdue_days_sum, titles}}
    total_overdue = 0
    for tid, title, priority, due_date, pid, pname in overdue_tasks:
        p = priority.value if priority else "unknown"
        key = (pid, p)
        if key not in cluster_data:
            cluster_data[key] = {
                "project_id": pid, "project_name": pname or f"Project#{pid}",
                "priority": p, "count": 0, "overdue_days_sum": 0.0, "titles": [],
            }
        cluster_data[key]["count"] += 1
        total_overdue += 1
        days_overdue = (now - due_date).total_seconds() / 86400 if due_date else 0
        cluster_data[key]["overdue_days_sum"] += days_overdue
        if title and len(cluster_data[key]["titles"]) < 3:
            cluster_data[key]["titles"].append(title[:60])

    clusters = []
    for key, d in sorted(cluster_data.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]:
        clusters.append({
            "project_id": d["project_id"],
            "project_name": d["project_name"],
            "priority": d["priority"],
            "count": d["count"],
            "avg_days_overdue": round(d["overdue_days_sum"] / d["count"], 1) if d["count"] else 0,
            "titles": d["titles"],
        })

    return ApiResponse.success({
        "clusters": clusters,
        "total_overdue": total_overdue,
    }).to_response()


@tasks_bp.route('/priority-trend', methods=['GET'])
@unified_auth_required
def task_priority_trend():
    """Daily task priority distribution trend for the current user.

    Groups tasks by created_at date and priority (critical/high/medium/low),
    returning per-day counts per priority level over the last N days.
    Reveals how the task priority mix shifts over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        Task.query
        .filter(Task.owner_id == user.id, Task.created_at >= since)
        .with_entities(
            func.date(Task.created_at).label("d"),
            Task.priority,
            func.count().label("cnt"),
        )
        .group_by(func.date(Task.created_at), Task.priority)
        .order_by(func.date(Task.created_at))
        .all()
    )

    priority_keys = ["critical", "high", "medium", "low"]
    trend: dict = {}  # {date_str: {priority: count}}
    for d, pri, cnt in rows:
        ds = d.isoformat() if d else None
        if not ds:
            continue
        bucket = trend.setdefault(ds, {})
        p = pri.value if hasattr(pri, "value") else str(pri)
        bucket[p] = cnt

    # Build full date range
    date_range = []
    cur = since.date() + timedelta(days=1)
    end = datetime.utcnow().date()
    while cur <= end:
        date_range.append(cur.isoformat())
        cur += timedelta(days=1)

    # Fill gaps
    out = []
    for ds in date_range:
        b = trend.get(ds, {})
        out.append({
            "date": ds,
            "critical": b.get("critical", 0),
            "high": b.get("high", 0),
            "medium": b.get("medium", 0),
            "low": b.get("low", 0),
        })

    totals = {k: sum(d[k] for d in out) for k in priority_keys}

    return ApiResponse.success({
        "days": days,
        "trend": out,
        "totals": totals,
    }).to_response()


@tasks_bp.route("/dependency-chain", methods=["GET"])
@unified_auth_required
def task_dependency_chain():
    """Analyze task dependency chains for the current user.

    Finds tasks with subtask relationships and builds dependency chains.
    Returns per-chain: root task, depth, total tasks, completion progress.

    Query params:
    - project_id: optional project filter
    - limit: max chains returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10
    project_id = request.args.get("project_id", type=int)

    from models.agent import Task
    q = Task.query.filter(Task.owner_id == user.id, Task.parent_id == None)
    if project_id:
        q = q.filter(Task.project_id == project_id)

    root_tasks = q.order_by(Task.created_at.desc()).limit(limit * 3).all()

    chains = []
    for root in root_tasks:
        # BFS to find all descendants
        visited = set()
        queue = [root.id]
        all_ids = [root.id]
        max_depth = 0
        depth_map = {root.id: 0}
        while queue:
            tid = queue.pop(0)
            if tid in visited:
                continue
            visited.add(tid)
            children = Task.query.filter_by(parent_id=tid).all()
            for child in children:
                if child.id not in visited:
                    all_ids.append(child.id)
                    depth_map[child.id] = depth_map[tid] + 1
                    max_depth = max(max_depth, depth_map[child.id])
                    queue.append(child.id)

        if len(all_ids) < 2:
            continue

        # Count completed
        all_tasks = Task.query.filter(Task.id.in_(all_ids)).all()
        completed = sum(1 for t in all_tasks if t.status and t.status.value == "done")
        in_progress = sum(1 for t in all_tasks if t.status and t.status.value == "in_progress")

        chains.append({
            "root_id": root.id,
            "root_title": root.title or f"Task#{root.id}",
            "depth": max_depth,
            "total_tasks": len(all_ids),
            "completed": completed,
            "in_progress": in_progress,
            "progress_pct": round(completed / len(all_ids) * 100, 1) if all_ids else 0.0,
        })

    chains.sort(key=lambda c: c["total_tasks"], reverse=True)
    return ApiResponse.success({"chains": chains[:limit]}).to_response()


@tasks_bp.route("/rework-analysis", methods=["GET"])
@unified_auth_required
def task_rework_analysis():
    """Analyze task rework — tasks reverted from done/review to in_progress/todo.

    Scans TaskHistory for status transitions where old_value is a
    completed state (done/review) and new_value is an active state
    (in_progress/todo). The value-combination filter is robust to
    whether the change was logged as UPDATED or STATUS_CHANGED.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(30, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        days, limit = 30, 15

    since = datetime.utcnow() - timedelta(days=days)

    rework_events = (
        TaskHistory.query
        .filter(
            TaskHistory.changed_at >= since,
            TaskHistory.old_value.in_(["done", "review"]),
            TaskHistory.new_value.in_(["in_progress", "todo"]),
        )
        .all()
    )

    task_rework_count = {}
    for ev in rework_events:
        task_rework_count[ev.task_id] = task_rework_count.get(ev.task_id, 0) + 1

    if not task_rework_count:
        return ApiResponse.success({
            "tasks": [], "by_project": [], "days": days,
            "total_reworked": 0, "total_rework_events": 0,
        }).to_response()

    reworked_task_ids = list(task_rework_count.keys())
    # Scope to the current user's tasks via project ownership (canonical pattern)
    tasks = (
        Task.query.join(Project).filter(
            Task.id.in_(reworked_task_ids), Project.owner_id == user.id
        ).all()
    )
    if not tasks:
        return ApiResponse.success({
            "tasks": [], "by_project": [], "days": days,
            "total_reworked": 0, "total_rework_events": len(rework_events),
        }).to_response()

    project_ids = {t.project_id for t in tasks}
    project_names = {p.id: p.name for p in Project.query.filter(Project.id.in_(project_ids)).all()}

    project_rework = {}
    task_items = []
    for t in tasks:
        cnt = task_rework_count.get(t.id, 0)
        project_rework[t.project_id] = project_rework.get(t.project_id, 0) + cnt
        task_items.append({
            "task_id": t.id,
            "title": (t.title or f"Task#{t.id}")[:60],
            "project_name": project_names.get(t.project_id, f"Project#{t.project_id}"),
            "rework_count": cnt,
        })

    task_items.sort(key=lambda x: x["rework_count"], reverse=True)
    project_items = sorted(
        [{"project_name": project_names.get(pid, f"Project#{pid}"), "rework_count": cnt}
         for pid, cnt in project_rework.items()],
        key=lambda x: x["rework_count"], reverse=True,
    )

    return ApiResponse.success({
        "tasks": task_items[:limit],
        "by_project": project_items[:10],
        "days": days,
        "total_reworked": len(tasks),
        "total_rework_events": len(rework_events),
    }).to_response()