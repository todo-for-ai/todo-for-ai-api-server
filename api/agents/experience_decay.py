"""
Experience decay, validation, and alert routes.

Extracted from experience_analytics.py to separate decay/validation logic
from core analytics.
"""

from datetime import datetime, timedelta

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentExperience,
    validate_json_request,
)


@agents_bp.route("/experiences/decay-by-domain", methods=["GET"])
@unified_auth_required
def experiences_decay_by_domain():
    """Per-domain decay comparison for the user's experiences.

    Aggregates valid experiences by domain, reporting for each domain:
    total count, active count (confidence >= 0.5), decayed count
    (confidence < 0.5), average confidence, and total reuses.
    Sorted by decayed count descending. Reveals which knowledge
    domains have the most stale / low-confidence entries.
    """
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        limit = 15

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"domains": [], "total_active": 0, "total_decayed": 0}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.domain,
            AgentExperience.confidence,
            AgentExperience.times_reused,
        )
        .all()
    )

    buckets = {}  # {domain: {total, active, decayed, conf_sum, conf_n, reuses}}
    for domain, confidence, times_reused in rows:
        d = domain or "(未分类)"
        conf = confidence if confidence is not None else 0.0
        b = buckets.get(d)
        if b is None:
            b = {"total": 0, "active": 0, "decayed": 0, "conf_sum": 0.0, "conf_n": 0, "reuses": 0}
            buckets[d] = b
        b["total"] += 1
        if conf >= 0.5:
            b["active"] += 1
        else:
            b["decayed"] += 1
        b["conf_sum"] += conf
        b["conf_n"] += 1
        b["reuses"] += (times_reused or 0)

    total_active = sum(b["active"] for b in buckets.values())
    total_decayed = sum(b["decayed"] for b in buckets.values())

    domains = []
    for d, b in buckets.items():
        domains.append({
            "domain": d,
            "total": b["total"],
            "active": b["active"],
            "decayed": b["decayed"],
            "avg_confidence": round(b["conf_sum"] / b["conf_n"], 3) if b["conf_n"] else 0.0,
            "reuses": b["reuses"],
        })
    domains.sort(key=lambda x: x["decayed"], reverse=True)
    domains = domains[:limit]

    return ApiResponse.success({
        "domains": domains,
        "total_active": total_active,
        "total_decayed": total_decayed,
    }).to_response()


@agents_bp.route("/experiences/decay-by-task-type", methods=["GET"])
@unified_auth_required
def experiences_decay_by_task_type():
    """Per-task-type decay comparison for the user's experiences.

    Same as decay-by-domain but grouped by task_type. Reveals which
    task categories have the most stale / low-confidence entries.
    """
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        limit = 15

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"task_types": [], "total_active": 0, "total_decayed": 0}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.task_type,
            AgentExperience.confidence,
            AgentExperience.times_reused,
        )
        .all()
    )

    buckets = {}
    for task_type, confidence, times_reused in rows:
        t = task_type or "(未分类)"
        conf = confidence if confidence is not None else 0.0
        b = buckets.get(t)
        if b is None:
            b = {"total": 0, "active": 0, "decayed": 0, "conf_sum": 0.0, "conf_n": 0, "reuses": 0}
            buckets[t] = b
        b["total"] += 1
        if conf >= 0.5:
            b["active"] += 1
        else:
            b["decayed"] += 1
        b["conf_sum"] += conf
        b["conf_n"] += 1
        b["reuses"] += (times_reused or 0)

    total_active = sum(b["active"] for b in buckets.values())
    total_decayed = sum(b["decayed"] for b in buckets.values())

    task_types = []
    for t, b in buckets.items():
        task_types.append({
            "task_type": t,
            "total": b["total"],
            "active": b["active"],
            "decayed": b["decayed"],
            "avg_confidence": round(b["conf_sum"] / b["conf_n"], 3) if b["conf_n"] else 0.0,
            "reuses": b["reuses"],
        })
    task_types.sort(key=lambda x: x["decayed"], reverse=True)
    task_types = task_types[:limit]

    return ApiResponse.success({
        "task_types": task_types,
        "total_active": total_active,
        "total_decayed": total_decayed,
    }).to_response()


@agents_bp.route("/experiences/confidence-decay-forecast", methods=["GET"])
@unified_auth_required
def experiences_confidence_decay_forecast():
    """Confidence decay forecast using linear regression on daily averages.

    Computes daily average confidence from the reuse trend, fits a simple
    linear regression, and projects 7 days into the future. Returns the
    historical trend plus forecast points, regression slope, and projected
    days-until-decay-threshold (avg confidence < 0.5). Reveals whether
    the experience pool is decaying and when it might cross the decay
    threshold if the trend continues.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"trend": [], "forecast": [], "slope": 0, "r_squared": 0, "days_to_decay": None}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
            AgentExperience.confidence.isnot(None),
        )
        .with_entities(
            AgentExperience.confidence,
            AgentExperience.last_reused_at,
            AgentExperience.created_at,
        )
        .all()
    )

    since = datetime.utcnow() - timedelta(days=days)

    # Bucket by date
    buckets: dict = {}
    for conf, last_reused_at, created_at in rows:
        ref = last_reused_at or created_at
        if ref is None or ref < since:
            continue
        d = ref.date().isoformat()
        buckets.setdefault(d, []).append(conf if conf is not None else 0.0)

    if len(buckets) < 3:
        return ApiResponse.success({"trend": [], "forecast": [], "slope": 0, "r_squared": 0, "days_to_decay": None}).to_response()

    # Build sorted daily averages
    daily = []
    for d in sorted(buckets.keys()):
        vals = buckets[d]
        daily.append({"date": d, "avg_confidence": round(sum(vals) / len(vals), 3)})

    # Linear regression: y = a + b*x
    n = len(daily)
    xs = list(range(n))
    ys = [d["avg_confidence"] for d in daily]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    ss_yy = sum((y - y_mean) ** 2 for y in ys)

    b = ss_xy / ss_xx if ss_xx else 0.0
    a = y_mean - b * x_mean
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx and ss_yy else 0.0

    # Forecast 7 days ahead
    last_date = datetime.strptime(daily[-1]["date"], "%Y-%m-%d").date()
    forecast = []
    for i in range(1, 8):
        fx = n - 1 + i
        fy = a + b * fx
        fd = last_date + timedelta(days=i)
        forecast.append({"date": fd.isoformat(), "predicted_confidence": round(max(0, min(1, fy)), 3)})

    # Days until avg confidence < 0.5
    days_to_decay = None
    if b < 0 and y_mean > 0.5:
        # Solve a + b * x = 0.5
        x_decay = (0.5 - a) / b
        days_to_decay = max(0, round(x_decay - (n - 1)))

    return ApiResponse.success({
        "trend": daily,
        "forecast": forecast,
        "slope": round(b, 4),
        "r_squared": round(r_squared, 4),
        "days_to_decay": days_to_decay,
    }).to_response()


# ---------------------------------------------------------------------------
# Agent Experience Decay & Validation endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/<int:agent_id>/experiences/decay", methods=["POST"])
@unified_auth_required
def apply_experience_decay(agent_id):
    """Apply time-based confidence decay to an Agent's experiences.

    Query params:
      days_threshold – minimum age in days before decay applies (default 30)
      decay_rate – confidence reduction factor per cycle (default 0.02)
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request() or {}
    days_threshold = data.get("days_threshold", 30)
    decay_rate = data.get("decay_rate", 0.02)

    decayed = AgentExperience.apply_decay(
        agent_id=agent_id,
        days_threshold=days_threshold,
        decay_rate=decay_rate,
    )
    db.session.commit()

    return ApiResponse.success(
        {"decayed_count": decayed},
        f"Applied decay to {decayed} experiences",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/validate", methods=["POST"])
@unified_auth_required
def validate_experience(agent_id, experience_id):
    """Cross-validate an experience by another Agent.

    The validator agent confirms or refutes the experience's accuracy,
    affecting its confidence score.
    """
    user = get_current_user()
    # Verify the validator agent belongs to the user
    validator = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not validator:
        return ApiResponse.not_found("Validator agent not found").to_response()

    data = validate_json_request()
    is_accurate = data.get("is_accurate", True)
    if "is_accurate" not in data:
        return ApiResponse.error("is_accurate is required (true/false)").to_response()

    result = AgentExperience.cross_validate(
        experience_id=experience_id,
        validator_agent_id=agent_id,
        is_accurate=is_accurate,
    )
    if not result:
        return ApiResponse.not_found("Experience not found or already invalid").to_response()

    db.session.commit()
    action = "验证通过" if is_accurate else "已反驳"
    return ApiResponse.success(
        result.to_dict(),
        f"经验已{action}，新置信度: {result.confidence}",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/validation-stats", methods=["GET"])
@unified_auth_required
def get_experience_validation_stats(agent_id):
    """Get validation statistics for an Agent's experiences."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    stats = AgentExperience.get_validation_stats(agent_id)
    return ApiResponse.success(stats).to_response()


@agents_bp.route("/maintenance/decay-all-experiences", methods=["POST"])
@unified_auth_required
def decay_all_experiences():
    """System maintenance: apply decay to all agents' experiences.

    Typically called by a scheduled job or admin action.
    """
    user = get_current_user()
    data = validate_json_request() or {}
    days_threshold = data.get("days_threshold", 30)
    decay_rate = data.get("decay_rate", 0.02)

    decayed = AgentExperience.apply_decay(
        agent_id=None,  # All agents
        days_threshold=days_threshold,
        decay_rate=decay_rate,
    )
    db.session.commit()

    return ApiResponse.success(
        {"decayed_count": decayed},
        f"Applied decay to {decayed} experiences across all agents",
    ).to_response()


# ---------------------------------------------------------------------------
# Agent Adaptive Capabilities endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/experiences/decay-alerts", methods=["GET"])
@unified_auth_required
def experiences_decay_alerts():
    """Flag Agents whose experience-base confidence is declining.

    Splits each Agent's valid experiences into two halves by created_at
    (older vs newer) within the window and compares average confidence.
    Agents whose newer-half average is meaningfully below the older-half
    average are returned as decay alerts with a recommended action, so
    owners can re-train or review recent low-quality experiences.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
        min_drop = max(0.02, min(0.5, float(request.args.get("min_drop", 0.1))))
        limit = max(1, min(30, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days, min_drop, limit = 30, 0.1, 10

    since = datetime.utcnow() - timedelta(days=days)
    midpoint = datetime.utcnow() - timedelta(days=days / 2)

    rows = (
        AgentExperience.query
        .join(Agent, AgentExperience.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            AgentExperience.created_at >= since,
            AgentExperience.is_valid.is_(True),
            AgentExperience.confidence.isnot(None),
        )
        .with_entities(
            AgentExperience.agent_id,
            Agent.name,
            AgentExperience.confidence,
            AgentExperience.created_at,
        )
        .all()
    )

    buckets = {}
    for aid, aname, conf, created in rows:
        if conf is None or created is None:
            continue
        info = buckets.setdefault(aid, {"name": aname or f"Agent#{aid}", "older": [], "newer": []})
        if created < midpoint:
            info["older"].append(conf)
        else:
            info["newer"].append(conf)

    def _avg(xs):
        return sum(xs) / len(xs) if xs else None

    alerts = []
    for aid, info in buckets.items():
        older_avg = _avg(info["older"])
        newer_avg = _avg(info["newer"])
        if older_avg is None or newer_avg is None:
            continue
        drop = older_avg - newer_avg
        if drop < min_drop:
            continue
        alerts.append({
            "agent_id": aid,
            "agent_name": info["name"],
            "older_avg_confidence": round(older_avg, 3),
            "newer_avg_confidence": round(newer_avg, 3),
            "drop": round(drop, 3),
            "older_count": len(info["older"]),
            "newer_count": len(info["newer"]),
            "current_confidence": round(newer_avg, 3),
            "recommendation": "review_recent_experiences" if drop >= 0.2 else "monitor",
        })

    alerts.sort(key=lambda a: a["drop"], reverse=True)
    return ApiResponse.success({
        "alerts": alerts[:limit],
        "total_alerts": len(alerts),
        "days": days,
        "min_drop": min_drop,
    }).to_response()