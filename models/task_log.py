"""
任务日志模型
"""

import enum
from sqlalchemy import Column, BigInteger, String, Text, Enum, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class TaskLogActorType(enum.Enum):
    """日志记录者类型"""

    HUMAN = 'human'
    AGENT = 'agent'
    SYSTEM = 'system'


class TaskLog(BaseModel):
    """任务日志（追加写）"""

    __tablename__ = 'task_logs'

    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    actor_type = Column(Enum(TaskLogActorType), nullable=False, comment='记录者类型')
    actor_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True, comment='记录者用户ID')
    actor_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, index=True, comment='记录者Agent ID')
    content = Column(Text, nullable=False, comment='日志内容')
    content_type = Column(String(32), nullable=False, default='text/markdown', comment='内容类型')

    task = relationship('Task', foreign_keys=[task_id])
    actor_user = relationship('User', foreign_keys=[actor_user_id])
    actor_agent = relationship('Agent', foreign_keys=[actor_agent_id])

    def to_dict(self):
        data = super().to_dict()
        data['actor_type'] = self.actor_type.value if self.actor_type else None
        return data
