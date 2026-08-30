"""
通知外部 provider 适配层
"""

from typing import Any, Dict
import requests

from models import NotificationChannelType


class NotificationProviderError(Exception):
    def __init__(self, message, status_code=None, response_excerpt=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_excerpt = response_excerpt


def render_channel_payload(channel, event_row, delivery_row):
    channel_type = str(channel.channel_type or '').strip().lower()
    payload = event_row.payload or {}

    title = str(payload.get('title') or payload.get('rendered_title') or event_row.event_type)
    body = str(payload.get('body') or payload.get('rendered_body') or '')
    link_url = str(payload.get('link_url') or '')
    actor_name = str(payload.get('actor_name') or '系统')
    event_type = str(event_row.event_type or '')

    if channel_type == NotificationChannelType.WEBHOOK.value:
        return {
            'title': title,
            'text': body,
            'event_type': event_type,
            'event_id': event_row.event_id,
            'link_url': link_url,
            'actor_name': actor_name,
            'resource_type': event_row.resource_type,
            'resource_id': event_row.resource_id,
            'project_id': event_row.project_id,
            'organization_id': event_row.organization_id,
            'payload': payload,
        }

    if channel_type == NotificationChannelType.FEISHU.value:
        text = f'{title}\n{body}'.strip()
        if link_url:
            text = f'{text}\n{link_url}'.strip()
        return {
            'msg_type': 'text',
            'content': {
                'text': text,
            },
        }

    if channel_type == NotificationChannelType.WECOM.value:
        content = f'**{title}**\n{body}'.strip()
        if link_url:
            content = f'{content}\n<{link_url}|查看详情>'
        config = channel.config or {}
        return {
            'msgtype': 'markdown',
            'markdown': {
                'content': content,
            },
            'mentioned_list': config.get('mentioned_list') or [],
            'mentioned_mobile_list': config.get('mentioned_mobile_list') or [],
        }

    if channel_type == NotificationChannelType.DINGTALK.value:
        text = f'#### {title}\n\n{body}'.strip()
        if link_url:
            text = f'{text}\n\n[查看详情]({link_url})'
        config = channel.config or {}
        return {
            'msgtype': 'markdown',
            'markdown': {
                'title': title[:64],
                'text': text,
            },
            'at': {
                'atMobiles': config.get('at_mobiles') or [],
                'isAtAll': False,
            },
        }

    raise NotificationProviderError(f'Unsupported channel_type: {channel_type}')


def send_channel_message(channel, event_row, delivery_row, timeout_seconds=8):
    channel_type = str(channel.channel_type or '').strip().lower()
    config = channel.config or {}
    url = str(config.get('url') or config.get('webhook_url') or '').strip()
    if not url:
        raise NotificationProviderError('Missing webhook url')

    payload = render_channel_payload(channel, event_row, delivery_row)
    headers: Dict[str, Any] = {'Content-Type': 'application/json'}
    if channel_type == NotificationChannelType.WEBHOOK.value:
        for key, value in (config.get('headers') or {}).items():
            header_key = str(key or '').strip()
            if header_key:
                headers[header_key] = str(value or '').strip()

    response = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
    excerpt = (response.text or '')[:1000]
    if response.status_code < 200 or response.status_code >= 300:
        raise NotificationProviderError(
            f'Provider request failed with status {response.status_code}',
            status_code=response.status_code,
            response_excerpt=excerpt,
        )
    return {
        'status_code': response.status_code,
        'response_excerpt': excerpt,
        'request_payload': payload,
    }
