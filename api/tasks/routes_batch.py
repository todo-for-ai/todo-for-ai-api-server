"""Batch task operations and task dependency routes."""

from flask import request

from models import db, Task
from api.base import ApiResponse
from core.auth import unified_auth_required

from . import tasks_bp

MAX_BATCH_SIZE = 100


@tasks_bp.route('/batch/update-status', methods=['POST'])
@unified_auth_required
def batch_update_status():
    data = request.get_json()
    task_ids = data.get('task_ids', [])
    new_status = data.get('status')

    if not task_ids or not new_status:
        return ApiResponse.error('task_ids and status are required').to_response()
    if len(task_ids) > MAX_BATCH_SIZE:
        return ApiResponse.error(f'Max {MAX_BATCH_SIZE} tasks per batch').to_response()

    tasks = db.session.query(Task).filter(Task.id.in_(task_ids)).all()
    for task in tasks:
        task.status = new_status
    db.session.commit()

    return ApiResponse.success(data={'updated': len(tasks)}).to_response()


@tasks_bp.route('/batch/update-priority', methods=['POST'])
@unified_auth_required
def batch_update_priority():
    data = request.get_json()
    task_ids = data.get('task_ids', [])
    new_priority = data.get('priority')

    if not task_ids or not new_priority:
        return ApiResponse.error('task_ids and priority are required').to_response()
    if len(task_ids) > MAX_BATCH_SIZE:
        return ApiResponse.error(f'Max {MAX_BATCH_SIZE} tasks per batch').to_response()

    tasks = db.session.query(Task).filter(Task.id.in_(task_ids)).all()
    for task in tasks:
        task.priority = new_priority
    db.session.commit()

    return ApiResponse.success(data={'updated': len(tasks)}).to_response()


@tasks_bp.route('/batch/delete', methods=['POST'])
@unified_auth_required
def batch_delete():
    data = request.get_json()
    task_ids = data.get('task_ids', [])

    if not task_ids:
        return ApiResponse.error('task_ids are required').to_response()
    if len(task_ids) > MAX_BATCH_SIZE:
        return ApiResponse.error(f'Max {MAX_BATCH_SIZE} tasks per batch').to_response()

    deleted = db.session.query(Task).filter(Task.id.in_(task_ids)).delete(synchronize_session=False)
    db.session.commit()

    return ApiResponse.success(data={'deleted': deleted}).to_response()


@tasks_bp.route('/batch/assign', methods=['POST'])
@unified_auth_required
def batch_assign():
    data = request.get_json()
    task_ids = data.get('task_ids', [])
    assignees = data.get('assignees')

    if not task_ids or assignees is None:
        return ApiResponse.error('task_ids and assignees are required').to_response()
    if len(task_ids) > MAX_BATCH_SIZE:
        return ApiResponse.error(f'Max {MAX_BATCH_SIZE} tasks per batch').to_response()

    tasks = db.session.query(Task).filter(Task.id.in_(task_ids)).all()
    for task in tasks:
        task.assignees = assignees
    db.session.commit()

    return ApiResponse.success(data={'updated': len(tasks)}).to_response()


@tasks_bp.route('/<int:task_id>/dependencies', methods=['GET'])
@unified_auth_required
def get_dependencies(task_id):
    task = db.session.query(Task).get(task_id)
    if not task:
        return ApiResponse.error('Task not found', 404).to_response()
    return ApiResponse.success(data={
        'blocking': task.blocking_task_ids or [],
        'blocked_by': task.blocked_by_task_ids or [],
    }).to_response()


@tasks_bp.route('/<int:task_id>/dependencies', methods=['PUT'])
@unified_auth_required
def update_dependencies(task_id):
    data = request.get_json()
    blocking = data.get('blocking_task_ids', [])
    blocked_by = data.get('blocked_by_task_ids', [])

    task = db.session.query(Task).get(task_id)
    if not task:
        return ApiResponse.error('Task not found', 404).to_response()

    task.blocking_task_ids = blocking
    task.blocked_by_task_ids = blocked_by
    db.session.commit()

    return ApiResponse.success(data=task.to_dict()).to_response()
