"""
通知服务
"""

from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set

from models import (
    db,
    NotificationChannel,
    NotificationDelivery,
    NotificationChannelType,
    NotificationEvent,
    Task,
    UserNotification,
    UserSettings,
)
from core.notification_dispatcher import enqueue_pending_deliveries_for_events


TASK_EVENT_SHORT_NAMES = {
    'created',
    'updated',
    'status_changed',
    'completed',
    'assigned',
    'mentioned',
}

SUPPORTED_NOTIFICATION_CHANNEL_TYPES = {
    NotificationChannelType.IN_APP.value,
    NotificationChannelType.WEBHOOK.value,
    NotificationChannelType.FEISHU.value,
    NotificationChannelType.WECOM.value,
    NotificationChannelType.DINGTALK.value,
}

NOTIFICATION_EVENT_CATALOG: List[Dict[str, object]] = [
    {
        'event_type': 'task.created',
        'title': '任务创建',
        'description': '任务被创建时触发',
        'category': 'task',
        'default_level': 'info',
        'supports_in_app': True,
        'supports_external': True,
    },
    {
        'event_type': 'task.updated',
        'title': '任务更新',
        'description': '任务内容发生普通更新时触发',
        'category': 'task',
        'default_level': 'info',
        'supports_in_app': False,
        'supports_external': True,
    },
    {
        'event_type': 'task.status_changed',
        'title': '任务状态变更',
        'description': '任务状态发生变化时触发',
        'category': 'task',
        'default_level': 'info',
        'supports_in_app': False,
        'supports_external': True,
    },
    {
        'event_type': 'task.completed',
        'title': '任务完成',
        'description': '任务被标记为完成时触发',
        'category': 'task',
        'default_level': 'success',
        'supports_in_app': True,
        'supports_external': True,
    },
    {
        'event_type': 'task.assigned',
        'title': '任务分配',
        'description': '你被分配到任务时触发',
        'category': 'task',
        'default_level': 'info',
        'supports_in_app': True,
        'supports_external': True,
    },
    {
        'event_type': 'task.mentioned',
        'title': '任务提及',
        'description': '你在任务中被提及时触发',
        'category': 'task',
        'default_level': 'info',
        'supports_in_app': True,
        'supports_external': True,
    },
]

SUPPORTED_NOTIFICATION_EVENT_TYPES = {item['event_type'] for item in NOTIFICATION_EVENT_CATALOG}
SECRET_CONFIG_KEYS = {'secret', 'sign_secret', 'access_token'}
MASKED_HEADER_KEYS = {'authorization', 'x-api-key', 'x-token', 'token'}


def get_notification_event_catalog():
    return [dict(item) for item in NOTIFICATION_EVENT_CATALOG]


def normalize_notification_event_types(events):
    normalized = []
    seen = set()
    for item in (events or []):
        event_name = str(item or '').strip().lower()
        if not event_name:
            continue
        if '.' not in event_name and event_name in TASK_EVENT_SHORT_NAMES:
            event_name = f'task.{event_name}'
        if event_name not in SUPPORTED_NOTIFICATION_EVENT_TYPES:
            return None
        if event_name in seen:
            continue
        seen.add(event_name)
        normalized.append(event_name)
    return normalized


def validate_notification_channel_config(channel_type, raw_config):
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        return None, 'config must be object'

    channel_type_value = str(channel_type or '').strip().lower()
    if channel_type_value not in SUPPORTED_NOTIFICATION_CHANNEL_TYPES:
        return None, 'Invalid channel_type'

    if channel_type_value == NotificationChannelType.IN_APP.value:
        return {}, None

    webhook_url = str(raw_config.get('webhook_url') or raw_config.get('url') or '').strip()
    if not webhook_url.startswith('http://') and not webhook_url.startswith('https://'):
        return None, f'{channel_type_value} webhook url must be http/https url'

    if channel_type_value == NotificationChannelType.WEBHOOK.value:
        headers = raw_config.get('headers') or {}
        if not isinstance(headers, dict):
            return None, 'webhook config.headers must be object'
        sanitized_headers = {}
        for key, value in headers.items():
            header_key = str(key or '').strip()
            if not header_key:
                continue
            sanitized_headers[header_key] = str(value or '').strip()
        return {
            'url': webhook_url,
            'headers': sanitized_headers,
        }, None

    if channel_type_value == NotificationChannelType.FEISHU.value:
        sanitized = {'webhook_url': webhook_url}
        secret = str(raw_config.get('secret') or '').strip()
        if secret:
            sanitized['secret'] = secret
        return sanitized, None

    if channel_type_value == NotificationChannelType.WECOM.value:
        return {
            'webhook_url': webhook_url,
            'mentioned_list': _normalize_string_list(raw_config.get('mentioned_list')),
            'mentioned_mobile_list': _normalize_string_list(raw_config.get('mentioned_mobile_list')),
        }, None

    if channel_type_value == NotificationChannelType.DINGTALK.value:
        sanitized = {
            'webhook_url': webhook_url,
            'at_mobiles': _normalize_string_list(raw_config.get('at_mobiles')),
        }
        secret = str(raw_config.get('secret') or '').strip()
        if secret:
            sanitized['secret'] = secret
        return sanitized, None

    return dict(raw_config), None


def serialize_notification_channel(row):
    data = row.to_dict()
    data['events'] = normalize_notification_event_types(data.get('events') or []) or []
    data['config'] = _mask_notification_channel_config(data.get('config') or {})
    return data


def create_task_notifications(task: Task,
                              event_type: str,
                              actor_user=None,
                              payload: Optional[Dict] = None,
                              event_id: Optional[str] = None,
                              previous_assignees: Optional[Sequence[Dict]] = None,
                              previous_mentions: Optional[Sequence[Dict]] = None):
    event_name = str(event_type or '').strip().lower()
    if '.' not in event_name:
        event_name = f'task.{event_name}'
    if event_name not in SUPPORTED_NOTIFICATION_EVENT_TYPES:
        return []

    payload = dict(payload or {})
    recipient_user_ids = _resolve_task_recipient_user_ids(
        task,
        event_name,
        actor_user=actor_user,
        previous_assignees=previous_assignees,
        previous_mentions=previous_mentions,
    )
    title, body, level = _build_task_notification_content(task, event_name, actor_user)
    link_url = f'/todo-for-ai/pages/tasks/{task.id}'
    actor_name = _display_actor(actor_user)

    event_payload = dict(payload)
    event_payload.update({
        'title': title,
        'body': body,
        'rendered_title': title,
        'rendered_body': body,
        'level': level,
        'link_url': link_url,
        'actor_name': actor_name,
    })

    event_row = ensure_notification_event(
        event_id=event_id,
        event_type=event_name,
        category='task',
        actor_user_id=getattr(actor_user, 'id', None),
        resource_type='task',
        resource_id=task.id,
        project_id=task.project_id,
        organization_id=getattr(task.project, 'organization_id', None),
        payload=event_payload,
        target_user_ids=recipient_user_ids,
        created_by=getattr(actor_user, 'email', None),
    )

    created_rows = []
    for user_id in recipient_user_ids:
        if not _is_in_app_notification_enabled(user_id, event_name):
            continue

        dedup_key = _build_task_notification_dedup_key(task, event_name, user_id)
        exists = UserNotification.query.filter_by(dedup_key=dedup_key).first()
        if exists:
            created_rows.append(exists)
            continue

        row = UserNotification(
            user_id=user_id,
            event_id=event_row.event_id,
            event_type=event_name,
            category='task',
            title=title,
            body=body,
            level=level,
            link_url=link_url,
            resource_type='task',
            resource_id=task.id,
            actor_user_id=getattr(actor_user, 'id', None),
            project_id=task.project_id,
            organization_id=getattr(task.project, 'organization_id', None),
            extra_payload=event_payload,
            dedup_key=dedup_key,
            created_by=getattr(actor_user, 'email', None),
        )
        db.session.add(row)
        created_rows.append(row)

    event_row.in_app_processed_at = datetime.utcnow()
    _ensure_external_delivery_rows(
        event_row=event_row,
        task=task,
        recipient_user_ids=recipient_user_ids,
        actor_user=actor_user,
    )
    return created_rows


def ensure_notification_event(event_id,
                              event_type,
                              category,
                              actor_user_id=None,
                              resource_type='task',
                              resource_id=None,
                              project_id=None,
                              organization_id=None,
                              payload=None,
                              target_user_ids=None,
                              created_by=None):
    if not event_id:
        return None

    row = NotificationEvent.query.filter_by(event_id=event_id).first()
    if row:
        row.event_type = event_type
        row.category = category
        row.actor_user_id = actor_user_id
        row.resource_type = resource_type
        row.resource_id = resource_id
        row.project_id = project_id
        row.organization_id = organization_id
        row.payload = payload or {}
        row.target_user_ids = target_user_ids or []
        return row

    row = NotificationEvent(
        event_id=event_id,
        event_type=event_type,
        category=category,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        organization_id=organization_id,
        payload=payload or {},
        target_user_ids=target_user_ids or [],
        dispatch_state='pending',
        created_by=created_by,
    )
    db.session.add(row)
    return row


def _normalize_string_list(value):
    if not value:
        return []
    result = []
    seen = set()
    for item in value:
        item_str = str(item or '').strip()
        if not item_str or item_str in seen:
            continue
        seen.add(item_str)
        result.append(item_str)
    return result


def _mask_notification_channel_config(config):
    masked = dict(config or {})
    if 'headers' in masked and isinstance(masked['headers'], dict):
        headers = {}
        for key, value in masked['headers'].items():
            header_key = str(key or '')
            if header_key.strip().lower() in MASKED_HEADER_KEYS:
                headers[header_key] = '******'
            else:
                headers[header_key] = value
        masked['headers'] = headers

    for key in list(masked.keys()):
        if str(key).strip().lower() in SECRET_CONFIG_KEYS and masked.get(key):
            masked[key] = '******'
    return masked


def _participant_human_user_ids(items):
    user_ids = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('type') or '').strip().lower() != 'human':
            continue
        try:
            user_ids.add(int(item.get('id')))
        except Exception:
            continue
    return user_ids


def _resolve_task_recipient_user_ids(task,
                                     event_name,
                                     actor_user=None,
                                     previous_assignees=None,
                                     previous_mentions=None):
    current_assignee_ids = _participant_human_user_ids(task.assignees)
    current_mention_ids = _participant_human_user_ids(task.mentions)
    previous_assignee_ids = _participant_human_user_ids(previous_assignees)
    previous_mention_ids = _participant_human_user_ids(previous_mentions)

    recipient_user_ids: Set[int] = set()
    project_owner_id = getattr(task.project, 'owner_id', None) or getattr(task, 'owner_id', None)

    if event_name == 'task.assigned':
        recipient_user_ids.update(current_assignee_ids - previous_assignee_ids)
    elif event_name == 'task.mentioned':
        recipient_user_ids.update(current_mention_ids - previous_mention_ids)
    elif event_name == 'task.created':
        recipient_user_ids.update(current_assignee_ids)
        recipient_user_ids.update(current_mention_ids)
        if project_owner_id:
            recipient_user_ids.add(project_owner_id)
    elif event_name in {'task.completed', 'task.status_changed'}:
        recipient_user_ids.update(current_assignee_ids)
        recipient_user_ids.update(current_mention_ids)
        if project_owner_id:
            recipient_user_ids.add(project_owner_id)
        if getattr(task, 'creator_id', None):
            recipient_user_ids.add(task.creator_id)

    actor_user_id = getattr(actor_user, 'id', None)
    if actor_user_id:
        recipient_user_ids.discard(actor_user_id)

    return sorted(recipient_user_ids)


def _display_actor(actor_user):
    if not actor_user:
        return '有成员'
    return (
        getattr(actor_user, 'full_name', None)
        or getattr(actor_user, 'nickname', None)
        or getattr(actor_user, 'username', None)
        or getattr(actor_user, 'email', None)
        or '有成员'
    )


def _build_task_notification_content(task, event_name, actor_user):
    actor_name = _display_actor(actor_user)
    task_title = getattr(task, 'title', None) or f'任务 #{task.id}'

    if event_name == 'task.created':
        return f'新任务：{task_title}', f'{actor_name} 创建了任务', 'info'
    if event_name == 'task.completed':
        return f'任务已完成：{task_title}', f'{actor_name} 将任务标记为已完成', 'success'
    if event_name == 'task.assigned':
        return f'你被分配到任务：{task_title}', f'{actor_name} 将你加入任务协作人', 'info'
    if event_name == 'task.mentioned':
        return f'你在任务中被提及：{task_title}', f'{actor_name} 在任务中提及了你', 'info'
    if event_name == 'task.status_changed':
        return f'任务状态更新：{task_title}', f'{actor_name} 更新了任务状态', 'info'
    return f'任务更新：{task_title}', f'{actor_name} 更新了任务', 'info'


def _build_task_notification_dedup_key(task, event_name, user_id):
    revision = getattr(task, 'revision', None) or 1
    return f'task:{task.id}:{event_name}:rev:{revision}:user:{int(user_id)}'


def _is_in_app_notification_enabled(user_id, event_name):
    settings = UserSettings.query.filter_by(user_id=user_id).first()
    if not settings or not settings.settings_data:
        return True

    prefs = settings.settings_data.get('notification_preferences') or {}
    if prefs.get('in_app_enabled') is False:
        return False

    disabled_events = normalize_notification_event_types(prefs.get('disabled_events') or [])
    if disabled_events and event_name in disabled_events:
        return False
    return True


def _event_matches(channel_events, event_name):
    normalized_events = normalize_notification_event_types(channel_events or [])
    if normalized_events is None:
        normalized_events = []
    if not normalized_events:
        return True
    return event_name in normalized_events


def _query_scope_channels(scope_type, scope_id, event_name):
    if not scope_id:
        return []
    rows = NotificationChannel.query.filter_by(
        scope_type=scope_type,
        scope_id=scope_id,
        enabled=True,
    ).all()
    return [
        row for row in rows
        if str(row.channel_type).strip().lower() != NotificationChannelType.IN_APP.value
        and _event_matches(row.events, event_name)
    ]


def _ensure_external_delivery_rows(event_row, task, recipient_user_ids, actor_user=None):
    channel_map = {}
    for row in _query_scope_channels('project', task.project_id, event_row.event_type):
        channel_map[row.id] = row

    organization_id = getattr(task.project, 'organization_id', None)
    for row in _query_scope_channels('organization', organization_id, event_row.event_type):
        channel_map[row.id] = row

    for user_id in recipient_user_ids:
        for row in _query_scope_channels('user', user_id, event_row.event_type):
            channel_map[row.id] = row

    for channel in channel_map.values():
        exists = NotificationDelivery.query.filter_by(
            event_id=event_row.event_id,
            channel_id=channel.id,
        ).first()
        if exists:
            continue
        db.session.add(NotificationDelivery(
            event_type=event_row.event_type,
            event_id=event_row.event_id,
            channel_id=channel.id,
            status='pending',
            attempts=0,
            created_by=getattr(actor_user, 'email', None),
        ))
