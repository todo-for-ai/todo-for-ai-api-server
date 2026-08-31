"""外部系统连接器服务（Phase 4 互操作写回侧，Linear 首个落地）

外部事件 → 平台任务/评论的导入：
- Linear webhook（Issue / Comment 的 create|update）→ 平台任务 upsert
  （external key 记在 tasks.creator_identifier = 'linear:<IDENTIFIER>'）
  与追加式评论（TaskLog），状态按 Linear state.type 映射。
- 验签：`Linear-Signature` = HMAC-SHA256(raw body, webhook secret)，
  常量时间比较，fail-closed。
- 每次处理写 TaskEventOutbox（connector.linear.*），出站可见于开放事件流，
  与读侧协议构成双向同步闭环。

配置（ExternalConnectorConfig）：workspace + provider 一条，secret 加密存储。
"""

import hmac
from datetime import datetime

import structlog

from models import (
    ExternalConnectorConfig,
    Project,
    Task,
    TaskEventOutbox,
    TaskLog,
    TaskLogActorType,
    TaskStatus,
    db,
)
from services.github_app import decrypt_str, encrypt_str

logger = structlog.get_logger()

# Linear state.type → 平台 TaskStatus
LINEAR_STATE_MAP = {
    'completed': TaskStatus.DONE,
    'canceled': TaskStatus.CANCELLED,
    'cancelled': TaskStatus.CANCELLED,
    'started': TaskStatus.IN_PROGRESS,
    'unstarted': TaskStatus.TODO,
    'backlog': TaskStatus.TODO,
}


def get_connector(workspace_id: int, provider: str):
    return ExternalConnectorConfig.query.filter_by(
        workspace_id=workspace_id, provider=provider,
    ).first()


def list_connectors(workspace_id: int):
    return ExternalConnectorConfig.query.filter_by(workspace_id=workspace_id).all()


def upsert_connector(workspace_id: int, provider: str, data: dict) -> ExternalConnectorConfig:
    config = get_connector(workspace_id, provider)
    if not config:
        config = ExternalConnectorConfig(workspace_id=workspace_id, provider=provider)
        db.session.add(config)
    if 'enabled' in data:
        config.enabled = bool(data['enabled'])
    if 'default_project_id' in data and data['default_project_id']:
        config.default_project_id = int(data['default_project_id'])
    if data.get('secret'):
        config.secret_encrypted = encrypt_str(str(data['secret']))
    db.session.commit()
    logger.info("connector.config_upserted", workspace_id=workspace_id, provider=provider)
    return config


def verify_linear_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Linear webhook 验签：HMAC-SHA256 hex，常量时间比较。"""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, 'sha256').hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_gitlab_token(token: str, secret: str) -> bool:
    """GitLab webhook 验签：X-GitLab-Token 明文令牌，常量时间比较。"""
    if not token or not secret:
        return False
    return hmac.compare_digest(str(token), str(secret))


# ── Jira 导入处理 ──

def verify_jira_token(token: str, secret: str) -> bool:
    """Jira webhook 验签：配置令牌常量时间比较（header/query 均可携带）。"""
    if not token or not secret:
        return False
    return hmac.compare_digest(str(token), str(secret))


# Jira status name（小写包含匹配）→ 平台 TaskStatus；Jira 工作流状态名可自定义，
# 这里按常见英文/中文名归类，未命中保持现状（不入映射就不改状态）
JIRA_STATUS_MAP = {
    'done': TaskStatus.DONE,
    '完成': TaskStatus.DONE,
    'resolved': TaskStatus.DONE,
    'closed': TaskStatus.DONE,
    'in progress': TaskStatus.IN_PROGRESS,
    'in review': TaskStatus.REVIEW,
    '进行中': TaskStatus.IN_PROGRESS,
    'to do': TaskStatus.TODO,
    'open': TaskStatus.TODO,
    'backlog': TaskStatus.TODO,
    '待办': TaskStatus.TODO,
}


def _map_jira_status(status_name):
    lowered = str(status_name or '').strip().lower()
    for key, status in JIRA_STATUS_MAP.items():
        if key in lowered:
            return status
    return None


def _jira_external_key(issue_key: str) -> str:
    return f"jira:{issue_key}"[:100]


def _apply_jira_issue(workspace_id: int, config: ExternalConnectorConfig, issue: dict) -> dict:
    issue_key = issue.get('key')
    external_key = _jira_external_key(issue_key)
    fields = issue.get('fields') or {}
    title = str(fields.get('summary') or f"[jira] {issue_key}")[:500]
    description = (fields.get('description') or '').strip()

    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    task = _find_task(project.id, external_key)
    created = task is None
    if created:
        task = Task(
            project_id=project.id,
            owner_id=project.owner_id,
            title=title,
            content=description,
            status=TaskStatus.TODO,
            creator_type='ai',
            creator_identifier=external_key,
            created_by='connector:jira',
        )
        db.session.add(task)
        db.session.flush()
    else:
        task.title = title or task.title

    status_name = (fields.get('status') or {}).get('name')
    new_status = _map_jira_status(status_name)
    status_changed = False
    if new_status and task.status.value != new_status.value:
        task.status = new_status
        status_changed = True

    _emit_sync_event(task, 'connector.jira.issue_synced', {
        'issue_key': issue_key,
        'created': created,
        'status_changed': status_changed,
        'jira_status': status_name,
    })
    return {
        'handled': True, 'kind': 'issue',
        'task_id': task.id, 'created': created, 'status_changed': status_changed,
    }


def _apply_jira_comment(workspace_id: int, config: ExternalConnectorConfig, payload: dict) -> dict:
    issue = payload.get('issue') or {}
    issue_key = issue.get('key')
    external_key = _jira_external_key(issue_key)

    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    task = _find_task(project.id, external_key)
    if not task:
        return {'handled': False, 'kind': 'comment', 'reason': 'task not imported yet'}

    comment = payload.get('comment') or {}
    author = ((comment.get('author') or {}).get('displayName')) or 'jira-user'
    body = str(comment.get('body') or '').strip()
    db.session.add(TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.HUMAN,
        content=f"[Jira · {author}] {body}",
        created_by='connector:jira',
    ))
    _emit_sync_event(task, 'connector.jira.comment_synced', {
        'issue_key': issue_key, 'author': author,
    })
    return {'handled': True, 'kind': 'comment', 'task_id': task.id}


def ingest_jira(workspace_id: int, payload: dict) -> dict:
    """处理一条 Jira webhook 事件（jira:issue_created/updated、jira:comment_created）。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_JIRA)
    if not config or not config.enabled:
        raise ValueError('jira connector not enabled for this workspace')

    webhook_event = str(payload.get('webhookEvent') or '')
    issue = payload.get('issue') or {}

    if webhook_event in ('jira:issue_created', 'jira:issue_updated') and issue.get('key'):
        result = _apply_jira_issue(workspace_id, config, issue)
    elif webhook_event == 'jira:comment_created' and issue.get('key'):
        result = _apply_jira_comment(workspace_id, config, payload)
    else:
        return {'handled': False, 'reason': f'{webhook_event} ignored'}

    config.last_synced_at = datetime.utcnow()
    db.session.commit()
    logger.info("connector.ingested_jira", workspace_id=workspace_id, **result)
    return result


# ── GitLab 导入处理 ──

# GitLab issue state → 平台 TaskStatus
GITLAB_STATE_MAP = {
    'opened': TaskStatus.TODO,
    'reopened': TaskStatus.TODO,
    'closed': TaskStatus.DONE,
}


def _gitlab_external_key(gl_project_id, iid: str) -> str:
    return f"gitlab:{gl_project_id}:{iid}"[:100]


def _apply_gitlab_issue(workspace_id: int, config: ExternalConnectorConfig,
                        attrs: dict, gl_project: dict) -> dict:
    iid = attrs.get('iid')
    external_key = _gitlab_external_key(gl_project.get('id'), iid)
    title = str(attrs.get('title') or f"[gitlab] #{iid}")[:500]
    description = (attrs.get('description') or '').strip()
    if attrs.get('url'):
        description = f"{description}\n\n来源: {attrs['url']}".strip()

    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    task = _find_task(project.id, external_key)
    created = task is None
    if created:
        task = Task(
            project_id=project.id,
            owner_id=project.owner_id,
            title=title,
            content=description,
            status=TaskStatus.TODO,
            creator_type='ai',
            creator_identifier=external_key,
            created_by='connector:gitlab',
        )
        db.session.add(task)
        db.session.flush()
    else:
        task.title = title or task.title

    state = str(attrs.get('state') or '').lower()
    new_status = GITLAB_STATE_MAP.get(state)
    status_changed = False
    if new_status and task.status.value != new_status.value:
        task.status = new_status
        status_changed = True

    _emit_sync_event(task, 'connector.gitlab.issue_synced', {
        'action': str(attrs.get('action') or ''),
        'iid': iid,
        'gl_project_id': gl_project.get('id'),
        'created': created,
        'status_changed': status_changed,
    })
    return {
        'handled': True, 'kind': 'issue',
        'task_id': task.id, 'created': created, 'status_changed': status_changed,
    }


def _apply_gitlab_note(workspace_id: int, config: ExternalConnectorConfig, payload: dict) -> dict:
    attrs = payload.get('object_attributes') or {}
    issue_ref = payload.get('issue') or {}
    external_key = _gitlab_external_key((payload.get('project') or {}).get('id'), issue_ref.get('iid'))

    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    task = _find_task(project.id, external_key)
    if not task:
        return {'handled': False, 'kind': 'comment', 'reason': 'task not imported yet'}

    author = (payload.get('user') or {}).get('username') or 'gitlab-user'
    body = str(attrs.get('note') or '').strip()
    db.session.add(TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.HUMAN,
        content=f"[GitLab · {author}] {body}",
        created_by='connector:gitlab',
    ))
    _emit_sync_event(task, 'connector.gitlab.comment_synced', {
        'iid': issue_ref.get('iid'), 'author': author,
    })
    return {'handled': True, 'kind': 'comment', 'task_id': task.id}


def ingest_gitlab(workspace_id: int, payload: dict) -> dict:
    """处理一条 GitLab webhook 事件（issue / note）。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_GITLAB)
    if not config or not config.enabled:
        raise ValueError('gitlab connector not enabled for this workspace')

    object_kind = str(payload.get('object_kind') or '')

    if object_kind == 'issue':
        result = _apply_gitlab_issue(
            workspace_id, config,
            payload.get('object_attributes') or {},
            payload.get('project') or {},
        )
    elif object_kind == 'note' and (payload.get('issue') or payload.get('object_attributes', {}).get('issue')):
        result = _apply_gitlab_note(workspace_id, config, payload)
    else:
        return {'handled': False, 'reason': f'{object_kind} ignored'}

    config.last_synced_at = datetime.utcnow()
    db.session.commit()
    logger.info("connector.ingested_gitlab", workspace_id=workspace_id, **result)
    return result


# ── 导入处理 ──

def _find_task(project_id: int, external_key: str):
    return Task.query.filter_by(
        project_id=project_id, creator_identifier=external_key,
    ).first()


def _emit_sync_event(task, event_type: str, payload: dict) -> None:
    project = task.project
    db.session.add(TaskEventOutbox(
        event_id=f"conn-{task.id}-{event_type}-{datetime.utcnow().timestamp()}",
        event_type=event_type,
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=project.organization_id if project else None,
        payload=payload,
        occurred_at=datetime.utcnow(),
        created_by='connector:linear',
    ))


def _apply_issue(workspace_id: int, config: ExternalConnectorConfig, action: str, issue: dict) -> dict:
    identifier = issue.get('identifier') or issue.get('id')
    external_key = f"linear:{identifier}"[:100]
    title = str(issue.get('title') or f"[linear] {identifier}")[:500]
    description = (issue.get('description') or '').strip()
    if issue.get('url'):
        description = f"{description}\n\n来源: {issue['url']}".strip()

    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    task = _find_task(project.id, external_key)
    created = task is None
    if created:
        task = Task(
            project_id=project.id,
            owner_id=project.owner_id,
            title=title,
            content=description,
            status=TaskStatus.TODO,
            creator_type='ai',
            creator_identifier=external_key,
            created_by='connector:linear',
        )
        db.session.add(task)
        db.session.flush()
    else:
        task.title = title or task.title

    # 状态同步（update 动作且带 state）
    state_info = issue.get('state') or {}
    state_type = str(state_info.get('type') or '').lower()
    new_status = LINEAR_STATE_MAP.get(state_type)
    status_changed = False
    if new_status and task.status.value != new_status.value:
        task.status = new_status
        status_changed = True

    _emit_sync_event(task, 'connector.linear.issue_synced', {
        'action': action,
        'identifier': identifier,
        'created': created,
        'status_changed': status_changed,
    })
    return {
        'handled': True, 'kind': 'issue', 'action': action,
        'task_id': task.id, 'created': created, 'status_changed': status_changed,
    }


def _apply_comment(workspace_id: int, config: ExternalConnectorConfig, comment: dict) -> dict:
    issue_ref = comment.get('issue') or {}
    identifier = issue_ref.get('identifier') or issue_ref.get('id')
    external_key = f"linear:{identifier}"[:100]

    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')

    task = _find_task(project.id, external_key)
    if not task:
        return {'handled': False, 'kind': 'comment', 'reason': 'task not imported yet'}

    author = ((comment.get('user') or {}).get('name')) or 'linear-user'
    body = str(comment.get('body') or '').strip()
    db.session.add(TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.HUMAN,
        content=f"[Linear · {author}] {body}",
        created_by='connector:linear',
    ))
    _emit_sync_event(task, 'connector.linear.comment_synced', {
        'identifier': identifier, 'author': author,
    })
    return {'handled': True, 'kind': 'comment', 'task_id': task.id}


def ingest_linear(workspace_id: int, payload: dict) -> dict:
    """处理一条 Linear webhook 事件（Issue/Comment × create|update）。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_LINEAR)
    if not config or not config.enabled:
        raise ValueError('linear connector not enabled for this workspace')

    event_type = str(payload.get('type') or '')
    action = str(payload.get('action') or '')
    data = payload.get('data') or {}

    if event_type == 'Issue' and action in ('create', 'update'):
        result = _apply_issue(workspace_id, config, action, data)
    elif event_type == 'Comment' and action == 'create':
        result = _apply_comment(workspace_id, config, data)
    else:
        return {'handled': False, 'reason': f'{event_type}.{action} ignored'}

    config.last_synced_at = datetime.utcnow()
    db.session.commit()
    logger.info("connector.ingested", workspace_id=workspace_id, **result)
    return result
