"""
站内通知模型
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Text, BigInteger
from sqlalchemy.orm import relationship
from .base import BaseModel


class UserNotification(BaseModel):
    __tablename__ = 'user_notifications'

    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='接收人用户ID')
    event_id = Column(String(64), nullable=False, index=True, comment='原始事件ID')
    event_type = Column(String(64), nullable=False, index=True, comment='事件类型')
    category = Column(String(32), nullable=False, default='task', comment='通知分类')

    title = Column(String(255), nullable=False, comment='通知标题')
    body = Column(Text, comment='通知正文')
    level = Column(String(16), nullable=False, default='info', comment='通知级别')
    link_url = Column(String(500), comment='跳转链接')

    resource_type = Column(String(32), nullable=False, default='task', comment='资源类型')
    resource_id = Column(BigInteger, index=True, comment='资源ID')

    actor_user_id = Column(Integer, ForeignKey('users.id'), index=True, comment='触发人用户ID')
    project_id = Column(Integer, ForeignKey('projects.id'), index=True, comment='关联项目ID')
    organization_id = Column(Integer, ForeignKey('organizations.id'), index=True, comment='关联组织ID')

    extra_payload = Column(JSON, comment='扩展负载')
    read_at = Column(DateTime, index=True, comment='已读时间')
    archived_at = Column(DateTime, index=True, comment='归档时间')
    dedup_key = Column(String(255), nullable=False, unique=True, comment='去重键')

    user = relationship('User', foreign_keys=[user_id])
    actor = relationship('User', foreign_keys=[actor_user_id])

    def to_dict(self):
        data = super().to_dict()
        data['extra_payload'] = data.get('extra_payload') or {}
        data['is_read'] = bool(self.read_at)
        return data
