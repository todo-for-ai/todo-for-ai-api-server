"""各 provider 共用的导入处理工具。"""

from datetime import datetime

from models import ExternalConnectorConfig, Project, Task, TaskEventOutbox, db


def resolve_project(config: ExternalConnectorConfig) -> Project:
    """取 connector 配置指向的默认项目；缺失或无效直接报错。

    （原实现在三个 provider 的 issue/comment 处理里重复五次，统一于此。）
    """
    project = db.session.get(Project, config.default_project_id) if config.default_project_id else None
    if not project:
        raise ValueError('connector default_project_id missing or invalid')
    return project


def find_task(project_id: int, external_key: str):
    return Task.query.filter_by(
        project_id=project_id, creator_identifier=external_key,
    ).first()


def emit_sync_event(task, event_type: str, payload: dict) -> None:
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
