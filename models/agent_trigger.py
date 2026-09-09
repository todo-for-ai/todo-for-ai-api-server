"""
Agent 触发器模型
"""

import enum
from sqlalchemy import Column, String, Integer, ForeignKey, JSON, DateTime, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentTriggerType(enum.Enum):
    TASK_EVENT = 'task_event'
    REPO_EVENT = 'repo_event'
    CRON = 'cron'


class AgentTriggerAction(enum.Enum):
    RUN_AGENT = 'run_agent'
    CREATE_TASK = 'create_task'


class AgentMisfirePolicy(enum.Enum):
    SKIP = 'skip'
    CATCH_UP_ONCE = 'catch_up_once'


class AgentTrigger(BaseModel):
    __tablename__ = 'agent_triggers'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')

    name = Column(String(128), nullable=False, comment='触发器名称')
    trigger_type = Column(String(16), nullable=False, comment='触发器类型')
    enabled = Column(Boolean, nullable=False, default=True, comment='是否启用')
    priority = Column(Integer, nullable=False, default=100, comment='优先级')

    task_event_types = Column(JSON, comment='任务事件类型列表')
    task_filter = Column(JSON, comment='任务过滤条件')

    cron_expr = Column(String(64), comment='Cron表达式(UTC)')
    timezone = Column(String(64), nullable=False, default='UTC', comment='时区')
    misfire_policy = Column(String(24), nullable=False, default=AgentMisfirePolicy.CATCH_UP_ONCE.value, comment='错过触发策略')
    catch_up_window_seconds = Column(Integer, nullable=False, default=300, comment='补偿窗口秒数')
    dedup_window_seconds = Column(Integer, nullable=False, default=60, comment='触发去重窗口秒数')

    action = Column(String(16), nullable=False, default=AgentTriggerAction.RUN_AGENT.value,
                    server_default=AgentTriggerAction.RUN_AGENT.value, comment='触发动作: run_agent/create_task')
    action_payload = Column(JSON, comment='动作参数（create_task: project_id/title/description/priority/tags）')
    last_fired_key = Column(String(80), comment='create_task 动作的幂等键（最近一次触发）')

    last_triggered_at = Column(DateTime, comment='最近触发时间')
    next_fire_at = Column(DateTime, comment='下一次触发时间')

    agent = relationship('Agent', foreign_keys=[agent_id])

    def to_dict(self):
        data = super().to_dict()
        data['trigger_type'] = str(self.trigger_type or '').lower() or None
        data['enabled'] = bool(self.enabled)
        data['misfire_policy'] = str(self.misfire_policy or '').lower() or None
        data['task_event_types'] = self.task_event_types or []
        data['task_filter'] = self.task_filter or {}
        data['action'] = str(self.action or AgentTriggerAction.RUN_AGENT.value).lower() or None
        data['action_payload'] = self.action_payload or {}
        return data
