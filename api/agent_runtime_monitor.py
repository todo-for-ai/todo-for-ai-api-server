"""
Agent Runtime 监控 API

提供心跳、指标上报和配置同步端点
"""

from datetime import datetime, timedelta
from flask import Blueprint, g, request

from models import db, Agent, AgentSession, AgentHeartbeat, AgentMetrics, AgentRuntimeConfig
from .agent_common import agent_session_required, write_agent_audit
from .base import ApiResponse, validate_json_request


agent_runtime_monitor_bp = Blueprint('agent_runtime_monitor', __name__)


# ==================== 心跳 ====================

@agent_runtime_monitor_bp.route('/agent/heartbeat', methods=['POST'])
@agent_session_required
def agent_heartbeat():
    """
    Agent 心跳上报

    Request:
        {
            "timestamp": "2024-01-01T12:00:00Z",
            "status": "running",  // running, idle, busy, error
            "active_tasks": 2,
            "uptime_seconds": 3600,
            "extra": { ... }
        }
    """
    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    agent = g.current_agent
    session = g.current_agent_session

    # 记录心跳
    heartbeat = AgentHeartbeat.record_heartbeat(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        data={
            'status': data.get('status', 'running'),
            'active_tasks': data.get('active_tasks', 0),
            'uptime_seconds': data.get('uptime_seconds'),
            'extra': data.get('extra'),
        }
    )

    # 更新 session 活跃时间
    session.touch()

    # 审计日志
    write_agent_audit(
        event_type='agent.heartbeat',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        payload={
            'status': data.get('status'),
            'active_tasks': data.get('active_tasks'),
        },
    )

    return ApiResponse.success(
        data={
            'received_at': heartbeat.created_at.isoformat(),
            'next_expected': (datetime.utcnow() + timedelta(seconds=agent.heartbeat_interval_seconds or 30)).isoformat(),
        },
        message='Heartbeat received'
    ).to_response()


@agent_runtime_monitor_bp.route('/agent/health', methods=['POST'])
@agent_session_required
def agent_health_report():
    """
    Agent 详细健康状态上报

    Request:
        {
            "healthy": true,
            "checks": {
                "platform": {"status": "ok"},
                "openclaw": {"status": "ok"},
                "database": {"status": "ok"}
            },
            "timestamp": "2024-01-01T12:00:00Z"
        }
    """
    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    agent = g.current_agent

    # 存储健康检查记录（可以扩展存储到专门的表）
    # 这里简单记录到审计日志
    write_agent_audit(
        event_type='agent.health_report',
        actor_type='agent',
        actor_id=agent.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        payload={
            'healthy': data.get('healthy'),
            'checks': data.get('checks'),
        },
    )

    return ApiResponse.success(
        data={'received': True},
        message='Health report received'
    ).to_response()


# ==================== 指标上报 ====================

@agent_runtime_monitor_bp.route('/agent/metrics', methods=['POST'])
@agent_session_required
def agent_metrics():
    """
    Agent 指标上报

    Request:
        {
            "cpu_percent": 15.5,
            "memory_usage_mb": 128.0,
            "memory_percent": 25.0,
            "disk_usage_percent": 30.0,
            "tasks_completed": 10,
            "tasks_failed": 1,
            "tasks_active": 2,
            "uptime_seconds": 3600,
            "timestamp": "2024-01-01T12:00:00Z"
        }
    """
    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    agent = g.current_agent

    # 记录指标
    metrics = AgentMetrics.record_metrics(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        data=data
    )

    return ApiResponse.success(
        data={
            'received_at': metrics.created_at.isoformat(),
            'metrics_id': metrics.id,
        },
        message='Metrics received'
    ).to_response()


@agent_runtime_monitor_bp.route('/agent/metrics/batch', methods=['POST'])
@agent_session_required
def agent_metrics_batch():
    """
    批量指标上报

    Request:
        {
            "metrics": [
                {"cpu_percent": 10.0, "timestamp": "2024-01-01T12:00:00Z"},
                {"cpu_percent": 15.0, "timestamp": "2024-01-01T12:01:00Z"}
            ]
        }
    """
    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    agent = g.current_agent
    metrics_list = data.get('metrics', [])

    if not metrics_list:
        return ApiResponse.success(
            data={'received': 0},
            message='No metrics to process'
        ).to_response()

    # 批量记录指标
    recorded = []
    for m in metrics_list:
        metrics = AgentMetrics.record_metrics(
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            data=m
        )
        recorded.append(metrics.id)

    return ApiResponse.success(
        data={
            'received': len(recorded),
            'metrics_ids': recorded,
        },
        message=f'{len(recorded)} metrics received'
    ).to_response()


# ==================== 配置同步 ====================

@agent_runtime_monitor_bp.route('/agent/config', methods=['GET'])
@agent_session_required
def agent_get_config():
    """
    获取 Agent 运行时配置

    Response:
        {
            "version": 1,
            "max_concurrent_tasks": 5,
            "heartbeat_interval_seconds": 30,
            ...
        }
    """
    agent = g.current_agent

    # 获取激活的配置
    config = AgentRuntimeConfig.get_active_config(agent.id)

    if not config:
        # 创建默认配置
        config = AgentRuntimeConfig.create_config(
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            data={
                'max_concurrent_tasks': agent.max_concurrency or 5,
                'heartbeat_interval_seconds': agent.heartbeat_interval_seconds or 30,
                'task_timeout_seconds': agent.timeout_seconds or 1800,
                'task_max_retry': agent.max_retry or 2,
            }
        )

    return ApiResponse.success(
        data=config.to_dict(),
        message='Config retrieved'
    ).to_response()


@agent_runtime_monitor_bp.route('/agent/config/sync', methods=['POST'])
@agent_session_required
def agent_sync_config():
    """
    同步 Agent 配置（Agent 上报本地配置，获取最新配置）

    Request:
        {
            "local_config": {
                "version": 1,
                ...
            },
            "runtime_info": {
                "version": "2.0.0",
                "type": "openclaw"
            }
        }

    Response:
        {
            "config": { ... },
            "has_update": true,
            "update_reason": "version_mismatch"
        }
    """
    data = request.get_json() or {}

    agent = g.current_agent
    local_config = data.get('local_config', {})

    # 获取平台配置
    platform_config = AgentRuntimeConfig.get_active_config(agent.id)

    if not platform_config:
        platform_config = AgentRuntimeConfig.create_config(
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            data={
                'max_concurrent_tasks': agent.max_concurrency or 5,
                'heartbeat_interval_seconds': agent.heartbeat_interval_seconds or 30,
                'task_timeout_seconds': agent.timeout_seconds or 1800,
                'task_max_retry': agent.max_retry or 2,
            }
        )

    local_version = local_config.get('version', 0)
    platform_version = platform_config.version

    has_update = platform_version > local_version

    return ApiResponse.success(
        data={
            'config': platform_config.to_dict(),
            'has_update': has_update,
            'update_reason': 'version_mismatch' if has_update else None,
        },
        message='Config synced'
    ).to_response()


# ==================== 运行时状态查询（管理接口） ====================

@agent_runtime_monitor_bp.route('/agent/runtime/status', methods=['GET'])
@agent_session_required
def agent_runtime_status():
    """
    获取 Agent 运行时完整状态

    包含：最新心跳、最新指标、当前配置
    """
    agent = g.current_agent

    # 最新心跳
    latest_heartbeat = AgentHeartbeat.get_latest_by_agent(agent.id)

    # 最新指标
    latest_metrics = AgentMetrics.get_latest_metrics(agent.id)

    # 当前配置
    config = AgentRuntimeConfig.get_active_config(agent.id)

    # 会话信息
    session = AgentSession.query.filter_by(
        agent_id=agent.id,
        is_active=True
    ).order_by(AgentSession.created_at.desc()).first()

    return ApiResponse.success(
        data={
            'agent': {
                'id': agent.id,
                'name': agent.name,
                'status': agent.status.value if agent.status else None,
            },
            'heartbeat': {
                'status': latest_heartbeat.status if latest_heartbeat else None,
                'active_tasks': latest_heartbeat.active_tasks if latest_heartbeat else 0,
                'last_seen': latest_heartbeat.created_at.isoformat() if latest_heartbeat else None,
            } if latest_heartbeat else None,
            'metrics': {
                'cpu_percent': latest_metrics.cpu_percent if latest_metrics else None,
                'memory_usage_mb': latest_metrics.memory_usage_mb if latest_metrics else None,
                'tasks_completed': latest_metrics.tasks_completed if latest_metrics else None,
                'reported_at': latest_metrics.created_at.isoformat() if latest_metrics else None,
            } if latest_metrics else None,
            'config': config.to_dict() if config else None,
            'session': {
                'id': session.id if session else None,
                'token_prefix': session.token_prefix if session else None,
                'expires_at': session.expires_at.isoformat() if session else None,
            } if session else None,
        },
        message='Runtime status retrieved'
    ).to_response()
