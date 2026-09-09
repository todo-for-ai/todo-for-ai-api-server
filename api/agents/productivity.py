"""Agent productivity endpoints (thin routes).

计算逻辑全部在 services/agent_productivity_analytics.py；本文件只做
参数解析（含边界钳制与非法回退）、鉴权与响应包装。
"""

from flask import request

from ._shared import ApiResponse, agents_bp, get_current_user, unified_auth_required
from services.agent_productivity_analytics import (
    idle_ranking,
    productivity_alerts,
    productivity_by_kind,
    productivity_calendar_heatmap,
    productivity_hourly_heatmap,
    productivity_summary,
    productivity_trend,
    productivity_weekly_comparison,
)


def _parse_int_arg(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _parse_float_arg(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, float(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


@agents_bp.route("/productivity", methods=["GET"])
@unified_auth_required
def agent_productivity():
    """Per-Agent productivity stats: assignments by state, completion rate,
    average completion duration. Sorted by done desc, limited."""
    user = get_current_user()
    days = _parse_int_arg("days", 30, 1, 365)
    limit = _parse_int_arg("limit", 20, 1, 50)
    return ApiResponse.success(
        productivity_summary(user.id, days, limit)).to_response()


@agents_bp.route("/productivity/trend", methods=["GET"])
@unified_auth_required
def agent_productivity_trend():
    """Daily done/failed assignment trend, with per-kind layering."""
    user = get_current_user()
    days = _parse_int_arg("days", 30, 1, 365)
    return ApiResponse.success(productivity_trend(user.id, days)).to_response()


@agents_bp.route("/productivity/alerts", methods=["GET"])
@unified_auth_required
def agent_productivity_alerts():
    """Low-efficiency alerts: completion rate below threshold or failure rate
    above threshold, with at least ``min_assignments`` assignments."""
    user = get_current_user()
    days = _parse_int_arg("days", 30, 1, 365)
    min_completion_rate = _parse_float_arg("min_completion_rate", 50, 0, 100)
    max_failure_rate = _parse_float_arg("max_failure_rate", 30, 0, 100)
    min_assignments = _parse_int_arg("min_assignments", 3, 1, 1000)
    return ApiResponse.success(
        productivity_alerts(user.id, days, min_completion_rate,
                            max_failure_rate, min_assignments)).to_response()


@agents_bp.route("/productivity/by-kind", methods=["GET"])
@unified_auth_required
def agent_productivity_by_kind():
    """Productivity comparison grouped by Agent kind."""
    user = get_current_user()
    days = _parse_int_arg("days", 30, 1, 365)
    return ApiResponse.success(productivity_by_kind(user.id, days)).to_response()


@agents_bp.route("/productivity/hourly-heatmap", methods=["GET"])
@unified_auth_required
def agent_productivity_hourly_heatmap():
    """Hour-of-day × Agent completion heatmap (Python-side hour extraction)."""
    user = get_current_user()
    days = _parse_int_arg("days", 30, 1, 365)
    limit = _parse_int_arg("limit", 15, 1, 50)
    return ApiResponse.success(
        productivity_hourly_heatmap(user.id, days, limit)).to_response()


@agents_bp.route("/productivity/calendar-heatmap", methods=["GET"])
@unified_auth_required
def agent_productivity_calendar_heatmap():
    """Date × Agent completion calendar heatmap (GitHub-style)."""
    user = get_current_user()
    days = _parse_int_arg("days", 90, 1, 365)
    limit = _parse_int_arg("limit", 10, 1, 20)
    return ApiResponse.success(
        productivity_calendar_heatmap(user.id, days, limit)).to_response()


@agents_bp.route("/productivity/weekly-comparison", methods=["GET"])
@unified_auth_required
def agent_productivity_weekly_comparison():
    """Week-over-week done comparison per Agent with change percentage."""
    user = get_current_user()
    limit = _parse_int_arg("limit", 10, 1, 30)
    return ApiResponse.success(
        productivity_weekly_comparison(user.id, limit)).to_response()


@agents_bp.route("/idle-ranking", methods=["GET"])
@unified_auth_required
def agent_idle_ranking():
    """Agents ranked by idle duration with stage classification
    (active/idle/stale/dormant/never)."""
    user = get_current_user()
    limit = _parse_int_arg("limit", 20, 1, 50)
    return ApiResponse.success(idle_ranking(user.id, limit)).to_response()
