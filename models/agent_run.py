"""
Agent 运行记录模型
"""

import enum
from sqlalchemy import Column, String, Integer, ForeignKey, JSON, DateTime, Text
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentRunState(enum.Enum):
    QUEUED = 'queued'
    LEASED = 'leased'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


class AgentRun(BaseModel):
    __tablename__ = 'agent_runs'

    run_id = Column(String(64), nullable=False, unique=True, index=True, comment='运行ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    trigger_id = Column(Integer, ForeignKey('agent_triggers.id'), nullable=False, index=True, comment='触发器ID')

    trigger_reason = Column(String(64), nullable=False, comment='触发原因')
    input_payload = Column(JSON, comment='触发上下文')

    state = Column(String(16), nullable=False, default=AgentRunState.QUEUED.value, comment='运行状态')

    scheduled_at = Column(DateTime, nullable=False, comment='调度时间')
    started_at = Column(DateTime, comment='开始时间')
    ended_at = Column(DateTime, comment='结束时间')

    lease_id = Column(String(64), index=True, comment='租约ID')
    lease_expires_at = Column(DateTime, comment='租约过期时间')
    attempt_count = Column(Integer, nullable=False, default=0, comment='尝试次数')

    failure_code = Column(String(64), comment='失败码')
    failure_reason = Column(Text, comment='失败原因')

    idempotency_key = Column(String(128), nullable=False, unique=True, index=True, comment='幂等键')

    agent = relationship('Agent', foreign_keys=[agent_id])
    trigger = relationship('AgentTrigger', foreign_keys=[trigger_id])

    def to_dict(self):
        data = super().to_dict()
        data['state'] = str(self.state or '').lower() or None
        return data
