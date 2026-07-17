"""
Dashboard metrics and agent monitoring endpoints.
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
    WorkflowStatus,
    Project,
    CrossProjectAgent,
    AgentConflict,
    SandboxViolation,
    get_request_args,
    paginate_query,
    parse_enum,
    ACTIVE_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
)

@agents_bp.route("/dashboard/metrics", methods=["GET"])
@unified_auth_required
def collaboration_metrics():
    """Aggregate collaboration metrics for the dashboard.

    Query params:
      project_id  – scope to a single project (optional)
      days        – look-back window in days (default 7)
    """
    try:
        user = get_current_user()
        project_id = request.args.get("project_id", type=int)
        days = request.args.get("days", 7, type=int)
        since = datetime.utcnow() - timedelta(days=max(1, min(days, 90)))

        # --- Base filters ---
        task_q = Task.query.filter(Task.created_at >= since)
        if project_id:
            task_q = task_q.filter_by(project_id=project_id)

        # --- Task metrics ---
        total_tasks = task_q.count()
        done_tasks = task_q.filter(Task.status == TaskStatus.DONE).count()
        failed_tasks = task_q.filter(
            Task.status.in_([TaskStatus.CANCELLED])
        ).count()
        in_progress = task_q.filter(Task.status == TaskStatus.IN_PROGRESS).count()
        blocked = task_q.filter(Task.status == TaskStatus.BLOCKED).count()
        review = task_q.filter(Task.status == TaskStatus.REVIEW).count()

        # Average completion time for tasks finished in window
        completed_in_window = task_q.filter(
            Task.status == TaskStatus.DONE,
            Task.updated_at >= since,
        ).all()
        completion_times = []
        for t in completed_in_window:
            if t.created_at and t.updated_at:
                delta = (t.updated_at - t.created_at).total_seconds()
                if delta > 0:
                    completion_times.append(delta)
        avg_completion_seconds = (
            sum(completion_times) / len(completion_times)
            if completion_times
            else 0
        )

        # --- Agent metrics ---
        agent_q = Agent.query
        total_agents = agent_q.count()
        active_agents = agent_q.filter_by(status=AgentStatus.ACTIVE).count()
        paused_agents = agent_q.filter_by(status=AgentStatus.PAUSED).count()
        offline_agents = agent_q.filter_by(status=AgentStatus.OFFLINE).count()

        # Agent utilization: agents with at least one running assignment
        agents_with_running = (
            db.session.query(TaskAssignment.agent_id)
            .filter(
                TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                TaskAssignment.agent_id.isnot(None),
            )
            .distinct()
            .count()
        )
        agent_utilization = (
            round(agents_with_running / active_agents * 100, 1) if active_agents else 0
        )

        # --- Assignment metrics ---
        assignment_q = TaskAssignment.query.filter(TaskAssignment.created_at >= since)
        total_assignments = assignment_q.count()
        done_assignments = assignment_q.filter(
            TaskAssignment.state == TaskAssignmentState.DONE
        ).count()
        failed_assignments = assignment_q.filter(
            TaskAssignment.state == TaskAssignmentState.FAILED
        ).count()

        # --- Workflow metrics ---
        wf_run_q = WorkflowRun.query.filter(WorkflowRun.created_at >= since)
        if project_id:
            wf_run_q = wf_run_q.filter_by(project_id=project_id)
        total_wf_runs = wf_run_q.count()
        succeeded_wf_runs = wf_run_q.filter(
            WorkflowRun.status == WorkflowStatus.SUCCEEDED
        ).count()
        failed_wf_runs = wf_run_q.filter(
            WorkflowRun.status == WorkflowStatus.FAILED
        ).count()
        running_wf_runs = wf_run_q.filter(
            WorkflowRun.status == WorkflowStatus.RUNNING
        ).count()
        wf_success_rate = (
            round(succeeded_wf_runs / total_wf_runs * 100, 1) if total_wf_runs else 0
        )

        # --- Handoff metrics ---
        handoff_count = (
            AuditLog.query.filter(
                AuditLog.action == "task.handoff",
                AuditLog.created_at >= since,
            )
            .count()
        )

        # --- Task creation trend (daily buckets) ---
        from sqlalchemy import func as sa_func
        daily_tasks = (
            db.session.query(
                sa_func.date(Task.created_at).label("date"),
                sa_func.count(Task.id).label("created"),
            )
            .filter(Task.created_at >= since)
            .group_by(sa_func.date(Task.created_at))
            .order_by(sa_func.date(Task.created_at))
            .all()
        )
        daily_done = (
            db.session.query(
                sa_func.date(Task.updated_at).label("date"),
                sa_func.count(Task.id).label("completed"),
            )
            .filter(
                Task.status == TaskStatus.DONE,
                Task.updated_at >= since,
            )
            .group_by(sa_func.date(Task.updated_at))
            .order_by(sa_func.date(Task.updated_at))
            .all()
        )
        # Merge into a single dict
        trend_map: dict = {}
        for d, c in daily_tasks:
            trend_map[str(d)] = {"date": str(d), "created": c, "completed": 0}
        for d, c in daily_done:
            key = str(d)
            if key in trend_map:
                trend_map[key]["completed"] = c
            else:
                trend_map[key] = {"date": key, "created": 0, "completed": c}
        trend = sorted(trend_map.values(), key=lambda x: x["date"])

        # --- Agent kind distribution ---
        kind_dist = (
            db.session.query(Agent.kind, sa_func.count(Agent.id))
            .group_by(Agent.kind)
            .all()
        )
        agent_kind_distribution = {str(k): c for k, c in kind_dist}

        # --- Top agents by completed tasks ---
        top_agents_q = (
            db.session.query(
                Agent.id, Agent.name, Agent.kind,
                sa_func.count(TaskAssignment.id).label("completed_count"),
            )
            .join(TaskAssignment, TaskAssignment.agent_id == Agent.id)
            .filter(
                TaskAssignment.state == TaskAssignmentState.DONE,
                TaskAssignment.created_at >= since,
            )
            .group_by(Agent.id, Agent.name, Agent.kind)
            .order_by(sa_func.count(TaskAssignment.id).desc())
            .limit(10)
            .all()
        )
        top_agents = [
            {"id": a_id, "name": name, "kind": str(kind), "completed_count": cnt}
            for a_id, name, kind, cnt in top_agents_q
        ]

        # --- Agent performance details ---
        agent_perf_q = (
            db.session.query(
                Agent.id, Agent.name,
                sa_func.count(TaskAssignment.id).label("total_assignments"),
                sa_func.sum(
                    case(
                        (TaskAssignment.state == TaskAssignmentState.DONE, 1),
                        else_=0,
                    )
                ).label("done_count"),
                sa_func.sum(
                    case(
                        (TaskAssignment.state == TaskAssignmentState.FAILED, 1),
                        else_=0,
                    )
                ).label("failed_count"),
            )
            .join(TaskAssignment, TaskAssignment.agent_id == Agent.id)
            .filter(
                Agent.owner_id == user.id,
                TaskAssignment.created_at >= since,
            )
            .group_by(Agent.id, Agent.name)
            .all()
        )
        agent_performance = []
        for a_id, a_name, total_a, done_a, failed_a in agent_perf_q:
            success_rate = round((done_a / total_a * 100), 1) if total_a else 0
            agent_performance.append({
                "id": a_id,
                "name": a_name,
                "total_assignments": total_a,
                "done": done_a,
                "failed": failed_a,
                "success_rate": success_rate,
            })

        return ApiResponse.success(
            {
                "window_days": days,
                "project_id": project_id,
                "tasks": {
                    "total": total_tasks,
                    "done": done_tasks,
                    "failed": failed_tasks,
                    "in_progress": in_progress,
                    "blocked": blocked,
                    "review": review,
                    "completion_rate": round(done_tasks / total_tasks * 100, 1) if total_tasks else 0,
                    "avg_completion_seconds": round(avg_completion_seconds, 1),
                },
                "agents": {
                    "total": total_agents,
                    "active": active_agents,
                    "paused": paused_agents,
                    "offline": offline_agents,
                    "utilization_pct": agent_utilization,
                    "kind_distribution": agent_kind_distribution,
                },
                "assignments": {
                    "total": total_assignments,
                    "done": done_assignments,
                    "failed": failed_assignments,
                },
                "workflows": {
                    "total_runs": total_wf_runs,
                    "succeeded": succeeded_wf_runs,
                    "failed": failed_wf_runs,
                    "running": running_wf_runs,
                    "success_rate": wf_success_rate,
                },
                "handoffs": handoff_count,
                "trend": trend,
                "top_agents": top_agents,
                "agent_performance": agent_performance,
            },
            "Collaboration metrics",
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to compute metrics: {str(e)}", 500).to_response()


@agents_bp.route("/dashboard/agent-monitor", methods=["GET"])
@unified_auth_required
def agent_monitor():
    """Real-time Agent status monitoring with historical trends.

    Returns per-agent status, current workload, recent activity, and
    hourly activity counts for sparkline-style trend charts.

    Query params:
      project_id – scope to a single project (optional)
      hours      – look-back window for activity trend (default 24)
    """
    try:
        user = get_current_user()
        project_id = request.args.get("project_id", type=int)
        hours = request.args.get("hours", 24, type=int)
        since = datetime.utcnow() - timedelta(hours=max(1, min(hours, 168)))

        # Get all user's agents
        agent_q = Agent.query.filter_by(owner_id=user.id)
        agents = agent_q.all()

        AGENT_OFFLINE_AFTER_SECONDS = 30 * 60
        now = datetime.utcnow()

        monitor_data = []
        for agent in agents:
            # Determine real-time status
            if agent.status == AgentStatus.ACTIVE and agent.last_seen_at:
                elapsed = (now - agent.last_seen_at).total_seconds()
                real_status = "offline" if elapsed > AGENT_OFFLINE_AFTER_SECONDS else "active"
            else:
                real_status = agent.status.value if agent.status else "unknown"

            # Current workload
            active_assignments = TaskAssignment.query.filter(
                TaskAssignment.agent_id == agent.id,
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
            ).all()

            current_tasks = []
            for a in active_assignments[:5]:
                task_info = {"id": a.task_id, "state": a.state.value}
                if a.task:
                    task_info["title"] = a.task.title[:60]
                    task_info["status"] = a.task.status.value if a.task.status else None
                current_tasks.append(task_info)

            # Reputation
            rep = AgentReputation.query.filter_by(agent_id=agent.id).first()
            rep_data = rep.to_dict() if rep else None

            # Recent experience count
            exp_count = AgentExperience.query.filter_by(
                agent_id=agent.id, is_valid=True,
            ).count()
            shared_exp_count = AgentExperience.query.filter_by(
                agent_id=agent.id, is_shared=True, is_valid=True,
            ).count()

            # Hourly activity trend (assignments created/completed per hour)
            from sqlalchemy import func as sa_func
            hourly_activity = (
                db.session.query(
                    sa_func.strftime("%Y-%m-%d %H:00", TaskAssignment.created_at).label("hour"),
                    sa_func.count(TaskAssignment.id).label("count"),
                )
                .filter(
                    TaskAssignment.agent_id == agent.id,
                    TaskAssignment.created_at >= since,
                )
                .group_by(sa_func.strftime("%Y-%m-%d %H:00", TaskAssignment.created_at))
                .order_by("hour")
                .all()
            )

            # SQLite compatibility: try strfttime, fall back to date_trunc for PostgreSQL
            if not hourly_activity:
                try:
                    hourly_activity = (
                        db.session.query(
                            sa_func.date_trunc("hour", TaskAssignment.created_at).label("hour"),
                            sa_func.count(TaskAssignment.id).label("count"),
                        )
                        .filter(
                            TaskAssignment.agent_id == agent.id,
                            TaskAssignment.created_at >= since,
                        )
                        .group_by(sa_func.date_trunc("hour", TaskAssignment.created_at))
                        .order_by("hour")
                        .all()
                    )
                except Exception:
                    hourly_activity = []

            trend = [{"hour": str(h), "count": c} for h, c in hourly_activity]

            # Cross-project access
            cross_projects = CrossProjectAgent.get_active_for_agent(agent.id)

            monitor_data.append({
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_kind": agent.kind.value if agent.kind else None,
                "real_status": real_status,
                "collaboration_role": agent.collaboration_role or "standalone",
                "capabilities": agent.capabilities or [],
                "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                "active_task_count": len(active_assignments),
                "current_tasks": current_tasks,
                "reputation": rep_data,
                "experience_count": exp_count,
                "shared_experience_count": shared_exp_count,
                "cross_project_count": len(cross_projects),
                "activity_trend": trend,
            })

        # Summary stats
        active_count = sum(1 for a in monitor_data if a["real_status"] == "active")
        offline_count = sum(1 for a in monitor_data if a["real_status"] == "offline")
        total_active_tasks = sum(a["active_task_count"] for a in monitor_data)

        return ApiResponse.success({
            "agents": monitor_data,
            "summary": {
                "total_agents": len(monitor_data),
                "active": active_count,
                "offline": offline_count,
                "other": len(monitor_data) - active_count - offline_count,
                "total_active_tasks": total_active_tasks,
                "window_hours": hours,
            },
        }, "Agent monitor data").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to get monitor data: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Workflow Triggers (CRON / Scheduled)
# ---------------------------------------------------------------------------


def _compute_next_fire(cron_expr: str, now: datetime) -> datetime | None:
    """Simple cron next-fire-time calculator.

    Supports the 5-field format: minute hour day-of-month month day-of-week.
    Uses a brute-force scan forward (max 366 days).
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return None

        def _parse_field(field: str, offset: int, size: int) -> set[int]:
            result = set()
            for part in field.split(","):
                if part == "*":
                    result.update(range(offset, offset + size))
                elif "/" in part:
                    base, step = part.split("/", 1)
                    start = offset if base == "*" else int(base)
                    step = int(step)
                    for v in range(start, offset + size, step):
                        result.add(v)
                elif "-" in part:
                    a, b = part.split("-", 1)
                    result.update(range(int(a), int(b) + 1))
                else:
                    result.add(int(part))
            return result

        minutes = _parse_field(parts[0], 0, 60)
        hours = _parse_field(parts[1], 0, 24)
        doms = _parse_field(parts[2], 1, 31)
        months = _parse_field(parts[3], 1, 12)
        dows = _parse_field(parts[4], 0, 7)  # 0=Sun, 7=Sun
        # Normalize Sunday
        if 7 in dows:
            dows.add(0)
            dows.discard(7)

        candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = now + timedelta(days=366)
        while candidate <= end:
            if (candidate.minute in minutes
                    and candidate.hour in hours
                    and candidate.day in doms
                    and candidate.month in months
                    and candidate.weekday() in {(d + 6) % 7 for d in dows}):  # Mon=0..Sun=6
                return candidate
            candidate += timedelta(minutes=1)
        return None
    except Exception:
        return None

