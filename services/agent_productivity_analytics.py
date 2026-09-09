"""Agent 生产力分析服务（概览 / 趋势 / 告警 / 分组 / 热力图 / 周对比 / 闲置排行）。

从 api/agents/productivity.py 下沉的计算逻辑：路由层只做参数解析与鉴权，
本模块以 owner_id 为入参。此前概览/告警/分组三处的分桶聚合是同一段代码
抄了三遍，这里统一到 _aggregate_assignments / _bucket_by_state。

归一化约定：所有函数返回「路由层直接放进 ApiResponse.success 的 dict」。
"""

from datetime import datetime, timedelta

from sqlalchemy import func

from models import (
    Agent,
    TaskAssignment,
    TaskAssignmentState,
)

_EMPTY_BUCKET = {"total": 0, "done": 0, "failed": 0,
                 "cancelled": 0, "expired": 0, "in_progress": 0}


def _owner_agent_ids(owner_id):
    return [a.id for a in Agent.query.filter_by(owner_id=owner_id)
            .with_entities(Agent.id).all()]


def _assignment_rows(agent_ids, since):
    return (
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


def _bucket_by_state(bucket, state, claimed_at, completed_at, durations_key,
                    durations):
    """按状态累加一个桶；done 且双时间戳有效时累计完成时长（小时）。"""
    bucket["total"] += 1
    s = state.value if state else None
    if s == "done":
        bucket["done"] += 1
        if claimed_at and completed_at and completed_at > claimed_at:
            durations.setdefault(durations_key, []).append(
                (completed_at - claimed_at).total_seconds() / 3600)
    elif s == "failed":
        bucket["failed"] += 1
    elif s == "cancelled":
        bucket["cancelled"] += 1
    elif s == "expired":
        bucket["expired"] += 1
    else:
        bucket["in_progress"] += 1


def _aggregate_assignments(agent_ids, since):
    """按 Agent 聚合：返回 (agg, durations) 两个 dict。"""
    agg: dict = {}
    durations: dict = {}
    for aid, state, claimed_at, completed_at in _assignment_rows(agent_ids, since):
        bucket = agg.setdefault(aid, {"agent_id": aid, **_EMPTY_BUCKET})
        _bucket_by_state(bucket, state, claimed_at, completed_at, aid, durations)
    return agg, durations


def _agent_name_map(agent_ids):
    if not agent_ids:
        return {}
    return {a.id: a.name for a in Agent.query.filter(
        Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()}


def _avg_hours(durations, key):
    ds = durations.get(key, [])
    return round(sum(ds) / len(ds), 2) if ds else None


def productivity_summary(owner_id, days, limit):
    """每个 Agent 的任务分派产出统计（按完成数降序，截前 limit 个）。"""
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"days": days, "items": []}

    agg, durations = _aggregate_assignments(agent_ids, since)
    name_map = _agent_name_map(list(agg.keys()))

    items = []
    for aid, b in agg.items():
        total, done = b["total"], b["done"]
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
            "avg_completion_hours": _avg_hours(durations, aid),
        })
    items.sort(key=lambda x: x["done"], reverse=True)
    return {"days": days, "items": items[:limit]}


def productivity_trend(owner_id, days):
    """按日完成/失败趋势（含按 Agent kind 分层）。"""
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"days": days, "trend": [], "total_done": 0,
                "total_failed": 0, "by_kind_totals": {}}

    kind_map: dict = {}
    for aid, kind in (
        Agent.query
        .filter(Agent.id.in_(agent_ids))
        .with_entities(Agent.id, Agent.kind)
        .all()
    ):
        kind_map[aid] = kind.value if kind else "unknown"

    rows = (
        TaskAssignment.query
        .filter(
            TaskAssignment.agent_id.in_(agent_ids),
            TaskAssignment.created_at >= since,
            TaskAssignment.state.in_([TaskAssignmentState.DONE,
                                      TaskAssignmentState.FAILED]),
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
        bucket = by_day.setdefault(key, {"date": key, "done": 0, "failed": 0,
                                         "by_kind": {}})
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
    by_kind_totals_sorted = dict(sorted(by_kind_totals.items(),
                                        key=lambda kv: kv[1]["done"], reverse=True))
    return {"days": days, "trend": trend, "total_done": total_done,
            "total_failed": total_failed, "by_kind_totals": by_kind_totals_sorted}


def productivity_alerts(owner_id, days, min_completion_rate, max_failure_rate,
                        min_assignments):
    """低效 Agent 告警（完成率过低或失败率过高，且派单数达标）。"""
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"days": days, "items": []}

    agg, durations = _aggregate_assignments(agent_ids, since)
    name_map = _agent_name_map(list(agg.keys()))

    items = []
    for aid, b in agg.items():
        total = b["total"]
        if total < min_assignments:
            continue
        done, failed = b["done"], b["failed"]
        completion_rate = round(done / total * 100, 1) if total else 0
        failure_rate = round(failed / total * 100, 1) if total else 0
        reasons = []
        if completion_rate < min_completion_rate:
            reasons.append(f"完成率 {completion_rate}% < {min_completion_rate}%")
        if failure_rate > max_failure_rate:
            reasons.append(f"失败率 {failure_rate}% > {max_failure_rate}%")
        if not reasons:
            continue
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
            "avg_completion_hours": _avg_hours(durations, aid),
            "reasons": reasons,
        })
    # 最差优先：按完成率升序、失败率降序
    items.sort(key=lambda x: (x["completion_rate"], -x["failure_rate"]))
    return {"days": days, "min_completion_rate": min_completion_rate,
            "max_failure_rate": max_failure_rate,
            "min_assignments": min_assignments, "items": items}


def productivity_by_kind(owner_id, days):
    """按 Agent kind 分组的产出对比。"""
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"days": days, "items": []}

    kind_map = {
        aid: (k.value if k else "unknown")
        for aid, k in Agent.query.filter(Agent.id.in_(agent_ids))
        .with_entities(Agent.id, Agent.kind).all()
    }

    agg: dict = {}
    durations: dict = {}
    agents_seen: dict = {}
    for aid, state, claimed_at, completed_at in _assignment_rows(agent_ids, since):
        kind = kind_map.get(aid, "unknown")
        bucket = agg.setdefault(kind, {"kind": kind, **_EMPTY_BUCKET})
        agents_seen.setdefault(kind, set()).add(aid)
        _bucket_by_state(bucket, state, claimed_at, completed_at, kind, durations)

    items = []
    for kind, b in agg.items():
        total = b["total"]
        items.append({
            "kind": kind,
            "agent_count": len(agents_seen.get(kind, set())),
            **{k: b[k] for k in ("total", "done", "failed", "cancelled",
                                 "expired", "in_progress")},
            "completion_rate": round(b["done"] / total * 100, 1) if total else 0,
            "failure_rate": round(b["failed"] / total * 100, 1) if total else 0,
            "avg_completion_hours": _avg_hours(durations, kind),
        })
    items.sort(key=lambda x: (-x["completion_rate"], x["failure_rate"]))
    return {"days": days, "items": items}


def _done_completion_rows(agent_ids, since):
    return (
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


def productivity_hourly_heatmap(owner_id, days, limit):
    """小时 × Agent 完成热力图（Python 侧取小时，跨库兼容）。"""
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"days": days, "agents": [], "matrix": {},
                "max_cell": 0, "peak_hour": None}

    name_map = _agent_name_map(agent_ids)
    matrix: dict = {}
    totals: dict = {}
    hour_totals = [0] * 24
    max_cell = 0
    for aid, completed_at in _done_completion_rows(agent_ids, since):
        h = completed_at.hour
        bucket = matrix.setdefault(aid, {})
        bucket[h] = bucket.get(h, 0) + 1
        if bucket[h] > max_cell:
            max_cell = bucket[h]
        totals[aid] = totals.get(aid, 0) + 1
        hour_totals[h] += 1

    top_agents = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    peak_hour = max(range(24), key=lambda h: hour_totals[h]) if any(hour_totals) else None
    agents_out = [{"agent_id": aid,
                   "name": name_map.get(aid, f"Agent#{aid}"),
                   "done": totals.get(aid, 0)}
                  for aid, _ in top_agents]
    matrix_out = {str(aid): matrix.get(aid, {}) for aid, _ in top_agents}
    return {"days": days, "agents": agents_out, "matrix": matrix_out,
            "hour_totals": hour_totals, "max_cell": max_cell,
            "peak_hour": peak_hour}


def productivity_calendar_heatmap(owner_id, days, limit):
    """日期 × Agent 完成日历热力图（GitHub 风格）。"""
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"days": days, "agents": [], "matrix": {},
                "max_cell": 0, "date_range": []}

    name_map = _agent_name_map(agent_ids)
    matrix: dict = {}
    totals: dict = {}
    max_cell = 0
    for aid, completed_at in _done_completion_rows(agent_ids, since):
        ds = completed_at.strftime("%Y-%m-%d")
        bucket = matrix.setdefault(aid, {})
        bucket[ds] = bucket.get(ds, 0) + 1
        if bucket[ds] > max_cell:
            max_cell = bucket[ds]
        totals[aid] = totals.get(aid, 0) + 1

    top_agents = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    agents_out = [{"agent_id": aid,
                   "name": name_map.get(aid, f"Agent#{aid}"),
                   "done": totals.get(aid, 0)}
                  for aid, _ in top_agents]
    matrix_out = {str(aid): matrix.get(aid, {}) for aid, _ in top_agents}

    date_range = []
    d = since.date() + timedelta(days=1)
    end = datetime.utcnow().date()
    while d <= end:
        date_range.append(d.isoformat())
        d += timedelta(days=1)

    return {"days": days, "agents": agents_out, "matrix": matrix_out,
            "max_cell": max_cell, "date_range": date_range}


def productivity_weekly_comparison(owner_id, limit):
    """周对比：每 Agent 本周/上周完成数与变化百分比。"""
    agent_ids = _owner_agent_ids(owner_id)
    if not agent_ids:
        return {"agents": [], "total_this_week": 0, "total_last_week": 0}

    name_map = _agent_name_map(agent_ids)

    now = datetime.utcnow()
    current_week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    prev_week_start = current_week_start - timedelta(weeks=1)

    rows = _done_completion_rows(agent_ids, prev_week_start)

    agent_week: dict = {}
    total_this = 0
    total_last = 0
    for aid, completed_at in rows:
        wk = agent_week.setdefault(aid, {"this_week": 0, "last_week": 0})
        if completed_at >= current_week_start:
            wk["this_week"] += 1
            total_this += 1
        else:
            wk["last_week"] += 1
            total_last += 1

    agents_out = []
    for aid, wk in sorted(agent_week.items(),
                          key=lambda kv: kv[1]["this_week"], reverse=True)[:limit]:
        this_w, last_w = wk["this_week"], wk["last_week"]
        change = (round((this_w - last_w) / last_w * 100, 1) if last_w > 0
                  else (100.0 if this_w > 0 else 0.0))
        agents_out.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"Agent#{aid}"),
            "this_week": this_w,
            "last_week": last_w,
            "change_pct": change,
        })
    return {"agents": agents_out, "total_this_week": total_this,
            "total_last_week": total_last}


def idle_ranking(owner_id, limit):
    """按闲置时长排序（active/idle/stale/dormant/never 五档）。"""
    rows = (
        TaskAssignment.query
        .join(Agent, TaskAssignment.agent_id == Agent.id)
        .filter(Agent.owner_id == owner_id)
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
        .filter_by(owner_id=owner_id)
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

    results.sort(key=lambda r: -(r["idle_hours"]
                                 if r["idle_hours"] is not None else float("inf")))

    stage_counts = {}
    for r in results:
        stage_counts[r["stage"]] = stage_counts.get(r["stage"], 0) + 1
    return {"agents": results[:limit], "total_agents": len(results),
            "stage_counts": stage_counts}
