"""
通知事件日志模型
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, BigInteger
from sqlalchemy.orm import relationship
from .base import BaseModel


class NotificationEvent(BaseModel):
    __tablename__ = 'notification_events'

    event_id = Column(String(64), nullable=False, unique=True, index=True, comment='通知事件ID')
    event_type = Column(String(64), nullable=False, index=True, comment='通知事件类型')
    category = Column(String(32), nullable=False, default='task', comment='通知事件分类')

    actor_user_id = Column(Integer, ForeignKey('users.id'), index=True, comment='触发人用户ID')

    resource_type = Column(String(32), nullable=False, default='task', comment='资源类型')
    resource_id = Column(BigInteger, index=True, comment='资源ID')
    project_id = Column(Integer, ForeignKey('projects.id'), index=True, comment='关联项目ID')
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True, comment='关联组织ID')

    payload = Column(JSON, comment='原始事件负载')
    target_user_ids = Column(JSON, comment='目标用户ID列表')

    dispatch_state = Column(String(16), nullable=False, default='pending', comment='投递状态')
    in_app_processed_at = Column(DateTime, comment='站内通知生成时间')
    external_queued_at = Column(DateTime, comment='外部通知入队时间')
    external_last_dispatched_at = Column(DateTime, comment='最后外部投递时间')

    actor = relationship('User', foreign_keys=[actor_user_id])

    def to_dict(self):
        data = super().to_dict()
        data['payload'] = data.get('payload') or {}
        data['target_user_ids'] = data.get('target_user_ids') or []
        return data
