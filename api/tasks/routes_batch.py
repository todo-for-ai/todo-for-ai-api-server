"""Batch task operations and task dependency routes."""

from flask import request

from models import db, Task, TaskStatus
from api.base import ApiResponse
from core.auth import unified_auth_required
from services.task_graph import find_dependency_cycle
from api.user_websocket import notify_task_graph_changed

from . import tasks_bp

MAX_BATCH_SIZE = 100


def _coerce_status(new_status):
    """状态字符串兼容枚举 name（DONE）与 value（done）：其余端点（人工更新/MCP）
    均按 value 校验，批量接口此前裸赋值导致 value 触发 Enum KeyError 500。"""
    try:
        return TaskStatus(new_status)
    except ValueError:
        try:
            return TaskStatus[str(new_status).strip().upper()]
        except KeyError:
            return None


def _notify_graph_by_project(tasks, reason):
    """按项目分组推送任务图刷新事件（跨项目批量时每个项目各推一次）。"""
    by_project = {}
    for task in tasks:
        by_project.setdefault(task.project_id, []).append(task.id)
    for project_id, ids in by_project.items():
        notify_task_graph_changed(project_id, ids, reason)


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

    status_enum = _coerce_status(new_status)
    if status_enum is None:
        return ApiResponse.error(f'Invalid status: {new_status}').to_response()

    tasks = db.session.query(Task).filter(Task.id.in_(task_ids)).all()
    for task in tasks:
        task.status = status_enum
    db.session.commit()

    # 任务图实时刷新：批量状态变更按项目分组通知（TaskGraphTab 订阅）
    _notify_graph_by_project(tasks, 'batch_status_changed')

    # 出站 webhook 订阅推送（异步后台线程，不阻塞请求）
    try:
        from services.webhook_dispatcher import dispatch_event, task_snapshot
        from models import Project as _Project
        for project_id in {t.project_id for t in tasks}:
            project = db.session.get(_Project, project_id)
            ws_id = project.organization_id if project else None
            if ws_id:
                for t in tasks:
                    if t.project_id == project_id:
                        dispatch_event(ws_id, 'task.status_changed',
                                       {'task': task_snapshot(t)})
    except Exception:
        pass

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

    # 防环：依赖门语义下环 = 互相等待、永久无法派发，写侧直接拒绝。
    # 自依赖与传递成环同拦；失效引用（已删任务）由派发门容忍，不在此校验。
    cycle_blocker = find_dependency_cycle(task.id, blocked_by)
    if cycle_blocker is not None:
        return ApiResponse.error(
            f'Dependency cycle rejected: task {task.id} cannot depend on task '
            f'{cycle_blocker} (directly or transitively)',
            400,
        ).to_response()

    task.blocking_task_ids = blocking
    task.blocked_by_task_ids = blocked_by
    db.session.commit()

    # 任务图实时刷新：依赖边变化通知项目房间（TaskGraphTab 订阅）
    notify_task_graph_changed(task.project_id, [task.id], 'dependencies_changed')

    return ApiResponse.success(data=task.to_dict()).to_response()
