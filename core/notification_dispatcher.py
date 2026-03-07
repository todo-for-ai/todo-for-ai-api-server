"""
通知投递调度器
"""

import os
import socket
from datetime import datetime, timedelta
from sqlalchemy import or_

from models import db, NotificationDelivery, NotificationDeliveryStatus, NotificationEvent, NotificationChannel
from core.notification_queue import (
    acquire_delivery_lock,
    enqueue_deliveries,
    pop_delivery,
    promote_due_retries,
    release_delivery_lock,
    schedule_delivery_retry,
)
from core.notification_providers import NotificationProviderError, send_channel_message

MAX_RETRY_ATTEMPTS = int(os.environ.get('NOTIFICATION_MAX_RETRY_ATTEMPTS', '5'))
RETRY_BACKOFF_SCHEDULE_SECONDS = [30, 120, 300, 900, 1800]
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _compute_retry_time(attempts):
    index = max(0, min(attempts - 1, len(RETRY_BACKOFF_SCHEDULE_SECONDS) - 1))
    return datetime.utcnow() + timedelta(seconds=RETRY_BACKOFF_SCHEDULE_SECONDS[index])


def enqueue_pending_deliveries_for_events(event_ids):
    normalized = [str(item).strip() for item in (event_ids or []) if str(item).strip()]
    if not normalized:
        return []

    rows = NotificationDelivery.query.filter(
        NotificationDelivery.event_id.in_(normalized),
        NotificationDelivery.status.in_([
            NotificationDeliveryStatus.PENDING.value,
            NotificationDeliveryStatus.RETRYING.value,
        ]),
    ).all()
    delivery_ids = [row.id for row in rows if row.id]
    if delivery_ids:
        enqueue_deliveries(delivery_ids)

    event_rows = NotificationEvent.query.filter(NotificationEvent.event_id.in_(normalized)).all()
    now = datetime.utcnow()
    for row in event_rows:
        row.external_queued_at = now
        if row.dispatch_state in {'pending', 'failed'}:
            row.dispatch_state = 'queued'
    if event_rows:
        db.session.commit()
    return delivery_ids


def scan_due_pending_delivery_ids(limit=200):
    now = datetime.utcnow()
    rows = NotificationDelivery.query.filter(
        NotificationDelivery.status.in_([
            NotificationDeliveryStatus.PENDING.value,
            NotificationDeliveryStatus.RETRYING.value,
        ]),
        or_(
            NotificationDelivery.next_retry_at.is_(None),
            NotificationDelivery.next_retry_at <= now,
        ),
    ).order_by(
        NotificationDelivery.created_at.asc(),
        NotificationDelivery.id.asc(),
    ).limit(limit).all()
    return [row.id for row in rows if row.id]


def _refresh_event_state(event_id):
    event_row = NotificationEvent.query.filter_by(event_id=event_id).first()
    if not event_row:
        return

    deliveries = NotificationDelivery.query.filter_by(event_id=event_id).all()
    if not deliveries:
        event_row.dispatch_state = 'completed'
        return

    statuses = {str(item.status or '').strip().lower() for item in deliveries}
    if statuses == {NotificationDeliveryStatus.SENT.value}:
        event_row.dispatch_state = 'completed'
    elif NotificationDeliveryStatus.DEAD.value in statuses and len(statuses) == 1:
        event_row.dispatch_state = 'failed'
    elif NotificationDeliveryStatus.RETRYING.value in statuses or NotificationDeliveryStatus.PENDING.value in statuses:
        event_row.dispatch_state = 'queued'
    else:
        event_row.dispatch_state = 'partial'
    event_row.external_last_dispatched_at = datetime.utcnow()


def process_delivery(delivery_id, worker_id=None):
    worker_id = worker_id or WORKER_ID
    if not acquire_delivery_lock(delivery_id, worker_id):
        return {'status': 'locked'}

    try:
        delivery = NotificationDelivery.query.get(delivery_id)
        if not delivery:
            return {'status': 'missing'}

        if str(delivery.status or '').strip().lower() == NotificationDeliveryStatus.SENT.value:
            return {'status': 'already_sent'}

        now = datetime.utcnow()
        if delivery.next_retry_at and delivery.next_retry_at > now:
            return {'status': 'waiting_retry'}

        channel = NotificationChannel.query.get(delivery.channel_id)
        event_row = NotificationEvent.query.filter_by(event_id=delivery.event_id).first()
        if not channel or not event_row:
            delivery.status = NotificationDeliveryStatus.DEAD.value
            delivery.response_excerpt = 'Missing channel or notification event'
            _refresh_event_state(delivery.event_id)
            db.session.commit()
            return {'status': 'dead_missing_dependency'}

        delivery.attempts = int(delivery.attempts or 0) + 1

        try:
            result = send_channel_message(channel, event_row, delivery)
            delivery.status = NotificationDeliveryStatus.SENT.value
            delivery.next_retry_at = None
            delivery.response_code = result.get('status_code')
            delivery.response_excerpt = result.get('response_excerpt')
            delivery.request_payload = result.get('request_payload')
            delivery.delivered_at = now
            _refresh_event_state(delivery.event_id)
            db.session.commit()
            return {'status': 'sent', 'response_code': delivery.response_code}
        except NotificationProviderError as error:
            delivery.response_code = error.status_code
            delivery.response_excerpt = error.response_excerpt or str(error)
            delivery.last_error_at = now
            if delivery.attempts >= MAX_RETRY_ATTEMPTS:
                delivery.status = NotificationDeliveryStatus.DEAD.value
                _refresh_event_state(delivery.event_id)
                db.session.commit()
                return {'status': 'dead', 'reason': str(error)}

            retry_at = _compute_retry_time(delivery.attempts)
            delivery.status = NotificationDeliveryStatus.RETRYING.value
            delivery.next_retry_at = retry_at
            _refresh_event_state(delivery.event_id)
            db.session.commit()
            schedule_delivery_retry(delivery.id, retry_at)
            return {'status': 'retrying', 'retry_at': retry_at.isoformat(), 'reason': str(error)}
    finally:
        release_delivery_lock(delivery_id, worker_id)


def dispatch_once(timeout_seconds=5):
    promote_due_retries()
    delivery_id = pop_delivery(timeout_seconds=timeout_seconds)
    if delivery_id is None:
        due_ids = scan_due_pending_delivery_ids(limit=100)
        if due_ids:
            enqueue_deliveries(due_ids)
            promote_due_retries()
            delivery_id = pop_delivery(timeout_seconds=1)
    if delivery_id is None:
        return {'status': 'idle'}
    return process_delivery(delivery_id)


def dispatch_batch(max_items=50):
    results = []
    promote_due_retries(limit=max_items)
    for _ in range(max_items):
        result = dispatch_once(timeout_seconds=1)
        results.append(result)
        if result.get('status') == 'idle':
            break
    return results
