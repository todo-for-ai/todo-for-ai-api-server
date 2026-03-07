"""
Agent Runtime 通知拉取 API
"""

import time
from datetime import datetime
from flask import Blueprint, g

from models import db, AgentNotificationReceipt, NotificationEvent
from .base import ApiResponse, validate_json_request
from .agent_common import agent_session_required, write_agent_audit

agent_runtime_notifications_bp = Blueprint('agent_runtime_notifications', __name__)


def _normalize_int(value, default_value, min_value=None, max_value=None):
    try:
        num = int(value)
    except Exception:
        num = default_value
    if min_value is not None:
        num = max(min_value, num)
    if max_value is not None:
        num = min(max_value, num)
    return num


def _parse_bool(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _build_notification_item(receipt, event_row):
    payload = event_row.payload or {}
    return {
        'receipt_id': receipt.id,
        'event_id': event_row.event_id,
        'event_type': event_row.event_type,
        'category': event_row.category,
        'resource_type': event_row.resource_type,
        'resource_id': event_row.resource_id,
        'project_id': event_row.project_id,
        'organization_id': event_row.organization_id,
        'title': payload.get('rendered_title') or payload.get('title'),
        'body': payload.get('rendered_body') or payload.get('body'),
        'link_url': payload.get('link_url'),
        'payload': payload,
        'created_at': event_row.created_at.isoformat() if event_row.created_at else None,
        'first_pulled_at': receipt.first_pulled_at.isoformat() if receipt.first_pulled_at else None,
        'last_pulled_at': receipt.last_pulled_at.isoformat() if receipt.last_pulled_at else None,
        'acked_at': receipt.acked_at.isoformat() if receipt.acked_at else None,
    }


def _query_receipts(agent_id, include_acked=False, max_items=20):
    query = AgentNotificationReceipt.query.filter_by(agent_id=agent_id)
    if not include_acked:
        query = query.filter(AgentNotificationReceipt.acked_at.is_(None))
    receipts = query.order_by(
        AgentNotificationReceipt.created_at.asc(),
        AgentNotificationReceipt.id.asc(),
    ).limit(max_items).all()

    items = []
    for receipt in receipts:
        event_row = NotificationEvent.query.filter_by(event_id=receipt.event_id).first()
        if not event_row:
            continue
        items.append((receipt, event_row))
    return items


@agent_runtime_notifications_bp.route('/agent/notifications/pull', methods=['POST'])
@agent_session_required
def pull_agent_notifications():
    agent = g.current_agent
    data = validate_json_request(optional_fields=['max_items', 'wait_seconds', 'include_acked'])
    if isinstance(data, tuple):
        return data

    max_items = _normalize_int(data.get('max_items', 20), 20, min_value=1, max_value=100)
    wait_seconds = _normalize_int(data.get('wait_seconds', 0), 0, min_value=0, max_value=20)
    include_acked = _parse_bool(data.get('include_acked'))

    deadline = time.time() + wait_seconds
    matched = []
    while True:
        matched = _query_receipts(agent.id, include_acked=include_acked, max_items=max_items)
        if matched or wait_seconds <= 0 or time.time() >= deadline:
            break
        time.sleep(1)

    now = datetime.utcnow()
    items = []
    for receipt, event_row in matched:
        if not receipt.first_pulled_at:
            receipt.first_pulled_at = now
        receipt.last_pulled_at = now
        items.append(_build_notification_item(receipt, event_row))

    write_agent_audit(
        event_type='agent.notifications.pull',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent_notification_receipt',
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        payload={'count': len(items), 'include_acked': include_acked},
    )
    db.session.commit()

    return ApiResponse.success(
        {'items': items},
        'Agent notifications pulled successfully',
    ).to_response()


@agent_runtime_notifications_bp.route('/agent/notifications/<int:receipt_id>/ack', methods=['POST'])
@agent_session_required
def ack_agent_notification(receipt_id):
    agent = g.current_agent
    receipt = AgentNotificationReceipt.query.filter_by(id=receipt_id, agent_id=agent.id).first()
    if not receipt:
        return ApiResponse.not_found('Notification receipt not found').to_response()

    if not receipt.acked_at:
        receipt.acked_at = datetime.utcnow()

    write_agent_audit(
        event_type='agent.notifications.ack',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent_notification_receipt',
        target_id=receipt.id,
        workspace_id=agent.workspace_id,
        payload={'event_id': receipt.event_id},
    )
    db.session.commit()

    return ApiResponse.success(receipt.to_dict(), 'Agent notification acknowledged successfully').to_response()
