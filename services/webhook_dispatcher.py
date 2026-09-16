"""Webhook 出站派发器：平台事件 → 订阅的外部系统。

设计：
- 订阅匹配：active 且 events 含 '*' 或命中事件类型；
- 签名：X-Todo4AI-Signature: t=<unix秒>,v1=<hex(hmac_sha256(secret, f"{ts}.{body}"))>，
  外加 X-Todo4AI-Event 事件头，接收方可用同样密钥验签防伪造/防重放；
- 重试：最多 3 次（退避 1s/4s），任意一次 2xx 即成功；终态写 WebhookDelivery；
- 异步：请求路径调用 dispatch_event 时丢后台线程（自带 app context + 独立
  session），不阻塞请求也绝不反噬主事务；测试可传 synchronous=True。
"""

import hashlib
import hmac
import json
import time
from datetime import datetime
from threading import Thread

import requests as http_client
import structlog

from models import WebhookDelivery, WebhookSubscription, db
from services.github_app import decrypt_str

logger = structlog.get_logger()

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 4)
TIMEOUT_SECONDS = 10
PAYLOAD_MAX_BYTES = 64 * 1024

def task_snapshot(task) -> dict:
    """任务出站快照：只含对外的稳定字段，避免把内部对象序列化进 webhook。"""
    return {
        'id': task.id,
        'title': task.title,
        'status': task.status.value if task.status else None,
        'priority': task.priority.value if task.priority else None,
        'project_id': task.project_id,
        'is_ai_task': bool(task.is_ai_task),
        'updated_at': task.updated_at.isoformat() if getattr(task, 'updated_at', None) else None,
    }


# 平台当前对外发布的webhook事件类型
WEBHOOK_EVENT_TYPES = (
    'task.created',
    'task.status_changed',
    'task.completed',
    'task.failed',
)


def signing_secret(subscription: WebhookSubscription) -> str:
    return decrypt_str(subscription.secret_encrypted) if subscription.secret_encrypted else ''


def build_signature_header(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def subscription_matches(subscription: WebhookSubscription, event_type: str) -> bool:
    events = subscription.events or []
    return subscription.active and ('*' in events or event_type in events)


def _compact_payload(event_type: str, workspace_id: int, payload: dict) -> bytes:
    body = json.dumps({
        'event': event_type,
        'workspace_id': workspace_id,
        'occurred_at': datetime.utcnow().isoformat() + 'Z',
        'data': payload,
    }, ensure_ascii=False, default=str)
    raw = body.encode('utf-8')
    if len(raw) > PAYLOAD_MAX_BYTES:
        raw = raw[:PAYLOAD_MAX_BYTES]
    return raw


def deliver(subscription: WebhookSubscription, event_type: str, payload: dict) -> WebhookDelivery:
    """同步投递（含重试），返回终态 Delivery 记录（不 commit，由调用方决定）。"""
    secret = signing_secret(subscription)
    body = _compact_payload(event_type, subscription.workspace_id, payload)
    started = time.monotonic()
    last_code, last_error = None, None
    ok = False
    attempt = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        headers = {
            'Content-Type': 'application/json',
            'X-Todo4AI-Event': event_type,
        }
        if secret:
            headers['X-Todo4AI-Signature'] = build_signature_header(
                secret, body, int(time.time()))
        try:
            resp = http_client.post(subscription.url, data=body, headers=headers,
                                    timeout=TIMEOUT_SECONDS)
            last_code = resp.status_code
            if 200 <= resp.status_code < 300:
                ok = True
                last_error = None
                break
            last_error = f"http {resp.status_code}: {resp.text[:200]}"
        except Exception as e:  # noqa: BLE001 — 网络异常也计入重试
            last_error = str(e)[:500]
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])

    duration_ms = int((time.monotonic() - started) * 1000)
    delivery = WebhookDelivery(
        subscription_id=subscription.id,
        event_type=event_type,
        ok=ok,
        status_code=last_code,
        attempts=attempt,
        error=last_error,
        duration_ms=duration_ms,
    )
    return delivery


def deliver_synchronous(subscription: WebhookSubscription, event_type: str, payload: dict) -> WebhookDelivery:
    """测试/手动 ping 用：同步投递 + 落库提交。"""
    delivery = deliver(subscription, event_type, payload)
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _async_deliver(app, subscription_id: int, event_type: str, payload: dict):
    with app.app_context():
        try:
            subscription = db.session.get(WebhookSubscription, subscription_id)
            if not subscription:
                return
            delivery = deliver(subscription, event_type, payload)
            db.session.add(delivery)
            db.session.commit()
            if not delivery.ok:
                logger.warning("webhook.delivery_failed", subscription_id=subscription_id,
                               event_type=event_type, error=delivery.error,
                               attempts=delivery.attempts)
        except Exception as e:  # noqa: BLE001 — 出站失败绝不反噬主流程
            logger.error("webhook.dispatcher_error", error=str(e))


def dispatch_event(workspace_id: int, event_type: str, payload: dict,
                   app=None, synchronous: bool = False) -> int:
    """对 workspace 的所有匹配订阅派发事件；返回派发的订阅数。

    请求路径调用时保持默认异步（后台线程 + 独立 app context + 独立 session），
    绝不阻塞也绝不反噬主事务；synchronous=True 供测试与 ping 使用。"""
    if event_type not in WEBHOOK_EVENT_TYPES:
        logger.debug("webhook.event_not_published", event_type=event_type)
        return 0
    subscriptions = WebhookSubscription.query.filter_by(
        workspace_id=workspace_id, active=True).all()
    matched = [s for s in subscriptions if subscription_matches(s, event_type)]
    if not matched:
        return 0

    for subscription in matched:
        if synchronous:
            deliver_synchronous(subscription, event_type, payload)
        else:
            if app is None:
                from flask import current_app
                app = current_app._get_current_object()
            thread = Thread(
                target=_async_deliver,
                args=(app, subscription.id, event_type, payload),
                daemon=True,
            )
            thread.start()
    return len(matched)
