"""
任务日志 API（追加写）
"""

from flask import Blueprint, g
from models import db, Task, TaskLog, TaskLogActorType
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request, get_request_args
from .agent_common import agent_session_required
from api.organizations.events import record_organization_event


task_logs_bp = Blueprint('task_logs', __name__)


def _get_task_or_error(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return None, ApiResponse.not_found('Task not found').to_response()
    return task, None


def _agent_allowed_project_ids(agent):
    if not agent.allowed_project_ids:
        return None
    return {int(pid) for pid in agent.allowed_project_ids if str(pid).isdigit()}


@task_logs_bp.route('/tasks/<int:task_id>/logs', methods=['GET'])
@unified_auth_required
def list_task_logs(task_id):
    current_user = get_current_user()
    task, err = _get_task_or_error(task_id)
    if err:
        return err

    if not current_user.can_access_project(task.project):
        return ApiResponse.forbidden('Access denied').to_response()

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 200)

    query = TaskLog.query.filter(TaskLog.task_id == task_id).order_by(TaskLog.created_at.desc())
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [row.to_dict() for row in rows],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Task logs retrieved successfully',
    ).to_response()


@task_logs_bp.route('/tasks/<int:task_id>/logs', methods=['POST'])
@unified_auth_required
def append_task_log_by_user(task_id):
    current_user = get_current_user()
    task, err = _get_task_or_error(task_id)
    if err:
        return err

    if not current_user.can_access_project(task.project):
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(required_fields=['content'], optional_fields=['content_type'])
    if isinstance(data, tuple):
        return data

    content = str(data['content']).strip()
    if not content:
        return ApiResponse.error('content is required', 400).to_response()

    row = TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.HUMAN,
        actor_user_id=current_user.id,
        content=content,
        content_type=(data.get('content_type') or 'text/markdown')[:32],
        created_by=current_user.email,
    )
    db.session.add(row)

    record_organization_event(
        organization_id=task.project.organization_id if task.project else None,
        event_type='task.log.appended',
        actor_type='user',
        actor_id=current_user.id,
        actor_name=current_user.full_name or current_user.nickname or current_user.username or current_user.email,
        target_type='task',
        target_id=task.id,
        project_id=task.project_id,
        task_id=task.id,
        message=f"Task log appended: {task.title}",
        payload={
            'task_title': task.title,
            'project_name': task.project.name if task.project else None,
            'content_preview': content[:200],
            'content_type': (data.get('content_type') or 'text/markdown')[:32],
        },
        created_by=current_user.email,
    )

    db.session.commit()

    return ApiResponse.created(row.to_dict(), 'Task log appended successfully').to_response()


@task_logs_bp.route('/agent/tasks/<int:task_id>/logs', methods=['GET'])
@agent_session_required
def list_task_logs_by_agent(task_id):
    agent = g.current_agent
    task, err = _get_task_or_error(task_id)
    if err:
        return err

    allowed_project_ids = _agent_allowed_project_ids(agent)
    if allowed_project_ids and task.project_id not in allowed_project_ids:
        return ApiResponse.forbidden('Access denied').to_response()

    rows = TaskLog.query.filter(TaskLog.task_id == task_id).order_by(TaskLog.created_at.desc()).limit(200).all()
    return ApiResponse.success({'items': [row.to_dict() for row in rows]}, 'Task logs retrieved successfully').to_response()


@task_logs_bp.route('/agent/tasks/<int:task_id>/logs', methods=['POST'])
@agent_session_required
def append_task_log_by_agent(task_id):
    agent = g.current_agent
    task, err = _get_task_or_error(task_id)
    if err:
        return err

    allowed_project_ids = _agent_allowed_project_ids(agent)
    if allowed_project_ids and task.project_id not in allowed_project_ids:
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(required_fields=['content'], optional_fields=['content_type'])
    if isinstance(data, tuple):
        return data

    content = str(data['content']).strip()
    if not content:
        return ApiResponse.error('content is required', 400).to_response()

    row = TaskLog(
        task_id=task.id,
        actor_type=TaskLogActorType.AGENT,
        actor_agent_id=agent.id,
        content=content,
        content_type=(data.get('content_type') or 'text/markdown')[:32],
        created_by=f'agent:{agent.id}',
    )
    db.session.add(row)

    record_organization_event(
        organization_id=task.project.organization_id if task.project else None,
        event_type='task.log.appended',
        actor_type='agent',
        actor_id=agent.id,
        actor_name=agent.name,
        target_type='task',
        target_id=task.id,
        project_id=task.project_id,
        task_id=task.id,
        message=f"Task log appended by agent: {task.title}",
        payload={
            'task_title': task.title,
            'project_name': task.project.name if task.project else None,
            'content_preview': content[:200],
            'content_type': (data.get('content_type') or 'text/markdown')[:32],
        },
        created_by=f'agent:{agent.id}',
    )

    db.session.commit()

    return ApiResponse.created(row.to_dict(), 'Task log appended successfully').to_response()


@task_logs_bp.route('/agent/logs/batch', methods=['POST'])
@agent_session_required
def append_task_logs_batch_by_agent():
    agent = g.current_agent
    data = validate_json_request(required_fields=['logs'])
    if isinstance(data, tuple):
        return data

    logs = data.get('logs') or []
    if not isinstance(logs, list):
        return ApiResponse.error('logs must be array', 400).to_response()

    allowed_project_ids = _agent_allowed_project_ids(agent)
    uploaded = 0
    skipped = 0

    for item in logs:
        if not isinstance(item, dict):
            skipped += 1
            continue

        raw_task_id = item.get('task_id')
        if raw_task_id is None:
            skipped += 1
            continue

        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            skipped += 1
            continue

        task = db.session.get(Task, task_id)
        if not task:
            skipped += 1
            continue

        if allowed_project_ids and task.project_id not in allowed_project_ids:
            skipped += 1
            continue

        content = item.get('content')
        if content is None:
            content = item.get('message')
        content = str(content or '').strip()
        if not content:
            skipped += 1
            continue

        row = TaskLog(
            task_id=task.id,
            actor_type=TaskLogActorType.AGENT,
            actor_agent_id=agent.id,
            content=content,
            content_type=(item.get('content_type') or 'text/plain')[:32],
            created_by=f'agent:{agent.id}',
        )
        db.session.add(row)
        uploaded += 1

    db.session.commit()

    return ApiResponse.success(
        {'uploaded': uploaded, 'skipped': skipped},
        'Task logs batch processed',
    ).to_response()
