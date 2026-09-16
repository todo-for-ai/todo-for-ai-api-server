"""企业微信自建应用接入：回调验证/消息入站 → 平台任务，应用消息回推。

协议要点：
- 回调配置验证（GET）：echostr 解密成功即回发明文（平台 route 返回）；
- 消息回调（POST）：XML{Encrypt, MsgSignature, Timestamp, Nonce}，按官方
  SHA1 签名 + AES-256-CBC 解密；文本消息 → 建任务（按 from_user/群路由）；
- secret 字段约定（加密存储）：{"token": "...", "encoding_aes_key": "...",
  "corp_secret": "..."}；config_json：{corp_id, agent_id, api_base?, chats?}。
- 回推：/cgi-bin/message/send（access_token = corp_id + corp_secret），
  api_base 可覆盖（私有化/测试 mock）。
"""

from datetime import datetime

import requests
import structlog

from models import ExternalConnectorConfig, Project, Task, TaskLog, TaskLogActorType, TaskStatus, db
from services.connectors.common import emit_sync_event, find_task
from services.github_app import decrypt_str
from services import wecom_crypto as crypto

logger = structlog.get_logger()

DEFAULT_WECOM_API_BASE = 'https://qyapi.weixin.qq.com'
TASK_TITLE_MAX = 80


def load_secrets(config: ExternalConnectorConfig) -> dict:
    raw = decrypt_str(config.secret_encrypted) if config.secret_encrypted else ''
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith('{'):
        try:
            import json
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {'token': raw}


def _api_base(config: ExternalConnectorConfig) -> str:
    return str((config.config_json or {}).get('api_base') or DEFAULT_WECOM_API_BASE).rstrip('/')


def verify_callback(config: ExternalConnectorConfig, params: dict, body_xml: str = '') -> dict:
    """GET 验证（echostr）或 POST 消息的统一验签+解密入口。

    params: msg_signature/timestamp/nonce/echostr(GET)；body_xml 为 POST 体。
    返回 {'action': 'echo', 'plain': ...} 或 {'action': 'message', 'fields': {...}}。
    抛 WeComCryptoError 表示验签失败（route 层转 400/401）。
    """
    secrets = load_secrets(config)
    token = secrets.get('token') or ''
    aes_key = secrets.get('encoding_aes_key') or ''
    if not token or not aes_key:
        raise crypto.WeComCryptoError('wecom token/encoding_aes_key not configured')

    if body_xml:
        fields = crypto.parse_callback_xml(body_xml)
        encrypt = fields.get('Encrypt') or ''
        crypto.verify_signature(token, params.get('timestamp') or '', params.get('nonce') or '',
                                encrypt, params.get('msg_signature') or '')
        plain_xml = crypto.decrypt_message(aes_key, encrypt)
        inner = crypto.parse_callback_xml(plain_xml)
        return {'action': 'message', 'fields': inner}

    echostr = params.get('echostr') or ''
    crypto.verify_signature(token, params.get('timestamp') or '', params.get('nonce') or '',
                            echostr, params.get('msg_signature') or '')
    return {'action': 'echo', 'plain': crypto.decrypt_message(aes_key, echostr)}


def _resolve_project(config: ExternalConnectorConfig, fields: dict) -> Project:
    extra = config.config_json or {}
    chats = extra.get('chats') or {}
    project_id = chats.get(str(fields.get('FromUserName') or '')) or config.default_project_id
    project = db.session.get(Project, int(project_id)) if project_id else None
    if not project:
        raise ValueError('no project routed (configure chats mapping or default_project_id)')
    return project


def ingest_message(workspace_id: int, config: ExternalConnectorConfig, fields: dict) -> dict:
    """文本消息 → 建任务。返回 {action, created, task_id, reply_error}。"""
    if str(fields.get('MsgType') or '') != 'text':
        return {'action': 'ignored', 'reason': f"msg_type={fields.get('MsgType')}"}
    text = str(fields.get('Content') or '').strip()
    if not text:
        return {'action': 'ignored', 'reason': 'empty text'}

    from_user = str(fields.get('FromUserName') or '')
    project = _resolve_project(config, fields)
    external_key = f"wecom:{fields.get('MsgId') or (from_user + ':' + str(int(datetime.utcnow().timestamp())))}"[:100]

    title = text.replace('\n', ' ').strip()[:TASK_TITLE_MAX] or '[wecom] 空标题'
    task = find_task(project.id, external_key)
    created = task is None
    if created:
        task = Task(
            project_id=project.id,
            owner_id=project.owner_id,
            title=title,
            content=f"{text}\n\n来源: 企业微信用户 {from_user}",
            status=TaskStatus.TODO,
            is_ai_task=True,
            creator_type='ai',
            creator_identifier=external_key,
            created_by='connector:wecom',
        )
        db.session.add(task)
        db.session.flush()
    db.session.add(TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.SYSTEM,
        content=f"来自企业微信消息 {fields.get('MsgId')}（{'创建' if created else '幂等命中'}）",
    ))
    emit_sync_event(task, 'connector.wecom.ingested', {
        'from_user': from_user, 'created': created, 'task_id': task.id,
    })
    config.last_synced_at = datetime.utcnow()

    reply_error = ''
    try:
        send_wecom_task_notify(config, from_user, task, created)
    except Exception as e:  # noqa: BLE001
        reply_error = str(e)
        logger.warning("wecom.reply_failed", error=str(e), task_id=task.id)
    return {'action': 'task', 'created': created, 'task_id': task.id, 'reply_error': reply_error}


# ---------------------------------------------------------------------------
# 出站：access_token + 应用消息
# ---------------------------------------------------------------------------

def get_access_token(config: ExternalConnectorConfig) -> str:
    secrets = load_secrets(config)
    corp_id = (config.config_json or {}).get('corp_id')
    corp_secret = secrets.get('corp_secret')
    if not corp_id or not corp_secret:
        raise ValueError('wecom corp_id/corp_secret not configured')
    resp = requests.get(
        f"{_api_base(config)}/cgi-bin/gettoken",
        params={'corpid': corp_id, 'corpsecret': corp_secret}, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get('access_token')
    if not token:
        raise ValueError(f"gettoken failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    return token


def send_wecom_task_notify(config: ExternalConnectorConfig, to_user: str, task: Task, created: bool) -> dict:
    extra = config.config_json or {}
    agent_id = extra.get('agent_id')
    if not agent_id:
        raise ValueError('wecom agent_id not configured')
    token = get_access_token(config)
    payload = {
        'touser': to_user or '@all',
        'msgtype': 'text',
        'agentid': int(agent_id),
        'text': {
            'content': (f"任务 #{task.id} {'已创建' if created else '已存在'}：{task.title}\n"
                        f"— todo-for-ai"),
        },
    }
    resp = requests.post(f"{_api_base(config)}/cgi-bin/message/send",
                         params={'access_token': token}, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get('errcode') != 0:
        raise ValueError(f"message/send failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    return data
