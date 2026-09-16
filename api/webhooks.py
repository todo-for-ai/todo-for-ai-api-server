"""出站 Webhook 订阅中心 API。

- GET    /workspaces/<ws>/webhooks                       订阅列表（secret 不回显）
- POST   /workspaces/<ws>/webhooks                       创建（secret 仅创建响应返回一次）
- PUT    /workspaces/<ws>/webhooks/<id>                  更新（url/events/active/description/secret）
- DELETE /workspaces/<ws>/webhooks/<id>                  删除（级联删除派发记录）
- GET    /workspaces/<ws>/webhooks/<id>/deliveries       最近派发记录（可观测）
- POST   /workspaces/<ws>/webhooks/<id>/ping             发签名测试事件（synchronous）
"""

from flask import Blueprint

from core.auth import get_current_user, unified_auth_required
from models import WebhookDelivery, WebhookSubscription, db
from services.github_app import encrypt_str
from services.webhook_dispatcher import WEBHOOK_EVENT_TYPES, deliver_synchronous
from .agent_common import ensure_workspace_manage_access, get_workspace_or_404, write_agent_audit
from .base import ApiResponse, validate_json_request

webhooks_bp = Blueprint('webhooks', __name__)


def _get_owned_subscription(workspace_id: int, subscription_id: int):
    return WebhookSubscription.query.filter_by(
        id=subscription_id, workspace_id=workspace_id).first()


def _validate_events(events):
    if not isinstance(events, list) or not events:
        return None, 'events must be a non-empty array'
    normalized = []
    for item in events:
        event = str(item or '').strip()
        if event != '*' and event not in WEBHOOK_EVENT_TYPES:
            return None, f"unknown event type: {event!r} (allowed: {', '.join(WEBHOOK_EVENT_TYPES)}, or '*')"
        normalized.append(event)
    return normalized, None


def _validate_url(url):
    url = str(url or '').strip()
    if not url.startswith(('http://', 'https://')) or len(url) > 512:
        return None
    return url


@webhooks_bp.route('/workspaces/<int:workspace_id>/webhooks', methods=['GET'])
@unified_auth_required
def list_webhooks(workspace_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    subs = WebhookSubscription.query.filter_by(workspace_id=workspace_id).all()
    return ApiResponse.success(data={'items': [s.to_dict() for s in subs]}).to_response()


@webhooks_bp.route('/workspaces/<int:workspace_id>/webhooks', methods=['POST'])
@unified_auth_required
def create_webhook(workspace_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = validate_json_request(required_fields=['url', 'events'],
                                 optional_fields=['secret', 'description'])
    if isinstance(data, tuple):
        return data

    url = _validate_url(data.get('url'))
    if not url:
        return ApiResponse.error('url must be http(s) and <= 512 chars', 400).to_response()
    events, err = _validate_events(data.get('events'))
    if err:
        return ApiResponse.error(err, 400).to_response()
    secret = str(data.get('secret') or '').strip()
    if not secret:
        import secrets as _secrets
        secret = _secrets.token_hex(32)

    subscription = WebhookSubscription(
        workspace_id=workspace_id,
        url=url,
        events=events,
        secret_encrypted=encrypt_str(secret),
        active=True,
        description=str(data.get('description') or '')[:200],
    )
    db.session.add(subscription)
    db.session.commit()
    write_agent_audit(
        event_type='webhook.created',
        actor_type='user',
        actor_id=user.id,
        target_type='webhook',
        target_id=subscription.id,
        workspace_id=workspace_id,
        payload={'url': url, 'events': events},
        risk_score=20,
    )
    return ApiResponse.success(
        data={'subscription': subscription.to_dict(), 'secret': secret},
        message='Webhook created（secret 仅此一次返回，请妥善保存）',
    ).to_response()


@webhooks_bp.route('/workspaces/<int:workspace_id>/webhooks/<int:subscription_id>', methods=['PUT'])
@unified_auth_required
def update_webhook(workspace_id: int, subscription_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    subscription = _get_owned_subscription(workspace_id, subscription_id)
    if not subscription:
        return ApiResponse.error('webhook not found', 404).to_response()

    data = validate_json_request(
        optional_fields=['url', 'events', 'active', 'description', 'secret'])
    if isinstance(data, tuple):
        return data

    if 'url' in data:
        url = _validate_url(data.get('url'))
        if not url:
            return ApiResponse.error('url must be http(s) and <= 512 chars', 400).to_response()
        subscription.url = url
    if 'events' in data:
        events, err = _validate_events(data.get('events'))
        if err:
            return ApiResponse.error(err, 400).to_response()
        subscription.events = events
    if 'active' in data:
        subscription.active = bool(data['active'])
    if 'description' in data:
        subscription.description = str(data.get('description') or '')[:200]
    if data.get('secret'):
        subscription.secret_encrypted = encrypt_str(str(data['secret']))
    db.session.commit()
    write_agent_audit(
        event_type='webhook.updated',
        actor_type='user',
        actor_id=user.id,
        target_type='webhook',
        target_id=subscription.id,
        workspace_id=workspace_id,
        payload={'active': subscription.active},
    )
    return ApiResponse.success(data={'subscription': subscription.to_dict()},
                               message='Webhook updated').to_response()


@webhooks_bp.route('/workspaces/<int:workspace_id>/webhooks/<int:subscription_id>', methods=['DELETE'])
@unified_auth_required
def delete_webhook(workspace_id: int, subscription_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    subscription = _get_owned_subscription(workspace_id, subscription_id)
    if not subscription:
        return ApiResponse.error('webhook not found', 404).to_response()
    WebhookDelivery.query.filter_by(subscription_id=subscription.id).delete()
    db.session.delete(subscription)
    db.session.commit()
    write_agent_audit(
        event_type='webhook.deleted',
        actor_type='user',
        actor_id=user.id,
        target_type='webhook',
        target_id=subscription_id,
        workspace_id=workspace_id,
        payload={},
    )
    return ApiResponse.success(message='Webhook deleted').to_response()


@webhooks_bp.route('/workspaces/<int:workspace_id>/webhooks/<int:subscription_id>/deliveries', methods=['GET'])
@unified_auth_required
def list_deliveries(workspace_id: int, subscription_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    subscription = _get_owned_subscription(workspace_id, subscription_id)
    if not subscription:
        return ApiResponse.error('webhook not found', 404).to_response()
    deliveries = (WebhookDelivery.query.filter_by(subscription_id=subscription.id)
                  .order_by(WebhookDelivery.id.desc()).limit(50).all())
    return ApiResponse.success(data={'items': [d.to_dict() for d in deliveries]}).to_response()


@webhooks_bp.route('/workspaces/<int:workspace_id>/webhooks/<int:subscription_id>/ping', methods=['POST'])
@unified_auth_required
def ping_webhook(workspace_id: int, subscription_id: int):
    """发一条签名测试事件（同步），把投递结果直接返回给调用方。"""
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    subscription = _get_owned_subscription(workspace_id, subscription_id)
    if not subscription:
        return ApiResponse.error('webhook not found', 404).to_response()
    delivery = deliver_synchronous(subscription, 'task.created', {'ping': True})
    return ApiResponse.success(
        data={'delivery': delivery.to_dict()},
        message='Ping delivered' if delivery.ok else 'Ping failed',
    ).to_response()
