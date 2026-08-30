"""Organization event recording utilities."""

from datetime import datetime
from typing import Any, Dict, Optional

from flask import request

from models import db, OrganizationEvent


def _safe_text(value: Any, max_length: int = 200) -> str:
    text = str(value or '').strip()
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}..."


def _safe_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def record_organization_event(
    organization_id: Optional[int],
    event_type: str,
    actor_type: Optional[str] = None,
    actor_id: Optional[Any] = None,
    actor_name: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    message: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    source: str = 'api',
    level: str = 'info',
    occurred_at: Optional[datetime] = None,
    created_by: Optional[str] = None,
):
    if not organization_id:
        return None

    payload_data: Dict[str, Any] = dict(payload or {}) if isinstance(payload, dict) else {}
    if payload and not isinstance(payload, dict):
        payload_data['raw_payload'] = str(payload)

    event = OrganizationEvent(
        organization_id=int(organization_id),
        event_type=str(event_type or '').strip(),
        source=str(source or 'api').strip() or 'api',
        level=str(level or 'info').strip() or 'info',
        actor_type=_safe_text(actor_type or '', 32) or None,
        actor_id=_safe_id(actor_id),
        actor_name=_safe_text(actor_name or '', 128) or None,
        target_type=_safe_text(target_type or '', 32) or None,
        target_id=_safe_id(target_id),
        project_id=project_id,
        task_id=task_id,
        message=_safe_text(message or '', 512) or None,
        payload=payload_data,
        occurred_at=occurred_at or datetime.utcnow(),
        created_by=created_by,
    )
    if not event.actor_name and event.actor_id:
        event.actor_name = event.actor_id

    try:
        event.payload = {**payload_data, 'ip': request.remote_addr}
    except Exception:
        event.payload = payload_data

    db.session.add(event)
    return event
