"""Linear webhook 导入（首个落地的 connector）：issue 状态映射 + 评论追加。"""

from datetime import datetime

import structlog

from models import ExternalConnectorConfig, Project, Task, TaskLog, TaskLogActorType, TaskStatus, db
from services.connectors.common import emit_sync_event, find_task, resolve_project
from services.connectors.store import get_connector

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


def _apply_issue(workspace_id: int, config: ExternalConnectorConfig, action: str, issue: dict) -> dict:
    identifier = issue.get('identifier') or issue.get('id')
    external_key = f"linear:{identifier}"[:100]
    title = str(issue.get('title') or f"[linear] {identifier}")[:500]
    description = (issue.get('description') or '').strip()
    if issue.get('url'):
        description = f"{description}\n\n来源: {issue['url']}".strip()

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

    emit_sync_event(task, 'connector.linear.issue_synced', {
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

    project = resolve_project(config)

    task = find_task(project.id, external_key)
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
    emit_sync_event(task, 'connector.linear.comment_synced', {
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
