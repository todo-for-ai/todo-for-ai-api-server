"""飞书（Lark）自建应用接入：入站事件 → 平台任务，执行动态 → 群卡片回推。

协议要点：
- 事件订阅 v2：`url_verification` 握手直接回 challenge；业务事件取
  header.event_type == im.message.receive_v1，文本在 event.message.content
  （JSON 字符串 {"text": "@_user_1 帮我建任务"}）。
- 验签：header.token 与连接器 secret（verification_token）常量时间比较，
  fail-closed。
- 群路由：config_json.chats[chat_id] → project_id，缺省回落 default_project_id。
- 回推：tenant_access_token（app_id+app_secret）+ im/v1/messages 交互卡片；
  api_base 可覆盖（本地/私有化/测试 mock）。

secret 字段约定（加密存储的是整个 JSON 或明文 token）：
    {"verification_token": "...", "app_id": "...", "app_secret": "..."}
兼容只填明文 token 的旧格式（此时无法回推，仅入站建任务）。
"""

import hmac
import json
from datetime import datetime

import requests
import structlog

from models import ExternalConnectorConfig, Project, Task, TaskStatus, db
from services.connectors.common import emit_sync_event, find_task, resolve_project
from services.github_app import decrypt_str

logger = structlog.get_logger()

DEFAULT_LARK_API_BASE = 'https://open.feishu.cn'
TASK_TITLE_MAX = 80


# ---------------------------------------------------------------------------
# secret 解析与校验
# ---------------------------------------------------------------------------

def load_secrets(config: ExternalConnectorConfig) -> dict:
    """secret_encrypted → dict；明文 token 兼容为 {'verification_token': token}。"""
    raw = decrypt_str(config.secret_encrypted) if config.secret_encrypted else ''
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith('{'):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {'verification_token': raw}


def verify_lark_token(config: ExternalConnectorConfig, token: str) -> bool:
    expected = load_secrets(config).get('verification_token') or ''
    if not expected:
        return False
    return hmac.compare_digest(str(token or ''), expected)


# ---------------------------------------------------------------------------
# 入站事件处理
# ---------------------------------------------------------------------------

def _extract_text(message: dict) -> str:
    """message.content（JSON 字符串）→ 纯文本，去掉 @占位符。"""
    raw = message.get('content') or ''
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return ''
    text = str(parsed.get('text') or '').strip()
    # 飞书富文本/@ 占位：@_user_1 等
    cleaned = ' '.join(
        seg for seg in (part.strip() for part in text.split('\n'))
        if seg
    )
    import re
    cleaned = re.sub(r'@_user_\d+', '', cleaned)
    return cleaned.strip()


def _resolve_project_for_chat(config: ExternalConnectorConfig, chat_id: str) -> Project:
    extra = config.config_json or {}
    chats = extra.get('chats') or {}
    project_id = chats.get(chat_id) or config.default_project_id
    project = db.session.get(Project, int(project_id)) if project_id else None
    if not project:
        raise ValueError('no project routed for this chat (configure chats mapping or default_project_id)')
    return project


def ingest_lark(workspace_id: int, config: ExternalConnectorConfig, payload: dict) -> dict:
    """处理飞书事件回调；返回 {action: ...}。抛 ValueError 表示请求级错误。"""
    if payload.get('type') == 'url_verification':
        challenge = str(payload.get('challenge') or '')
        if not challenge:
            raise ValueError('missing challenge')
        return {'action': 'challenge', 'challenge': challenge}

    header = payload.get('header') or {}
    if header.get('event_type') != 'im.message.receive_v1':
        return {'action': 'ignored', 'reason': f"event_type={header.get('event_type')}"}

    event = payload.get('event') or {}
    message = event.get('message') or {}
    chat_id = str(message.get('chat_id') or '')
    message_id = str(message.get('message_id') or '')
    text = _extract_text(message)
    if not text:
        return {'action': 'ignored', 'reason': 'empty text'}

    project = _resolve_project_for_chat(config, chat_id)
    external_key = f"lark:{message_id or chat_id + ':' + str(int(datetime.utcnow().timestamp()))}"[:100]

    title = text.replace('\n', ' ').strip()[:TASK_TITLE_MAX] or '[lark] 空标题'
    content = f"{text}\n\n来源: 飞书群 {chat_id}"

    task = find_task(project.id, external_key)
    created = task is None
    if created:
        task = Task(
            project_id=project.id,
            owner_id=project.owner_id,
            title=title,
            content=content,
            status=TaskStatus.TODO,
            is_ai_task=True,
            creator_type='ai',
            creator_identifier=external_key,
            created_by='connector:lark',
        )
        db.session.add(task)
        db.session.flush()
    from models import TaskLog, TaskLogActorType
    db.session.add(TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.SYSTEM,
        content=f"来自飞书消息 {message_id}（{'创建' if created else '幂等命中'}）",
    ))

    emit_sync_event(task, 'connector.lark.ingested', {
        'chat_id': chat_id, 'message_id': message_id,
        'created': created, 'task_id': task.id,
    })
    config.last_synced_at = datetime.utcnow()

    # 回执卡片（失败不阻断入站：建任务已成功）
    reply_error = ''
    try:
        send_lark_task_card(config, chat_id, task, created)
    except Exception as e:  # noqa: BLE001 — 网络错误不能吞掉建任务结果
        reply_error = str(e)
        logger.warning("lark.reply_failed", error=str(e), task_id=task.id)

    return {'action': 'task', 'created': created, 'task_id': task.id,
            'reply_error': reply_error}


# ---------------------------------------------------------------------------
# 出站：tenant token + 卡片消息
# ---------------------------------------------------------------------------

def _api_base(config: ExternalConnectorConfig) -> str:
    return str((config.config_json or {}).get('api_base') or DEFAULT_LARK_API_BASE).rstrip('/')


def get_tenant_access_token(config: ExternalConnectorConfig) -> str:
    secrets = load_secrets(config)
    app_id = secrets.get('app_id') or (config.config_json or {}).get('app_id')
    app_secret = secrets.get('app_secret')
    if not app_id or not app_secret:
        raise ValueError('lark app_id/app_secret not configured')
    resp = requests.post(
        f"{_api_base(config)}/open-apis/auth/v3/tenant_access_token/internal",
        json={'app_id': app_id, 'app_secret': app_secret}, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get('tenant_access_token')
    if not token:
        raise ValueError(f"tenant_token failed: code={data.get('code')} msg={data.get('msg')}")
    return token


def build_task_card(task: Task, created: bool) -> dict:
    state = '✅ 已创建并进入派发队列' if created else '📌 已存在（幂等命中）'
    return {
        'config': {'wide_screen_mode': True},
        'header': {
            'title': {'tag': 'plain_text', 'content': f"任务 #{task.id}{' 已创建' if created else ' 已存在'}"},
            'template': 'green' if created else 'blue',
        },
        'elements': [
            {'tag': 'div', 'text': {
                'tag': 'lark_md',
                'content': f"**{task.title}**\n{state}",
            }},
            {'tag': 'hr'},
            {'tag': 'note', 'elements': [{
                'tag': 'plain_text',
                'content': f"todo-for-ai · {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            }]},
        ],
    }


def send_lark_task_card(config: ExternalConnectorConfig, chat_id: str, task: Task, created: bool) -> dict:
    token = get_tenant_access_token(config)
    resp = requests.post(
        f"{_api_base(config)}/open-apis/im/v1/messages",
        params={'receive_id_type': 'chat_id'},
        headers={'Authorization': f'Bearer {token}'},
        json={'receive_id': chat_id, 'msg_type': 'interactive', 'content': json.dumps(build_task_card(task, created))},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') not in (0, None):
        raise ValueError(f"send card failed: code={data.get('code')} msg={data.get('msg')}")
    return data
