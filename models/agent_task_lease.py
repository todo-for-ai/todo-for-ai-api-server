"""
Agent 任务租约模型
"""

from sqlalchemy import Column, String, Boolean, Integer, ForeignKey, BigInteger, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentTaskLease(BaseModel):
    __tablename__ = 'agent_task_leases'
    __table_args__ = (
        UniqueConstraint('task_id', 'active', name='uq_agent_task_leases_task_active'),
    )

    lease_id = Column(String(64), nullable=False, unique=True, index=True, comment='租约ID')
    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    attempt_id = Column(String(64), nullable=False, index=True, comment='Attempt ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')

    expires_at = Column(DateTime, nullable=False, index=True, comment='过期时间')
    active = Column(Boolean, nullable=False, default=True, comment='是否激活')
    version = Column(Integer, nullable=False, default=1, comment='版本号')

    task = relationship('Task', foreign_keys=[task_id])
    agent = relationship('Agent', foreign_keys=[agent_id])
