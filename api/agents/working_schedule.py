"""Agent working time windows (工作时间区间) endpoints.

GET  /agents/<id>/working-schedule          查看配置 + 当前求值
PUT  /agents/<id>/working-schedule          整体替换配置（服务端校验）
POST /agents/<id>/working-schedule/preview  预览求值（不落库，供编辑器实时反馈）

配置结构与语义见 services/agent_working_schedule.py。
"""

from datetime import datetime

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    db,
    AuditLog,
    Notification,
    get_current_user,
    get_owned_agent_or_response,
    unified_auth_required,
    validate_json_request,
    _client_ip,
    _queue_sse,
)
from services.agent_working_schedule import (
    evaluate_working_window,
    normalize_working_schedule,
)


def _schedule_payload(agent):
    schedule = agent.working_schedule or {}
    return {
        'agent_id': agent.id,
        'working_schedule': schedule,
        'evaluation': evaluate_working_window(schedule),
    }


@agents_bp.route("/<int:agent_id>/working-schedule", methods=["GET"])
@unified_auth_required
def get_working_schedule(agent_id):
    """Get an Agent's working time window config plus its current evaluation."""
    current_user = get_current_user()
    agent, response = get_owned_agent_or_response(agent_id, current_user)
    if response:
        return response
    return ApiResponse.success(_schedule_payload(agent), "Working schedule retrieved").to_response()


@agents_bp.route("/<int:agent_id>/working-schedule", methods=["PUT"])
@unified_auth_required
def update_working_schedule(agent_id):
    """Replace an Agent's working time window config (validated server-side)."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        data = validate_json_request(required_fields=["working_schedule"])
        if isinstance(data, tuple):
            return data

        try:
            schedule = normalize_working_schedule(data["working_schedule"])
        except ValueError as e:
            return ApiResponse.error(f"Invalid working_schedule: {e}", 400).to_response()

        agent.working_schedule = schedule
        db.session.commit()

        # 配置变更走与 update_agent 相同的 SSE + Notification 通知链
        _queue_sse(
            current_user.id,
            "agent_config_changed",
            {"agent_id": agent.id, "agent_name": agent.name, "changed_fields": ["working_schedule"]},
        )
        Notification.create_notification(
            user_id=current_user.id,
            event_type="agent_config_changed",
            agent_id=agent.id,
            payload={"changed_fields": ["working_schedule"]},
        )
        AuditLog.record(
            action="agent.working_schedule_updated",
            resource_type="agent",
            resource_id=agent.id,
            actor_type="human",
            actor_user_id=current_user.id,
            detail={"enabled": schedule.get("enabled", False)},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.success(_schedule_payload(agent), "Working schedule updated").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update working schedule: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/working-schedule/preview", methods=["POST"])
@unified_auth_required
def preview_working_schedule(agent_id):
    """Evaluate a candidate schedule (or the saved one) without persisting.

    Body: {"schedule": {...}, "at": "2026-09-09T12:00:00"(可选, UTC)}
    供编辑器在保存前实时反馈 in_window / next_window_at。
    """
    current_user = get_current_user()
    agent, response = get_owned_agent_or_response(agent_id, current_user)
    if response:
        return response

    data = request.get_json(silent=True) or {}
    raw_schedule = data.get("schedule")
    if raw_schedule is None:
        raw_schedule = agent.working_schedule or {}

    at = None
    if data.get("at"):
        try:
            at = datetime.fromisoformat(str(data["at"]).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return ApiResponse.error("at must be an ISO datetime string", 400).to_response()

    try:
        schedule = normalize_working_schedule(raw_schedule)
    except ValueError as e:
        return ApiResponse.error(f"Invalid working_schedule: {e}", 400).to_response()

    return ApiResponse.success(
        {
            "agent_id": agent.id,
            "working_schedule": schedule,
            "evaluation": evaluate_working_window(schedule, at),
        },
        "Working schedule preview",
    ).to_response()
