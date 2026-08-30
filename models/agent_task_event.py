"""
Agent 任务事件模型
"""

from sqlalchemy import Column, String, Text, Integer, ForeignKey, BigInteger, DateTime, JSON
from .base import BaseModel


class AgentTaskEvent(BaseModel):
    __tablename__ = 'agent_task_events'

    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    attempt_id = Column(String(64), nullable=False, index=True, comment='Attempt ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')

    event_type = Column(String(32), nullable=False, index=True, comment='事件类型')
    seq = Column(Integer, nullable=False, default=1, comment='顺序号')
    event_timestamp = Column(DateTime, nullable=False, comment='事件时间')
    payload = Column(JSON, comment='事件载荷')
    message = Column(Text, comment='可读消息')
