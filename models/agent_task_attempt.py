"""
Agent 任务 Attempt 模型
"""

import enum
from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, BigInteger, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentTaskAttemptState(enum.Enum):
    CREATED = 'created'
    ACTIVE = 'active'
    COMMITTED = 'committed'
    ABORTED = 'aborted'


class AgentTaskAttempt(BaseModel):
    __tablename__ = 'agent_task_attempts'

    attempt_id = Column(String(64), nullable=False, unique=True, index=True, comment='Attempt 唯一标识')
    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')

    state = Column(Enum(AgentTaskAttemptState), nullable=False, default=AgentTaskAttemptState.CREATED, comment='Attempt 状态')
    lease_id = Column(String(64), nullable=False, index=True, comment='关联租约ID')
    started_at = Column(DateTime, nullable=False, comment='开始时间')
    ended_at = Column(DateTime, comment='结束时间')

    failure_code = Column(String(64), comment='失败码')
    failure_reason = Column(Text, comment='失败原因')

    task = relationship('Task', foreign_keys=[task_id])
    agent = relationship('Agent', foreign_keys=[agent_id])

    def to_dict(self):
        data = super().to_dict()
        data['state'] = self.state.value if self.state else None
        return data
