"""
Agent Runtime WebSocket Namespace

Handles real-time communication with Agent Runtime:
- Connection authentication
- Heartbeat/health streaming
- Task push notifications
- Config updates
- Remote commands
"""

from datetime import datetime
from flask import request, session as socketio_session
from flask_socketio import Namespace, emit, join_room, disconnect
from models import Agent, AgentSession, db

# 进程内在线 Agent 注册表：连接/断连即时维护（remote 后端状态判定优先用它；
# 多 worker/多实例部署下它只覆盖本进程，远端实例退化为心跳新鲜度判定）
_CONNECTED_AGENT_IDS = set()

# daemon 可上报的运行时元数据字段（写入 agent.config['runtime_meta']）
_META_FIELDS = ('host', 'engine', 'version', 'os')


def is_agent_connected(agent_id) -> bool:
    """Agent 当前是否持有活跃 WS 连接（本进程视角）。"""
    return agent_id in _CONNECTED_AGENT_IDS


def merge_runtime_meta(agent, meta) -> bool:
    """把 daemon 上报的运行时元数据并入 agent.config['runtime_meta']。

    只在内容有变化时写库（心跳高频调用不产生写放大）。返回是否写入。
    """
    cleaned = {
        k: v for k, v in (meta or {}).items()
        if k in _META_FIELDS and v
    }
    if not cleaned:
        return False
    config = dict(agent.config or {})
    current = dict(config.get('runtime_meta') or {})
    if all(current.get(k) == v for k, v in cleaned.items()):
        return False
    current.update(cleaned)
    config['runtime_meta'] = current
    agent.config = config
    db.session.commit()
    return True


class AgentRuntimeNamespace(Namespace):
    """Agent Runtime WebSocket Namespace"""

    def __init__(self, namespace='/agent/ws'):
        super().__init__(namespace)

    def on_connect(self, auth=None):
        """Handle client connection with authentication"""
        if not auth:
            auth = request.args

        agent_key = auth.get('agent_key') if isinstance(auth, dict) else None
        token = auth.get('token') if isinstance(auth, dict) else None

        agent = None
        if agent_key:
            from models import AgentKey
            key = AgentKey.verify_key(agent_key)
            if key:
                agent = Agent.query.get(key.agent_id)
        elif token:
            session = AgentSession.verify_session_token(token)
            if session:
                agent = Agent.query.get(session.agent_id)

        if not agent:
            emit('auth_error', {'error': 'Invalid credentials'})
            disconnect()
            return False

        socketio_session['agent_id'] = agent.id
        socketio_session['workspace_id'] = agent.workspace_id
        _CONNECTED_AGENT_IDS.add(agent.id)

        # daemon 可在连接时上报运行时元数据（host/engine/version/os）
        if isinstance(auth, dict):
            try:
                merge_runtime_meta(agent, auth)
            except Exception:  # noqa: BLE001 — 元数据落库失败不阻断连接
                pass

        join_room(f'agent:{agent.id}')
        join_room(f'workspace:{agent.workspace_id}')

        emit('auth_success', {
            'agent_id': agent.id,
            'workspace_id': agent.workspace_id,
            'connected_at': datetime.utcnow().isoformat()
        })
        return True

    def on_disconnect(self):
        """Handle client disconnect"""
        agent_id = socketio_session.get('agent_id')
        if agent_id:
            _CONNECTED_AGENT_IDS.discard(agent_id)

    def on_heartbeat(self, data):
        """Handle heartbeat from agent"""
        agent_id = socketio_session.get('agent_id')
        if not agent_id:
            return

        agent = Agent.query.get(agent_id)
        if agent:
            agent.last_seen_at = datetime.utcnow()
            # 心跳可携带运行时元数据（仅变化时落库）
            meta = (data or {}).get('meta') if isinstance(data, dict) else None
            try:
                merge_runtime_meta(agent, meta)
            except Exception:  # noqa: BLE001
                pass
            db.session.commit()

        emit('heartbeat_ack', {
            'timestamp': datetime.utcnow().isoformat(),
            'server_time': datetime.utcnow().timestamp()
        })
    
    def on_task_ack(self, data):
        """Handle task acknowledgment from agent"""
        agent_id = socketio_session.get('agent_id')
        workspace_id = socketio_session.get('workspace_id')
        if not agent_id:
            return
        
        task_id = data.get('task_id')
        attempt_id = data.get('attempt_id')
        
        emit('task_assigned', {
            'task_id': task_id,
            'agent_id': agent_id,
            'attempt_id': attempt_id,
            'assigned_at': datetime.utcnow().isoformat()
        }, room=f'workspace:{workspace_id}')
    
    def on_metrics(self, data):
        """Handle metrics streaming from agent"""
        agent_id = socketio_session.get('agent_id')
        workspace_id = socketio_session.get('workspace_id')
        if not agent_id:
            return
        
        # For now just acknowledge; persistence can be added later
        emit('metrics_ack', {'received': True})


def push_task_to_agent(agent_id, task_data):
    """Push a task to a connected agent via WebSocket"""
    from flask_socketio import emit as broadcast_emit
    broadcast_emit('task_assign', task_data, room=f'agent:{agent_id}', namespace='/agent/ws')


def broadcast_config_update(workspace_id, config_data):
    """Broadcast config update to all agents in workspace"""
    from flask_socketio import emit as broadcast_emit
    broadcast_emit('config_update', config_data, room=f'workspace:{workspace_id}', namespace='/agent/ws')


def send_command_to_agent(agent_id, command, args=None):
    """Send a remote command to a specific agent"""
    from flask_socketio import emit as broadcast_emit
    broadcast_emit('command', {
        'command': command,
        'args': args or {},
        'sent_at': datetime.utcnow().isoformat()
    }, room=f'agent:{agent_id}', namespace='/agent/ws')
