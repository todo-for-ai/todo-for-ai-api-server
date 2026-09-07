"""Jira webhook 导入：issue 状态映射 + 评论追加。"""

from datetime import datetime

import structlog

from models import ExternalConnectorConfig, Project, Task, TaskLog, TaskLogActorType, TaskStatus, db
from services.connectors.common import emit_sync_event, find_task, resolve_project
from services.connectors.store import get_connector

logger = structlog.get_logger()

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

    project = resolve_project(config)

    task = find_task(project.id, external_key)
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

    emit_sync_event(task, 'connector.jira.issue_synced', {
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

    project = resolve_project(config)

    task = find_task(project.id, external_key)
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
    emit_sync_event(task, 'connector.jira.comment_synced', {
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
