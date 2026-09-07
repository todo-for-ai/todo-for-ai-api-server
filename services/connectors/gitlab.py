"""GitLab webhook 导入：issue 状态映射 + note 评论。"""

from datetime import datetime

import structlog

from models import ExternalConnectorConfig, Project, Task, TaskLog, TaskLogActorType, TaskStatus, db
from services.connectors.common import emit_sync_event, find_task, resolve_project
from services.connectors.store import get_connector

logger = structlog.get_logger()

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

    emit_sync_event(task, 'connector.gitlab.issue_synced', {
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

    project = resolve_project(config)

    task = find_task(project.id, external_key)
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
    emit_sync_event(task, 'connector.gitlab.comment_synced', {
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
