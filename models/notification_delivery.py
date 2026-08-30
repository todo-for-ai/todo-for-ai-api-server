"""
通知投递记录模型
"""

import enum
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text, JSON
from sqlalchemy.orm import relationship
from .base import BaseModel


class NotificationDeliveryStatus(enum.Enum):
    PENDING = 'pending'
    SENT = 'sent'
    FAILED = 'failed'
    RETRYING = 'retrying'
    DEAD = 'dead'


class NotificationDelivery(BaseModel):
    __tablename__ = 'notification_deliveries'

    event_type = Column(String(64), nullable=False, index=True, comment='事件类型')
    event_id = Column(String(64), nullable=False, index=True, comment='事件ID')

    channel_id = Column(Integer, ForeignKey('notification_channels.id'), nullable=False, index=True, comment='通道ID')

    status = Column(String(16), nullable=False, default=NotificationDeliveryStatus.PENDING.value, comment='投递状态')
    attempts = Column(Integer, nullable=False, default=0, comment='尝试次数')
    next_retry_at = Column(DateTime, comment='下次重试时间')
    delivered_at = Column(DateTime, comment='投递成功时间')
    last_error_at = Column(DateTime, comment='最后错误时间')

    response_code = Column(Integer, comment='响应状态码')
    response_excerpt = Column(Text, comment='响应片段')
    request_payload = Column(JSON, comment='请求负载快照')

    channel = relationship('NotificationChannel', foreign_keys=[channel_id])

    def to_dict(self):
        data = super().to_dict()
        data['status'] = str(self.status or '').lower() or None
        data['request_payload'] = data.get('request_payload') or {}
        return data
