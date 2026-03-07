"""
Agent 通知回执模型
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentNotificationReceipt(BaseModel):
    __tablename__ = 'agent_notification_receipts'

    event_id = Column(String(64), nullable=False, index=True, comment='通知事件ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    first_pulled_at = Column(DateTime, comment='首次拉取时间')
    last_pulled_at = Column(DateTime, comment='最后拉取时间')
    acked_at = Column(DateTime, comment='确认消费时间')

    agent = relationship('Agent', foreign_keys=[agent_id])

    def to_dict(self):
        return super().to_dict()
