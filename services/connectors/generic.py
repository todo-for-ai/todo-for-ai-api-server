"""通用 Webhook 入站：任何能发 HTTP 的企业内部系统 → 平台任务。

约定：
- 验签：请求头 X-Todo4AI-Token 与连接器 secret 常量时间比较（fail-closed）；
- 字段映射：config_json.mapping = {"title": <path>, "content": <path>,
  "external_key": <path>, "priority": <path>}，path 为点分路径（"a.b.0.c"，
  数组用数字下标），缺省用请求体顶层 title/content；
- 幂等：external_key 缺省取请求头 X-Request-ID 或 body 哈希。
"""

import hashlib
import hmac
import json

from models import ExternalConnectorConfig, Project, Task, TaskLog, TaskLogActorType, TaskStatus, db
from services.connectors.common import emit_sync_event, find_task
from services.github_app import decrypt_str

TASK_TITLE_MAX = 200


def verify_generic_token(config: ExternalConnectorConfig, token: str) -> bool:
    raw = decrypt_str(config.secret_encrypted) if config.secret_encrypted else ''
    if not raw:
        return False
    return hmac.compare_digest(str(token or ''), raw.strip())


def _resolve_path(data, path: str):
    """点分路径取值：a.b.0.c；任一层缺失返回 None。"""
    cur = data
    for part in str(path or '').split('.'):
        if part == '':
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _mapping_get(config: ExternalConnectorConfig, body: dict, key: str, default=''):
    mapping = (config.config_json or {}).get('mapping') or {}
    path = mapping.get(key)
    value = _resolve_path(body, path) if path else body.get(key)
    if value is None:
        value = default if default else body.get(key)
    return value


def ingest_generic(workspace_id: int, config: ExternalConnectorConfig, body: dict,
                   request_id: str = '') -> dict:
    if not isinstance(body, dict) or not body:
        raise ValueError('empty JSON body')

    project = Project.query.get(config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    title = str(_mapping_get(config, body, 'title') or '').strip()[:TASK_TITLE_MAX]
    if not title:
        raise ValueError('title missing (check mapping.title)')
    content = str(_mapping_get(config, body, 'content') or title)
    external_key = str(_mapping_get(config, body, 'external_key') or request_id or '').strip()
    if not external_key:
        external_key = 'generic:' + hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    external_key = f"generic:{external_key}"[:100]

    priority = str(_mapping_get(config, body, 'priority') or 'medium').lower()
    if priority not in ('low', 'medium', 'high', 'urgent'):
        priority = 'medium'

    task = find_task(project.id, external_key)
    created = task is None
    if created:
        task = Task(
            project_id=project.id,
            owner_id=project.owner_id,
            title=title,
            content=content,
            status=TaskStatus.TODO,
            creator_type='ai',
            creator_identifier=external_key,
            created_by='connector:generic',
        )
        db.session.add(task)
        db.session.flush()
    db.session.add(TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.SYSTEM,
        content=f"来自通用 Webhook 入站（{'创建' if created else '幂等命中'}）",
    ))
    emit_sync_event(task, 'connector.generic.ingested', {
        'external_key': external_key, 'created': created, 'task_id': task.id,
    })
    config.last_synced_at = __import__('datetime').datetime.utcnow()
    return {'action': 'task', 'created': created, 'task_id': task.id}
