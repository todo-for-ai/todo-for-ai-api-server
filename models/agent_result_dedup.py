"""
Agent 结果去重模型
"""

from sqlalchemy import Column, String, Integer, ForeignKey, BigInteger, DateTime
from .base import BaseModel


class AgentResultDedup(BaseModel):
    __tablename__ = 'agent_result_dedup'

    idempotency_key = Column(String(128), nullable=False, unique=True, index=True, comment='幂等键')
    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    attempt_id = Column(String(64), nullable=False, index=True, comment='Attempt ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    committed_at = Column(DateTime, nullable=False, comment='提交时间')
