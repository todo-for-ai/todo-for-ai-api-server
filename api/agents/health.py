"""Agent composite health endpoints (thin routes).

业务逻辑全部在 services/agent_health_analytics.py（评分 / 告警 / 趋势 /
状态迁移）；本文件只做参数解析（含边界钳制）、鉴权与响应包装。
"""

from flask import request

from ._shared import ApiResponse, agents_bp, get_current_user, unified_auth_required
from services.agent_health_analytics import (
    compute_agent_health,
    compute_health_alerts,
    compute_health_trend,
    compute_state_transitions,
)


def _parse_days(default=30, minimum=1, maximum=365):
    try:
        return max(minimum, min(maximum, int(request.args.get("days", default))))
    except (TypeError, ValueError):
        return default


def _parse_weights():
    return {
        "reputation": request.args.get("w_reputation"),
        "completion": request.args.get("w_completion"),
        "conflict": request.args.get("w_conflict"),
        "violation": request.args.get("w_violation"),
    }


def _parse_agent_id():
    raw = request.args.get("agent_id")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@agents_bp.route("/health", methods=["GET"])
@unified_auth_required
def agent_health():
    """Per-Agent composite health score for the current user.

    Combines reputation / completion / conflict / violation sub-scores into
    a single 0-100 score per Agent. Optional ``w_reputation`` etc. query
    params override the default sub-score weights (normalised to sum to 1).
    """
    user = get_current_user()
    days = _parse_days()
    weights = _parse_weights()
    days, items = compute_agent_health(user.id, days, weights=weights)
    return ApiResponse.success({"days": days, "items": items}).to_response()


@agents_bp.route("/health/alerts", methods=["GET"])
@unified_auth_required
def agent_health_alerts():
    """Low-health Agent alert list with triggering reasons and recommendations."""
    user = get_current_user()
    days = _parse_days()
    try:
        min_health_score = max(0, min(100, float(request.args.get("min_health_score", 60))))
    except (TypeError, ValueError):
        min_health_score = 60
    weights = _parse_weights()
    alerts = compute_health_alerts(user.id, days, min_health_score, weights=weights)
    return ApiResponse.success({
        "days": days,
        "min_health_score": min_health_score,
        "items": alerts,
    }).to_response()


@agents_bp.route("/health/trend", methods=["GET"])
@unified_auth_required
def agent_health_trend():
    """Daily reputation-derived health trend (optionally for one ``agent_id``)."""
    user = get_current_user()
    days = _parse_days()
    data = compute_health_trend(user.id, days, agent_id=_parse_agent_id())
    return ApiResponse.success(data).to_response()


@agents_bp.route("/health/state-transitions", methods=["GET"])
@unified_auth_required
def agent_health_state_transitions():
    """Health state transition flow (healthy/degraded/critical), Sankey-style."""
    user = get_current_user()
    days = _parse_days(default=30, minimum=7)
    data = compute_state_transitions(user.id, days)
    return ApiResponse.success(data).to_response()
