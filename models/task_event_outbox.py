"""
任务事件 Outbox 模型
"""

from sqlalchemy import Column, String, Integer, BigInteger, ForeignKey, JSON, DateTime
from .base import BaseModel


class TaskEventOutbox(BaseModel):
    __tablename__ = 'task_event_outbox'

    event_id = Column(String(64), nullable=False, unique=True, index=True, comment='事件ID')
    event_type = Column(String(64), nullable=False, index=True, comment='事件类型')

    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, index=True, comment='项目ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), index=True, comment='组织ID')

    payload = Column(JSON, comment='事件负载')
    occurred_at = Column(DateTime, nullable=False, comment='发生时间')
    processed_at = Column(DateTime, comment='处理时间')

    def to_dict(self):
        data = super().to_dict()
        data['payload'] = data.get('payload') or {}
        return data
