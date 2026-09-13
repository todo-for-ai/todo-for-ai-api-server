"""
User WebSocket Namespace

Handles real-time communication with human users:
- Connection authentication via JWT
- Task room join/leave for scoped events
- Push notifications, task updates, chat messages, approval requests
"""

from datetime import datetime
from flask import request as flask_request, session as socketio_session
from flask_socketio import Namespace, emit, join_room, leave_room, disconnect


class UserNamespace(Namespace):
    """User WebSocket Namespace at /user/ws"""

    def __init__(self, namespace='/user/ws'):
        super().__init__(namespace)

    def on_connect(self, auth=None):
        """Handle user connection with JWT authentication"""
        if not auth:
            auth = flask_request.args

        token = auth.get('token') if isinstance(auth, dict) else None
        if not token:
            emit('auth_error', {'error': 'Token required'})
            disconnect()
            return False

        from flask_jwt_extended import decode_token
        from jwt.exceptions import InvalidTokenError
        try:
            decoded = decode_token(token)
            user_id = decoded.get('sub')
        except InvalidTokenError:
            user_id = None

        if not user_id:
            emit('auth_error', {'error': 'Invalid or expired token'})
            disconnect()
            return False

        socketio_session['user_id'] = user_id
        join_room(f'user:{user_id}')

        emit('auth_success', {
            'user_id': user_id,
            'connected_at': datetime.utcnow().isoformat()
        })
        return True

    def on_disconnect(self):
        """Handle user disconnect"""
        pass

    def on_join_task(self, data):
        """Join a task room to receive task-scoped events"""
        user_id = socketio_session.get('user_id')
        if not user_id:
            return
        task_id = data.get('task_id')
        if task_id:
            join_room(f'task:{task_id}')

    def on_leave_task(self, data):
        """Leave a task room"""
        task_id = data.get('task_id')
        if task_id:
            leave_room(f'task:{task_id}')

    def on_join_project(self, data):
        """Join a project room to receive project-scoped events（如任务图刷新）"""
        user_id = socketio_session.get('user_id')
        if not user_id:
            return
        project_id = data.get('project_id')
        if project_id:
            join_room(f'project:{project_id}')

    def on_leave_project(self, data):
        """Leave a project room"""
        project_id = data.get('project_id')
        if project_id:
            leave_room(f'project:{project_id}')


def push_to_user(user_id, event, data):
    """Push an event to a specific user via WebSocket"""
    from flask_socketio import emit as broadcast_emit
    broadcast_emit(event, data, room=f'user:{user_id}', namespace='/user/ws')


def push_to_task_room(task_id, event, data):
    """Push an event to all users in a task room"""
    from flask_socketio import emit as broadcast_emit
    broadcast_emit(event, data, room=f'task:{task_id}', namespace='/user/ws')


def push_to_project(project_id, event, data):
    """Push an event to all users in a project room"""
    from flask_socketio import emit as broadcast_emit
    broadcast_emit(event, data, room=f'project:{project_id}', namespace='/user/ws')


def notify_task_graph_changed(project_id, task_ids, reason):
    """任务状态/依赖变化后通知项目房间刷新任务图（前端 TaskGraphTab 订阅）。

    容错设计：project_id 缺失或推送通道异常都不抛出——图刷新是锦上添花，
    不能反过来打断状态翻转的主链路（runtime commit / 人工更新 / 依赖编辑）。
    """
    if not project_id:
        return
    try:
        push_to_project(int(project_id), 'task_graph_changed', {
            'project_id': int(project_id),
            'task_ids': [int(t) for t in (task_ids or []) if t],
            'reason': reason,
            'occurred_at': datetime.utcnow().isoformat(),
        })
    except Exception:
        pass
