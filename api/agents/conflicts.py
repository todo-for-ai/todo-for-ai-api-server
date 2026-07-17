"""
Agent conflict detection, resolution, and analytics endpoints.
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
    AuditLog,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    AgentConflict,
    ConflictType,
    ConflictSeverity,
    ConflictStatus,
    ConflictResolutionStrategy,
    AgentReputation,
    CollaborationProtocol,
    ProtocolMessage,
    ProtocolStatus,
    SandboxViolation,
    ACTIVE_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    parse_enum,
    get_request_args,
    paginate_query,
)

@agents_bp.route("/conflicts/sandbox-correlation", methods=["GET"])
@unified_auth_required
def conflicts_sandbox_correlation():
    """Cross-dimension correlation between Agent conflicts and sandbox
    violations.

    For each conflict (AgentConflict, created_at within window), checks
    whether a sandbox violation (SandboxViolation, blocked_at within
    ±window_hours, same Agent among conflict parties) occurred. Reports
    co-occurrence rate, breakdown by conflict_type, and the top agents
    whose conflicts most often coincide with sandbox violations. Reveals
    whether coordination breakdowns cluster with sandbox escape attempts.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        window_hours = max(0, min(168, int(request.args.get("window_hours", 2))))
    except (TypeError, ValueError):
        days = 30
        window_hours = 2

    since = datetime.utcnow() - timedelta(days=days)
    conflicts = (
        AgentConflict.query
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.created_at >= since,
        )
        .with_entities(
            AgentConflict.id, AgentConflict.conflict_type,
            AgentConflict.created_at, AgentConflict.agent_ids,
        )
        .all()
    )

    total_conflicts = len(conflicts)
    if total_conflicts == 0:
        return ApiResponse.success({
            "days": days,
            "window_hours": window_hours,
            "total_conflicts": 0,
            "with_violation": 0,
            "violation_rate": 0,
            "by_conflict_type": {},
            "top_agents": [],
        }).to_response()

    # Collect all agent ids involved across conflicts for violation prefetch
    involved_ids = set()
    for _id, ctype, created_at, agent_ids_json in conflicts:
        if agent_ids_json:
            for aid in agent_ids_json:
                involved_ids.add(aid)

    violations = (
        SandboxViolation.query
        .filter(
            SandboxViolation.agent_id.in_(list(involved_ids)),
            SandboxViolation.blocked_at >= since - timedelta(hours=window_hours),
        )
        .with_entities(SandboxViolation.agent_id, SandboxViolation.blocked_at)
        .all()
    ) if involved_ids else []
    violations_by_agent: dict = {}
    for aid, blocked_at in violations:
        violations_by_agent.setdefault(aid, []).append(blocked_at)

    with_violation = 0
    by_type_total: dict = {}
    by_type_with_violation: dict = {}
    per_agent: dict = {}
    for _id, ctype, created_at, agent_ids_json in conflicts:
        ct = ctype.value if ctype else "(未知)"
        by_type_total[ct] = by_type_total.get(ct, 0) + 1
        has_v = False
        for aid in (agent_ids_json or []):
            v_times = violations_by_agent.get(aid, [])
            if v_times and any(abs((t - created_at).total_seconds()) <= window_hours * 3600 for t in v_times):
                has_v = True
                break
        if has_v:
            with_violation += 1
            by_type_with_violation[ct] = by_type_with_violation.get(ct, 0) + 1
            for aid in (agent_ids_json or []):
                b = per_agent.setdefault(aid, {"agent_id": aid, "conflicts": 0, "with_violation": 0})
                b["conflicts"] += 1
                b["with_violation"] += 1
        else:
            for aid in (agent_ids_json or []):
                b = per_agent.setdefault(aid, {"agent_id": aid, "conflicts": 0, "with_violation": 0})
                b["conflicts"] += 1

    by_conflict_type = {
        ct: {
            "total": by_type_total.get(ct, 0),
            "with_violation": by_type_with_violation.get(ct, 0),
            "rate": round(by_type_with_violation.get(ct, 0) / by_type_total.get(ct, 0) * 100, 1) if by_type_total.get(ct, 0) else 0,
        }
        for ct in by_type_total
    }

    top_ids = sorted(per_agent.keys(), key=lambda k: per_agent[k]["with_violation"], reverse=True)[:8]
    name_map = {a.id: a.name for a in Agent.query.filter(Agent.id.in_(top_ids)).with_entities(Agent.id, Agent.name).all()} if top_ids else {}
    top_agents = [
        {
            "agent_id": aid,
            "name": name_map.get(aid, f"#{aid}"),
            "conflicts": per_agent[aid]["conflicts"],
            "with_violation": per_agent[aid]["with_violation"],
        }
        for aid in top_ids
    ]

    return ApiResponse.success({
        "days": days,
        "window_hours": window_hours,
        "total_conflicts": total_conflicts,
        "with_violation": with_violation,
        "violation_rate": round(with_violation / total_conflicts * 100, 1),
        "by_conflict_type": by_conflict_type,
        "top_agents": top_agents,
    }).to_response()
# ---------------------------------------------------------------------------
# Increment 89: Agent collaboration conflict detection & resolution
# ---------------------------------------------------------------------------

# How long a protocol may stay open without resolution before flagging deadlock.
_PROTOCOL_DEADLOCK_HOURS = 48


def _detect_duplicate_claims(user, now):
    """Detect tasks with more than one active assignment (duplicate claims)."""
    conflicts = []
    # Find task_ids with >1 active assignment
    from sqlalchemy import func
    dupes = (
        db.session.query(TaskAssignment.task_id, func.count(TaskAssignment.id).label("cnt"))
        .join(Task, TaskAssignment.task_id == Task.id)
        .filter(
            Task.creator_id == user.id,
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
        )
        .group_by(TaskAssignment.task_id)
        .having(func.count(TaskAssignment.id) > 1)
        .all()
    )
    for task_id, cnt in dupes:
        assignments = TaskAssignment.query.filter_by(task_id=task_id).filter(
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES)
        ).order_by(TaskAssignment.created_at.asc()).all()
        agent_ids = list({a.agent_id for a in assignments if a.agent_id})
        # Avoid duplicate conflict records for the same task
        existing = AgentConflict.query.filter_by(
            owner_id=user.id, conflict_type=ConflictType.DUPLICATE_CLAIM,
            task_id=task_id, status=ConflictStatus.DETECTED,
        ).first()
        if existing:
            continue
        # Suggest highest reputation wins
        winner = None
        best_score = -1
        for aid in agent_ids:
            rep = AgentReputation.query.filter_by(agent_id=aid).first()
            score = rep.score if rep and rep.score else 50
            if score > best_score:
                best_score = score
                winner = aid
        conflicts.append(AgentConflict(
            owner_id=user.id,
            conflict_type=ConflictType.DUPLICATE_CLAIM,
            severity=ConflictSeverity.CRITICAL,
            status=ConflictStatus.DETECTED,
            task_id=task_id,
            agent_ids=agent_ids,
            title=f"重复认领: 任务 #{task_id} 有 {cnt} 个活跃分配",
            description=f"任务 #{task_id} 同时被 {cnt} 个 Agent 活跃分配，可能导致重复执行。",
            evidence={"assignment_count": cnt, "assignments": [
                {"assignment_id": a.id, "agent_id": a.agent_id, "state": a.state.value if a.state else None, "created_at": a.created_at.isoformat() if a.created_at else None}
                for a in assignments
            ]},
            suggested_strategy=ConflictResolutionStrategy.HIGHEST_REPUTATION,
        ))
    return conflicts


def _detect_assignment_stale(user, now):
    """Detect assignments whose lease has expired but state is still active."""
    conflicts = []
    stale = (
        TaskAssignment.query
        .join(Task, TaskAssignment.task_id == Task.id)
        .filter(
            Task.creator_id == user.id,
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
            TaskAssignment.lease_expires_at.isnot(None),
            TaskAssignment.lease_expires_at < now,
        )
        .all()
    )
    for a in stale:
        existing = AgentConflict.query.filter_by(
            owner_id=user.id, conflict_type=ConflictType.ASSIGNMENT_STALE,
            task_id=a.task_id, status=ConflictStatus.DETECTED,
        ).first()
        if existing:
            continue
        conflicts.append(AgentConflict(
            owner_id=user.id,
            conflict_type=ConflictType.ASSIGNMENT_STALE,
            severity=ConflictSeverity.WARNING,
            status=ConflictStatus.DETECTED,
            task_id=a.task_id,
            agent_ids=[a.agent_id] if a.agent_id else [],
            title=f"过期分配: 任务 #{a.task_id} 分配 #{a.id}",
            description=f"分配 #{a.id} 的租约已于 {a.lease_expires_at.isoformat() if a.lease_expires_at else '?'} 过期，但状态仍为 {a.state.value if a.state else '?'}。",
            evidence={"assignment_id": a.id, "expired_at": a.lease_expires_at.isoformat() if a.lease_expires_at else None},
            suggested_strategy=ConflictResolutionStrategy.AUTO_RETRY,
        ))
    return conflicts


def _detect_protocol_deadlock(user, now):
    """Detect protocols that have been open too long without resolution."""
    conflicts = []
    cutoff = now - timedelta(hours=_PROTOCOL_DEADLOCK_HOURS)
    stuck = CollaborationProtocol.query.filter(
        CollaborationProtocol.owner_id == user.id,
        CollaborationProtocol.status.in_([ProtocolStatus.OPEN, ProtocolStatus.VOTING]),
        CollaborationProtocol.created_at < cutoff,
    ).all()
    for p in stuck:
        existing = AgentConflict.query.filter_by(
            owner_id=user.id, conflict_type=ConflictType.PROTOCOL_DEADLOCK,
            protocol_id=p.id, status=ConflictStatus.DETECTED,
        ).first()
        if existing:
            continue
        msg_count = ProtocolMessage.query.filter_by(protocol_id=p.id).count()
        conflicts.append(AgentConflict(
            owner_id=user.id,
            conflict_type=ConflictType.PROTOCOL_DEADLOCK,
            severity=ConflictSeverity.WARNING,
            status=ConflictStatus.DETECTED,
            protocol_id=p.id,
            agent_ids=[],
            title=f"协议僵局: 协议 #{p.id} 开放超过 {_PROTOCOL_DEADLOCK_HOURS}h",
            description=f"协议 #{p.id} (类型 {p.protocol_type.value if p.protocol_type else '?'}) 已开放 {((now - p.created_at).total_seconds() / 3600):.1f} 小时仍未决议，共 {msg_count} 条消息。",
            evidence={"protocol_id": p.id, "open_hours": round((now - p.created_at).total_seconds() / 3600, 1), "message_count": msg_count},
            suggested_strategy=ConflictResolutionStrategy.ESCALATE,
        ))
    return conflicts


@agents_bp.route("/conflicts/scan", methods=["POST"])
@unified_auth_required
def scan_conflicts():
    """Run a conflict detection scan for the current user.

    Detects duplicate claims, stale assignments, and protocol deadlocks.
    Creates AgentConflict records for newly-detected issues (skips duplicates).
    """
    user = get_current_user()
    now = datetime.utcnow()
    detected = []
    detected.extend(_detect_duplicate_claims(user, now))
    detected.extend(_detect_assignment_stale(user, now))
    detected.extend(_detect_protocol_deadlock(user, now))
    for c in detected:
        db.session.add(c)
    db.session.commit()
    if detected:
        _queue_sse(user.id, "conflicts_detected", {"count": len(detected)})
        flush_sse_notifications()
        AuditLog.record(
            action="conflicts.scan",
            resource_type="system",
            resource_id=0,
            actor_type="human",
            actor_user_id=user.id,
            detail={"detected": len(detected), "types": [c.conflict_type.value for c in detected]},
            ip_address=_client_ip(),
        )
    return ApiResponse.success({
        "detected": len(detected),
        "conflicts": [c.to_dict() for c in detected],
    }, f"Scan complete: {len(detected)} new conflict(s) detected").to_response()


@agents_bp.route("/conflicts", methods=["GET"])
@unified_auth_required
def list_conflicts():
    """List conflicts for the current user, optionally filtered."""
    user = get_current_user()
    q = AgentConflict.query.filter_by(owner_id=user.id)
    status_filter = request.args.get("status")
    if status_filter:
        try:
            q = q.filter_by(status=ConflictStatus(status_filter))
        except ValueError:
            pass
    type_filter = request.args.get("type")
    if type_filter:
        try:
            q = q.filter_by(conflict_type=ConflictType(type_filter))
        except ValueError:
            pass
    active_only = request.args.get("active_only", "true").lower() == "true"
    if active_only and not status_filter:
        q = q.filter(AgentConflict.status.in_([
            ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING
        ]))
    q = q.order_by(AgentConflict.created_at.desc())
    page, per_page, pag = paginate_query(q, default_per_page=20)
    items = [c.to_dict() for c in pag.items]
    return ApiResponse.success({"items": items, **page}).to_response()


@agents_bp.route("/conflicts/<int:conflict_id>", methods=["GET"])
@unified_auth_required
def get_conflict(conflict_id):
    """Get a conflict by ID."""
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    return ApiResponse.success({"conflict": c.to_dict()}).to_response()


@agents_bp.route("/conflicts/<int:conflict_id>/acknowledge", methods=["POST"])
@unified_auth_required
def acknowledge_conflict(conflict_id):
    """Mark a conflict as acknowledged (seen by owner)."""
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    c.status = ConflictStatus.ACKNOWLEDGED
    db.session.commit()
    return ApiResponse.success({"conflict": c.to_dict()}, "Conflict acknowledged").to_response()


@agents_bp.route("/conflicts/<int:conflict_id>/ignore", methods=["POST"])
@unified_auth_required
def ignore_conflict(conflict_id):
    """Dismiss a conflict without action."""
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    c.status = ConflictStatus.IGNORED
    c.resolution = "Dismissed by owner"
    c.resolved_at = datetime.utcnow()
    c.resolved_by_user_id = user.id
    AuditLog.record(
        action="conflict.ignore", resource_type="agent_conflict", resource_id=c.id,
        actor_type="human", actor_user_id=user.id,
        detail={"conflict_type": c.conflict_type.value if c.conflict_type else None},
    )
    db.session.commit()
    return ApiResponse.success({"conflict": c.to_dict()}, "Conflict ignored").to_response()


@agents_bp.route("/conflicts/<int:conflict_id>/resolve", methods=["POST"])
@unified_auth_required
def resolve_conflict(conflict_id):
    """Resolve a conflict with a chosen strategy.

    Body: { strategy, description? }
    For DUPLICATE_CLAIM + FIRST_WINS/HIGHEST_REPUTATION/LEAST_LOADED, this also
    revokes the losing assignments. For ASSIGMENT_STALE + AUTO_RETRY, it cancels
    the stale assignment.
    """
    user = get_current_user()
    c = AgentConflict.query.get(conflict_id)
    if not c or c.owner_id != user.id:
        return ApiResponse.not_found("Conflict not found").to_response()
    body = validate_json_request()
    strategy_str = body.get("strategy")
    try:
        strategy = ConflictResolutionStrategy(strategy_str)
    except ValueError:
        return ApiResponse.error(f"Invalid strategy: {strategy_str}").to_response()
    now = datetime.utcnow()
    actions = []

    # Automated side-effects for specific conflict/strategy combos
    if c.conflict_type == ConflictType.DUPLICATE_CLAIM and c.task_id:
        assignments = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
            TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES)
        ).order_by(TaskAssignment.created_at.asc()).all()
        winner_id = None
        if strategy == ConflictResolutionStrategy.FIRST_WINS and assignments:
            winner_id = assignments[0].id
        elif strategy == ConflictResolutionStrategy.HIGHEST_REPUTATION:
            best = None
            best_score = -1
            for a in assignments:
                rep = AgentReputation.query.filter_by(agent_id=a.agent_id).first()
                score = rep.score if rep and rep.score else 50
                if score > best_score:
                    best_score = score
                    best = a
            winner_id = best.id if best else None
        elif strategy == ConflictResolutionStrategy.LEAST_LOADED:
            best = None
            least = None
            for a in assignments:
                cnt = TaskAssignment.query.filter(
                    TaskAssignment.agent_id == a.agent_id,
                    TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                ).count()
                if least is None or cnt < least:
                    least = cnt
                    best = a
            winner_id = best.id if best else None
        if winner_id is not None:
            for a in assignments:
                if a.id != winner_id:
                    a.state = TaskAssignmentState.CANCELLED
                    a.completed_at = now
                    actions.append(f"cancelled assignment #{a.id} (agent {a.agent_id})")

    elif c.conflict_type == ConflictType.ASSIGNMENT_STALE and c.task_id:
        if strategy in (ConflictResolutionStrategy.AUTO_RETRY, ConflictResolutionStrategy.FIRST_WINS):
            stale_assignments = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
                TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                TaskAssignment.lease_expires_at.isnot(None),
                TaskAssignment.lease_expires_at < now,
            ).all()
            for a in stale_assignments:
                a.state = TaskAssignmentState.EXPIRED
                a.completed_at = now
                actions.append(f"expired stale assignment #{a.id}")

    c.resolve(strategy, body.get("description") or "; ".join(actions) or "Resolved manually", resolved_by_user_id=user.id)
    AuditLog.record(
        action="conflict.resolve", resource_type="agent_conflict", resource_id=c.id,
        actor_type="human", actor_user_id=user.id,
        detail={"strategy": strategy.value, "conflict_type": c.conflict_type.value if c.conflict_type else None,
                "actions": actions},
    )
    db.session.commit()
    _queue_sse(user.id, "conflict_resolved", {"conflict_id": conflict_id, "strategy": strategy.value})
    flush_sse_notifications()
    return ApiResponse.success({
        "conflict": c.to_dict(),
        "actions": actions,
    }, "Conflict resolved").to_response()


@agents_bp.route("/conflicts/dashboard", methods=["GET"])
@unified_auth_required
def conflicts_dashboard():
    """Aggregate conflict stats for the current user."""
    user = get_current_user()
    qs = AgentConflict.query.filter_by(owner_id=user.id)
    total = qs.count()
    by_type = {}
    by_status = {}
    by_severity = {}
    for ct in ConflictType:
        by_type[ct.value] = qs.filter_by(conflict_type=ct).count()
    for cs in ConflictStatus:
        by_status[cs.value] = qs.filter_by(status=cs).count()
    for sev in ConflictSeverity:
        by_severity[sev.value] = qs.filter_by(severity=sev).count()
    active = qs.filter(AgentConflict.status.in_([
        ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING
    ])).count()

    # Resolution latency stats: how long conflicts sit before being cleared.
    # Buckets: <1h, 1-24h, 1-7d, >7d. Reveals whether conflicts languish.
    resolved_rows = qs.filter(
        AgentConflict.resolved_at.isnot(None),
    ).with_entities(AgentConflict.created_at, AgentConflict.resolved_at).all()
    latencies = []
    for created, resolved in resolved_rows:
        if created and resolved and resolved > created:
            latencies.append((resolved - created).total_seconds())
    latency_stats = {"count": len(latencies), "avg_seconds": None,
                     "median_seconds": None, "max_seconds": None,
                     "by_bucket": {"under_1h": 0, "1h_to_24h": 0, "1d_to_7d": 0, "over_7d": 0}}
    if latencies:
        latencies.sort()
        latency_stats["avg_seconds"] = round(sum(latencies) / len(latencies), 1)
        mid = len(latencies) // 2
        latency_stats["median_seconds"] = round(latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2, 1)
        latency_stats["max_seconds"] = round(latencies[-1], 1)
        for s in latencies:
            if s < 3600:
                latency_stats["by_bucket"]["under_1h"] += 1
            elif s < 86400:
                latency_stats["by_bucket"]["1h_to_24h"] += 1
            elif s < 604800:
                latency_stats["by_bucket"]["1d_to_7d"] += 1
            else:
                latency_stats["by_bucket"]["over_7d"] += 1

    return ApiResponse.success({
        "total": total,
        "active": active,
        "by_type": by_type,
        "by_status": by_status,
        "by_severity": by_severity,
        "resolution_latency": latency_stats,
    }).to_response()


@agents_bp.route("/conflicts/by-agent", methods=["GET"])
@unified_auth_required
def conflicts_by_agent():
    """Per-Agent conflict involvement counts for the current user.

    Each conflict carries a JSON ``agent_ids`` list of parties; this expands
    those lists and counts, per Agent: total conflicts, active conflicts, and
    conflicts where the Agent appeared. Returns the top N by total, enriched
    with the Agent's name/kind for display. Reveals which Agents are most
    conflict-prone.
    """
    user = get_current_user()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    rows = AgentConflict.query.filter_by(owner_id=user.id).with_entities(
        AgentConflict.agent_ids, AgentConflict.status
    ).all()
    active_statuses = {ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING}
    agg: dict = {}
    for agent_ids, status in rows:
        for aid in (agent_ids or []):
            entry = agg.setdefault(aid, {"agent_id": aid, "total": 0, "active": 0})
            entry["total"] += 1
            if status in active_statuses:
                entry["active"] += 1

    top = sorted(agg.values(), key=lambda x: x["total"], reverse=True)[:limit]
    agent_ids = [e["agent_id"] for e in top]
    agents = {a.id: a for a in Agent.query.filter(Agent.id.in_(agent_ids)).all()} if agent_ids else {}
    for e in top:
        a = agents.get(e["agent_id"])
        e["name"] = a.name if a else None
        e["kind"] = a.kind.value if a and a.kind else None
    return ApiResponse.success({"items": top}).to_response()


@agents_bp.route("/conflicts/strategy-stats", methods=["GET"])
@unified_auth_required
def conflicts_strategy_stats():
    """Resolution strategy effectiveness for the current user.

    For each ``ConflictResolutionStrategy`` actually used (resolution_strategy
    set on a resolved/ignored conflict): usage count, and the recurrence rate
    — the fraction of conflicts resolved with that strategy whose ``task_id``
    later saw another conflict. A high recurrence rate flags strategies that
    suppress rather than solve. Conflicts without a task_id are excluded from
    recurrence calculation (cannot be linked to a later conflict).
    """
    user = get_current_user()
    rows = AgentConflict.query.filter_by(owner_id=user.id).filter(
        AgentConflict.resolution_strategy.isnot(None)
    ).with_entities(
        AgentConflict.resolution_strategy, AgentConflict.task_id
    ).all()

    usage: dict = {}
    task_strategies: list = []  # (task_id, strategy) for recurrence join
    for strat, task_id in rows:
        if strat is None:
            continue
        s = strat.value if hasattr(strat, "value") else str(strat)
        entry = usage.setdefault(s, {"strategy": s, "uses": 0, "recurrences": 0, "with_task": 0})
        entry["uses"] += 1
        if task_id is not None:
            entry["with_task"] += 1
            task_strategies.append((task_id, s))

    # Recurrence: a task that had a conflict resolved with strategy S, then
    # later had *any* conflict again. Count distinct tasks per strategy.
    all_task_conflicts = AgentConflict.query.filter_by(owner_id=user.id).filter(
        AgentConflict.task_id.isnot(None)
    ).with_entities(AgentConflict.task_id).all()
    task_conflict_count: dict = {}
    for (tid,) in all_task_conflicts:
        task_conflict_count[tid] = task_conflict_count.get(tid, 0) + 1

    seen_tasks: dict = {}
    for tid, s in task_strategies:
        if tid in seen_tasks:
            continue
        seen_tasks[tid] = s
        if task_conflict_count.get(tid, 0) > 1:
            usage[s]["recurrences"] += 1

    items = []
    for s, entry in usage.items():
        denom = entry["with_task"] or 1
        items.append({
            "strategy": s,
            "uses": entry["uses"],
            "with_task": entry["with_task"],
            "recurrences": entry["recurrences"],
            "recurrence_rate": round(entry["recurrences"] / denom, 3),
        })
    items.sort(key=lambda x: x["uses"], reverse=True)
    return ApiResponse.success({"items": items}).to_response()


@agents_bp.route("/conflicts/trend", methods=["GET"])
@unified_auth_required
def conflicts_trend():
    """Daily conflict detection vs resolution counts for the current user.

    Buckets by calendar day (UTC). ``detected`` uses ``created_at`` (when the
    scan found the conflict), ``resolved`` uses ``resolved_at`` (filled on
    resolve/ignore/auto-resolve). Useful for charting whether conflicts are
    accumulating faster than they are being cleared.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    from sqlalchemy import func as sa_func
    daily_detected = (
        db.session.query(
            sa_func.date(AgentConflict.created_at).label("date"),
            sa_func.count(AgentConflict.id).label("detected"),
        )
        .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
        .group_by(sa_func.date(AgentConflict.created_at))
        .all()
    )
    daily_resolved = (
        db.session.query(
            sa_func.date(AgentConflict.resolved_at).label("date"),
            sa_func.count(AgentConflict.id).label("resolved"),
        )
        .filter(
            AgentConflict.owner_id == user.id,
            AgentConflict.resolved_at.isnot(None),
            AgentConflict.resolved_at >= since,
        )
        .group_by(sa_func.date(AgentConflict.resolved_at))
        .all()
    )

    trend_map: dict = {}
    for d, c in daily_detected:
        key = str(d)
        trend_map[key] = {"date": key, "detected": c, "resolved": 0}
    for d, c in daily_resolved:
        key = str(d)
        if key in trend_map:
            trend_map[key]["resolved"] = c
        else:
            trend_map[key] = {"date": key, "detected": 0, "resolved": c}
    trend = sorted(trend_map.values(), key=lambda x: x["date"])
    return ApiResponse.success({
        "days": days,
        "trend": trend,
    }).to_response()


# Strategies considered safe to apply automatically (low-risk, reversible).
_AUTO_SAFE_STRATEGIES = {
    ConflictResolutionStrategy.AUTO_RETRY,
    ConflictResolutionStrategy.LEAST_LOADED,
}


@agents_bp.route("/maintenance/auto-resolve-conflicts", methods=["POST"])
@unified_auth_required
def auto_resolve_conflicts():
    """Maintenance endpoint: scan for conflicts and auto-resolve low-severity
    ones using their suggested strategy, when that strategy is in the safe set.

    CRITICAL conflicts are never auto-resolved — they require human judgement.
    Returns counts of scanned / auto-resolved / skipped.
    """
    user = get_current_user()
    now = datetime.utcnow()
    # Run detection first to surface any new conflicts
    detected = []
    detected.extend(_detect_duplicate_claims(user, now))
    detected.extend(_detect_assignment_stale(user, now))
    detected.extend(_detect_protocol_deadlock(user, now))
    for c in detected:
        db.session.add(c)
    db.session.flush()

    # Collect all DETECTED conflicts eligible for auto-resolution
    candidates = AgentConflict.query.filter(
        AgentConflict.owner_id == user.id,
        AgentConflict.status == ConflictStatus.DETECTED,
        AgentConflict.severity != ConflictSeverity.CRITICAL,
    ).all()

    auto_resolved = []
    skipped = []
    for c in candidates:
        strategy = c.suggested_strategy
        if strategy is None or strategy not in _AUTO_SAFE_STRATEGIES:
            skipped.append({"conflict_id": c.id, "reason": "no safe suggested strategy", "suggested": strategy.value if strategy else None})
            continue
        actions = []
        # Apply the same side-effects as the manual resolve endpoint
        if c.conflict_type == ConflictType.DUPLICATE_CLAIM and c.task_id:
            assignments = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES)
            ).order_by(TaskAssignment.created_at.asc()).all()
            if strategy == ConflictResolutionStrategy.LEAST_LOADED and assignments:
                best = None
                least = None
                for a in assignments:
                    cnt = TaskAssignment.query.filter(
                        TaskAssignment.agent_id == a.agent_id,
                        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                    ).count()
                    if least is None or cnt < least:
                        least = cnt
                        best = a
                if best:
                    for a in assignments:
                        if a.id != best.id:
                            a.state = TaskAssignmentState.CANCELLED
                            a.completed_at = now
                            actions.append(f"auto-cancelled assignment #{a.id}")
        elif c.conflict_type == ConflictType.ASSIGNMENT_STALE and c.task_id:
            if strategy == ConflictResolutionStrategy.AUTO_RETRY:
                stale = TaskAssignment.query.filter_by(task_id=c.task_id).filter(
                    TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                    TaskAssignment.lease_expires_at.isnot(None),
                    TaskAssignment.lease_expires_at < now,
                ).all()
                for a in stale:
                    a.state = TaskAssignmentState.EXPIRED
                    a.completed_at = now
                    actions.append(f"auto-expired stale assignment #{a.id}")
        c.resolve(strategy, "Auto-resolved by maintenance scan" + (f": {' '.join(actions)}" if actions else ""), resolved_by_user_id=None)
        auto_resolved.append({"conflict_id": c.id, "strategy": strategy.value, "actions": actions})

    db.session.commit()
    if auto_resolved:
        _queue_sse(user.id, "conflicts_auto_resolved", {"count": len(auto_resolved)})
        flush_sse_notifications()
        AuditLog.record(
            action="conflicts.auto_resolve",
            resource_type="system",
            resource_id=0,
            actor_type="system",
            detail={"detected": len(detected), "auto_resolved": len(auto_resolved), "skipped": len(skipped)},
            ip_address=_client_ip(),
        )
    return ApiResponse.success({
        "detected": len(detected),
        "auto_resolved": len(auto_resolved),
        "skipped": len(skipped),
        "resolved_details": auto_resolved,
        "skipped_details": skipped,
    }, f"Auto-resolve: {len(auto_resolved)} resolved, {len(skipped)} skipped").to_response()


# =========================================================================
