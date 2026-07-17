"""
Agent composite health score, alerts, trend, and state-transition endpoints.
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
    TaskAssignment,
    TaskAssignmentState,
    AgentConflict,
    SandboxViolation,
    get_request_args,
    paginate_query,
)

_SUB_SCORE_LABELS = {
    "reputation": "声誉",
    "completion": "完成率",
    "conflict": "冲突控制",
    "violation": "沙盒合规",
}


def _compute_agent_health(user, days, weights=None, with_recommendations=False):
    """Shared computation for agent composite health (used by /health and
    /health/alerts). Returns (days, items) sorted by health_score desc.

    ``weights`` optionally overrides the default sub-score weights
    {reputation: 0.4, completion: 0.3, conflict: 0.15, violation: 0.15};
    they are normalised to sum to 1.0. When ``with_recommendations`` is
    True, each item carries a ``recommendations`` list of concrete
    improvement suggestions derived from its weakest sub-scores."""
    w = {"reputation": 0.4, "completion": 0.3, "conflict": 0.15, "violation": 0.15}
    if weights:
        for k in w:
            try:
                v = float(weights.get(k, w[k]))
            except (TypeError, ValueError):
                v = w[k]
            w[k] = max(0.0, v)
    total_w = sum(w.values())
    if total_w <= 0:
        w = {"reputation": 0.4, "completion": 0.3, "conflict": 0.15, "violation": 0.15}
        total_w = sum(w.values())
    w = {k: v / total_w for k, v in w.items()}

    since = datetime.utcnow() - timedelta(days=days)
    agents = Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id, Agent.name, Agent.status).all()
    if not agents:
        return days, []

    agent_ids = [a.id for a in agents]

    reps = {r.agent_id: r for r in AgentReputation.query.filter(AgentReputation.agent_id.in_(agent_ids)).all()}
    assign_rows = (
        TaskAssignment.query
        .filter(TaskAssignment.agent_id.in_(agent_ids), TaskAssignment.created_at >= since)
        .with_entities(TaskAssignment.agent_id, TaskAssignment.state)
        .all()
    )
    prod: dict = {}
    for aid, state in assign_rows:
        b = prod.setdefault(aid, {"total": 0, "done": 0})
        b["total"] += 1
        if state and state.value == "done":
            b["done"] += 1

    conflict_rows = (
        AgentConflict.query
        .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
        .with_entities(AgentConflict.agent_ids)
        .all()
    )
    conflict_counts: dict = {}
    for (agent_ids_json,) in conflict_rows:
        for aid in (agent_ids_json or []):
            conflict_counts[aid] = conflict_counts.get(aid, 0) + 1

    violation_rows = (
        SandboxViolation.query
        .filter(SandboxViolation.agent_id.in_(agent_ids), SandboxViolation.blocked_at >= since)
        .with_entities(SandboxViolation.agent_id, func.count(SandboxViolation.id))
        .group_by(SandboxViolation.agent_id)
        .all()
    )
    violation_counts = {aid: c for aid, c in violation_rows}

    max_conflicts = max(conflict_counts.values(), default=1)
    max_violations = max(violation_counts.values(), default=1)

    items = []
    for a in agents:
        rep = reps.get(a.id)
        rep_score = rep.score if rep and rep.score is not None else 50.0
        p = prod.get(a.id, {"total": 0, "done": 0})
        completion_rate = (p["done"] / p["total"] * 100) if p["total"] > 0 else None
        completion_score = completion_rate if completion_rate is not None else 50.0
        cc = conflict_counts.get(a.id, 0)
        vc = violation_counts.get(a.id, 0)
        conflict_score = 100 * (1 - cc / max_conflicts) if max_conflicts > 0 else 100.0
        violation_score = 100 * (1 - vc / max_violations) if max_violations > 0 else 100.0

        health = round(
            rep_score * w["reputation"] + completion_score * w["completion"]
            + conflict_score * w["conflict"] + violation_score * w["violation"],
            1,
        )
        sub_scores = {
            "reputation": round(rep_score, 1),
            "completion": round(completion_score, 1),
            "conflict": round(conflict_score, 1),
            "violation": round(violation_score, 1),
        }
        item = {
            "agent_id": a.id,
            "name": a.name,
            "status": a.status.value if a.status else None,
            "health_score": health,
            "reputation_score": round(rep_score, 1),
            "completion_rate": round(completion_rate, 1) if completion_rate is not None else None,
            "total_assignments": p["total"],
            "done_assignments": p["done"],
            "conflicts": cc,
            "sandbox_violations": vc,
            "sub_scores": sub_scores,
        }
        if with_recommendations:
            recs = []
            if rep_score < 50:
                recs.append("声誉分偏低，建议复盘近期失败任务并补充正向反馈以恢复信任")
            if completion_rate is not None and completion_rate < 50:
                recs.append("完成率偏低，建议核减负载或拆解复杂任务后再分配")
            elif p["total"] == 0:
                recs.append("近期无任务分配，建议主动领取任务以建立产出记录")
            if cc > 0:
                recs.append(f"近期发生 {cc} 次协作冲突，建议复核协作边界与消息协议")
            if vc > 0:
                recs.append(f"近期发生 {vc} 次沙盒违规，建议收紧工具权限并复查沙盒策略")
            # 按子分数升序追加最弱维度提示
            weakest = sorted(sub_scores.items(), key=lambda x: x[1])[:1]
            for name, score in weakest:
                if not recs:
                    recs.append(f"当前最弱维度为「{_SUB_SCORE_LABELS.get(name, name)}」({score})，建议针对性改进")
            item["recommendations"] = recs
        items.append(item)
    items.sort(key=lambda x: x["health_score"], reverse=True)
    return days, items


@agents_bp.route("/health", methods=["GET"])
@unified_auth_required
def agent_health():
    """Per-Agent composite health score for the current user.

    Combines multiple dimensions into a single 0-100 health score per Agent:
      - reputation score (weight 0.4)
      - assignment completion rate (weight 0.3)
      - conflict penalty (weight 0.15): fewer recent conflicts is better
      - sandbox violation penalty (weight 0.15): fewer recent violations is better

    Also returns the raw sub-scores so callers can see what drags health down.
    Optional ``w_reputation`` / ``w_completion`` / ``w_conflict`` /
    ``w_violation`` query params override the default sub-score weights
    (normalised to sum to 1). Reveals a single comparable metric across all
    of a user's Agents.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    weights = {
        "reputation": request.args.get("w_reputation"),
        "completion": request.args.get("w_completion"),
        "conflict": request.args.get("w_conflict"),
        "violation": request.args.get("w_violation"),
    }
    days, items = _compute_agent_health(user, days, weights=weights)
    return ApiResponse.success({"days": days, "items": items}).to_response()


@agents_bp.route("/health/alerts", methods=["GET"])
@unified_auth_required
def agent_health_alerts():
    """Low-health Agent alert list for the current user.

    Returns Agents whose composite health_score falls below
    ``min_health_score`` (default 60), with triggering reasons (low
    reputation / low completion / conflicts / violations) and concrete
    ``recommendations``. Optional weight overrides (``w_reputation`` /
    ``w_completion`` / ``w_conflict`` / ``w_violation``) re-weight the
    composite score. Each entry includes the full health fields. Surfaces
    Agents needing attention.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        min_health_score = max(0, min(100, float(request.args.get("min_health_score", 60))))
    except (TypeError, ValueError):
        days = 30
        min_health_score = 60
    weights = {
        "reputation": request.args.get("w_reputation"),
        "completion": request.args.get("w_completion"),
        "conflict": request.args.get("w_conflict"),
        "violation": request.args.get("w_violation"),
    }
    _, items = _compute_agent_health(user, days, weights=weights, with_recommendations=True)
    alerts = []
    for a in items:
        if a["health_score"] >= min_health_score:
            continue
        reasons = []
        if a["sub_scores"]["reputation"] < 50:
            reasons.append(f"声誉 {a['sub_scores']['reputation']} 偏低")
        if a["completion_rate"] is not None and a["completion_rate"] < 50:
            reasons.append(f"完成率 {a['completion_rate']}% 偏低")
        if a["conflicts"] > 0:
            reasons.append(f"冲突 {a['conflicts']} 次")
        if a["sandbox_violations"] > 0:
            reasons.append(f"违规 {a['sandbox_violations']} 次")
        a_copy = dict(a)
        a_copy["reasons"] = reasons
        alerts.append(a_copy)

    return ApiResponse.success({
        "days": days,
        "min_health_score": min_health_score,
        "items": alerts,
    }).to_response()


@agents_bp.route("/health/trend", methods=["GET"])
@unified_auth_required
def agent_health_trend():
    """Daily reputation-derived health trend for the current user's Agents.

    Aggregates ``reputation.update`` audit entries (which carry ``new_score``
    and ``score_delta`` in detail) by day across all of the user's Agents.
    Per-day: average new_score (last-seen per agent that day), count of
    positive deltas, count of negative deltas. A proxy for whether the
    fleet's health is rising or falling over time.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        agent_id = int(request.args.get("agent_id")) if request.args.get("agent_id") else None
    except (TypeError, ValueError):
        days = 30
        agent_id = None

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if agent_id is not None and agent_id not in agent_ids:
        return ApiResponse.success({"days": days, "trend": [], "total_positive": 0, "total_negative": 0, "agent_id": agent_id, "agent_name": None, "by_kind_overall": {}}).to_response()
    if agent_id is not None:
        agent_ids = [agent_id]
    if not agent_ids:
        return ApiResponse.success({"days": days, "trend": [], "total_positive": 0, "total_negative": 0, "by_kind_overall": {}}).to_response()

    selected_name = None
    if agent_id is not None:
        selected_name = Agent.query.filter_by(id=agent_id).with_entities(Agent.name).first()
        selected_name = selected_name[0] if selected_name else None

    # agent_id -> kind 映射，用于按 kind 分组趋势
    kind_map = {
        aid: (k.value if k else "unknown")
        for aid, k in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.kind).all()
    }

    rows = (
        AuditLog.query
        .filter(
            AuditLog.action == "reputation.update",
            AuditLog.resource_type == "agent",
            AuditLog.resource_id.in_(agent_ids),
            AuditLog.created_at >= since,
        )
        .with_entities(
            func.date(AuditLog.created_at).label("d"),
            AuditLog.resource_id,
            AuditLog.detail,
        )
        .all()
    )

    # per (day, agent) keep last new_score; track pos/neg deltas
    last_score_by_day_agent: dict = {}
    pos_by_day: dict = {}
    neg_by_day: dict = {}
    for d, aid, detail in rows:
        if not d:
            continue
        key = (str(d), aid)
        det = detail or {}
        new_score = det.get("new_score")
        delta = det.get("score_delta")
        if new_score is not None:
            last_score_by_day_agent[key] = new_score
        if delta is not None:
            try:
                dval = float(delta)
                if dval > 0:
                    pos_by_day[str(d)] = pos_by_day.get(str(d), 0) + 1
                elif dval < 0:
                    neg_by_day[str(d)] = neg_by_day.get(str(d), 0) + 1
            except (TypeError, ValueError):
                pass

    # 按日聚合平均 new_score
    day_scores: dict = {}
    day_kind_scores: dict = {}  # {day: {kind: [scores]}}
    for (day, aid), score in last_score_by_day_agent.items():
        day_scores.setdefault(day, []).append(score)
        k = kind_map.get(aid, "unknown")
        day_kind_scores.setdefault(day, {}).setdefault(k, []).append(score)

    # 按日附加冲突事件计数（owner_id 命中当前用户；单 Agent 时进一步按参与方过滤）
    if agent_id is not None:
        conflict_rows_raw = (
            AgentConflict.query
            .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
            .with_entities(func.date(AgentConflict.created_at).label("d"), AgentConflict.agent_ids)
            .all()
        )
        conflict_by_day: dict = {}
        for d, agent_ids_json in conflict_rows_raw:
            if not d:
                continue
            if agent_ids_json and agent_id in (agent_ids_json or []):
                conflict_by_day[str(d)] = conflict_by_day.get(str(d), 0) + 1
    else:
        conflict_rows = (
            AgentConflict.query
            .filter(AgentConflict.owner_id == user.id, AgentConflict.created_at >= since)
            .with_entities(func.date(AgentConflict.created_at).label("d"), func.count(AgentConflict.id))
            .group_by("d")
            .all()
        )
        conflict_by_day = {str(d): c for d, c in conflict_rows if d}

    # 按日附加沙盒违规事件计数
    violation_rows = (
        SandboxViolation.query
        .filter(SandboxViolation.agent_id.in_(agent_ids), SandboxViolation.blocked_at >= since)
        .with_entities(func.date(SandboxViolation.blocked_at).label("d"), func.count(SandboxViolation.id))
        .group_by("d")
        .all()
    )
    violation_by_day = {str(d): c for d, c in violation_rows if d}

    trend = []
    kind_overall: dict = {}  # {kind: [scores]} 用于顶层 overall
    for day in sorted(day_scores.keys()):
        scores = day_scores[day]
        avg = round(sum(scores) / len(scores), 2) if scores else None
        dk = day_kind_scores.get(day, {})
        by_kind_avg: dict = {}
        for k, ks in dk.items():
            if ks:
                by_kind_avg[k] = round(sum(ks) / len(ks), 2)
                kind_overall.setdefault(k, []).extend(ks)
        trend.append({
            "date": day,
            "avg_reputation": avg,
            "positive": pos_by_day.get(day, 0),
            "negative": neg_by_day.get(day, 0),
            "conflicts": conflict_by_day.get(day, 0),
            "sandbox_violations": violation_by_day.get(day, 0),
            "by_kind_avg": by_kind_avg,
        })

    by_kind_overall = {k: round(sum(ks) / len(ks), 2) for k, ks in kind_overall.items() if ks}
    by_kind_overall_sorted = dict(sorted(by_kind_overall.items(), key=lambda kv: kv[1], reverse=True))

    return ApiResponse.success({
        "days": days,
        "trend": trend,
        "total_positive": sum(pos_by_day.values()),
        "total_negative": sum(neg_by_day.values()),
        "total_conflicts": sum(conflict_by_day.values()),
        "total_violations": sum(violation_by_day.values()),
        "agent_id": agent_id,
        "agent_name": selected_name,
        "by_kind_overall": by_kind_overall_sorted,
    }).to_response()


@agents_bp.route("/health/state-transitions", methods=["GET"])
@unified_auth_required
def agent_health_state_transitions():
    """Agent health state transition flow for the current user.

    Based on daily health trend data, classifies each agent-day as
    healthy/degraded/critical based on reputation score thresholds.
    Counts transitions between states, returning a flow suitable for
    Sankey-style visualization.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"days": days, "transitions": [], "states": []}).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    # Get daily scores per agent from audit log
    rows = (
        AuditLog.query
        .filter(
            AuditLog.action == "reputation.update",
            AuditLog.resource_type == "agent",
            AuditLog.resource_id.in_(agent_ids),
            AuditLog.created_at >= since,
        )
        .with_entities(
            func.date(AuditLog.created_at).label("d"),
            AuditLog.resource_id,
            AuditLog.detail,
        )
        .all()
    )

    # Classify score → state
    def classify(score: float) -> str:
        if score >= 80:
            return "healthy"
        if score >= 50:
            return "degraded"
        return "critical"

    # Build {agent_id: {date: state}} using last score per day
    agent_states: dict = {}  # {agent_id: [(date, state)]}
    for d, aid, detail in rows:
        if not d:
            continue
        try:
            new_score = detail.get("new_score", 0) if isinstance(detail, dict) else 0
        except (AttributeError, TypeError):
            new_score = 0
        state = classify(new_score)
        agent_states.setdefault(aid, {})[d.isoformat()] = state

    # Count transitions
    transitions: dict = {}  # {(from_state, to_state): count}
    state_totals: dict = {}  # {state: count}
    for aid, date_states in agent_states.items():
        sorted_dates = sorted(date_states.items())
        for i in range(len(sorted_dates)):
            _, s = sorted_dates[i]
            state_totals[s] = state_totals.get(s, 0) + 1
            if i > 0:
                prev_s = sorted_dates[i - 1][1]
                if prev_s != s:
                    key = (prev_s, s)
                    transitions[key] = transitions.get(key, 0) + 1

    # Format for Sankey
    states = ["healthy", "degraded", "critical"]
    flows = []
    for (src, dst), cnt in sorted(transitions.items(), key=lambda kv: kv[1], reverse=True):
        flows.append({"source": src, "target": dst, "value": cnt})

    return ApiResponse.success({
        "days": days,
        "states": [{"name": s, "count": state_totals.get(s, 0)} for s in states],
        "flows": flows,
        "total_transitions": sum(transitions.values()),
    }).to_response()

