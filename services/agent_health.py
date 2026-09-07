"""
Agent 健康检查服务

监控 Agent 在线状态、任务队列、性能指标
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from sqlalchemy import func
from models import db, Agent, AgentStatus, AgentTaskLease

logger = logging.getLogger(__name__)


class AgentHealthStatus:
    """Agent 健康状态"""

    ONLINE = 'online'
    OFFLINE = 'offline'
    DEGRADED = 'degraded'
    UNKNOWN = 'unknown'


class AgentHealthMonitor:
    """
    Agent 健康监控器

    检查 Agent 心跳、任务处理状态
    """

    # 心跳超时时间（秒）
    HEARTBEAT_TIMEOUT = 120
    # 任务队列告警阈值
    QUEUE_ALERT_THRESHOLD = 50

    def __init__(self):
        self._last_check = None

    def check_agent_health(self, agent_id: int) -> Dict:
        """检查单个 Agent 健康状态"""
        agent = Agent.query.get(agent_id)
        if not agent:
            return {'status': AgentHealthStatus.UNKNOWN, 'error': 'Agent not found'}

        # 检查心跳
        last_heartbeat = self._get_last_heartbeat(agent_id)
        is_online = False
        if last_heartbeat:
            time_since_heartbeat = (datetime.utcnow() - last_heartbeat).total_seconds()
            is_online = time_since_heartbeat < self.HEARTBEAT_TIMEOUT

        # 检查任务状态
        task_stats = self._get_task_stats(agent_id)

        # 确定健康状态
        if not is_online:
            status = AgentHealthStatus.OFFLINE
        elif task_stats['queued'] > self.QUEUE_ALERT_THRESHOLD:
            status = AgentHealthStatus.DEGRADED
        else:
            status = AgentHealthStatus.ONLINE

        return {
            'agent_id': agent_id,
            'status': status,
            'is_online': is_online,
            'last_heartbeat': last_heartbeat.isoformat() if last_heartbeat else None,
            'task_stats': task_stats,
            'checked_at': datetime.utcnow().isoformat(),
        }

    def check_workspace_agents(self, workspace_id: int) -> List[Dict]:
        """检查工作区所有 Agent 健康状态"""
        agents = Agent.query.filter_by(
            workspace_id=workspace_id,
            status=AgentStatus.ACTIVE
        ).all()

        results = []
        for agent in agents:
            health = self.check_agent_health(agent.id)
            results.append(health)

        return results

    def _get_last_heartbeat(self, agent_id: int) -> Optional[datetime]:
        """获取最后心跳时间"""
        # 从任务租赁记录推断（表上无 leased_at，created_at 即租约创建时间）
        latest_lease = AgentTaskLease.query.filter_by(
            agent_id=agent_id
        ).order_by(
            AgentTaskLease.created_at.desc()
        ).first()

        if latest_lease:
            return latest_lease.created_at

        # 或者从 Agent 更新时间推断
        agent = Agent.query.get(agent_id)
        if agent:
            return agent.updated_at

        return None

    def _get_task_stats(self, agent_id: int) -> Dict:
        """获取任务统计"""
        now = datetime.utcnow()

        # 活跃租赁数（正在处理：active 且未过期）
        active_leases = AgentTaskLease.query.filter(
            AgentTaskLease.agent_id == agent_id,
            AgentTaskLease.active == True,  # noqa: E712
            AgentTaskLease.expires_at > now,
        ).count()

        # 今日完成任务数：表上无完成时间戳，以"已释放（active=False）且
        # 最近更新在今天"近似（updated_at 由 BaseModel 在写时刷新）
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        completed_today = AgentTaskLease.query.filter(
            AgentTaskLease.agent_id == agent_id,
            AgentTaskLease.active == False,  # noqa: E712
            AgentTaskLease.updated_at >= today_start
        ).count()

        return {
            'active': active_leases,
            'queued': active_leases,  # 简化为活跃数
            'completed_today': completed_today,
        }

    def get_health_summary(self, workspace_id: int) -> Dict:
        """获取健康状态汇总"""
        results = self.check_workspace_agents(workspace_id)

        total = len(results)
        online = sum(1 for r in results if r['status'] == AgentHealthStatus.ONLINE)
        offline = sum(1 for r in results if r['status'] == AgentHealthStatus.OFFLINE)
        degraded = sum(1 for r in results if r['status'] == AgentHealthStatus.DEGRADED)

        return {
            'total': total,
            'online': online,
            'offline': offline,
            'degraded': degraded,
            'online_rate': round(online / total * 100, 2) if total > 0 else 0,
            'agents': results,
        }


# 全局监控器实例
_health_monitor: Optional[AgentHealthMonitor] = None


def get_health_monitor() -> AgentHealthMonitor:
    """获取健康监控器单例"""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = AgentHealthMonitor()
    return _health_monitor
