"""
Organization event model.
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String
from .base import BaseModel


class OrganizationEvent(BaseModel):
    __tablename__ = 'organization_events'

    organization_id = Column(Integer, nullable=False, index=True, comment='组织ID')
    event_type = Column(String(64), nullable=False, index=True, comment='事件类型')

    source = Column(String(32), nullable=False, default='api', index=True, comment='事件来源')
    level = Column(String(16), nullable=False, default='info', index=True, comment='事件级别')

    actor_type = Column(String(32), comment='行为主体类型')
    actor_id = Column(String(64), comment='行为主体ID')
    actor_name = Column(String(128), comment='行为主体名称')

    target_type = Column(String(32), comment='目标类型')
    target_id = Column(String(64), comment='目标ID')

    project_id = Column(Integer, index=True, comment='关联项目ID')
    task_id = Column(BigInteger, index=True, comment='关联任务ID')

    message = Column(String(512), comment='事件摘要')
    payload = Column(JSON, comment='附加信息')
    occurred_at = Column(DateTime, nullable=False, index=True, comment='事件发生时间')
