"""
通知 Channel 模型
"""

import enum
from sqlalchemy import Column, String, Integer, JSON, Boolean
from .base import BaseModel


class NotificationScopeType(enum.Enum):
    USER = 'user'
    ORGANIZATION = 'organization'
    PROJECT = 'project'


class NotificationChannelType(enum.Enum):
    IN_APP = 'in_app'
    WEBHOOK = 'webhook'
    FEISHU = 'feishu'
    WECOM = 'wecom'
    DINGTALK = 'dingtalk'


class NotificationChannel(BaseModel):
    __tablename__ = 'notification_channels'

    scope_type = Column(String(16), nullable=False, index=True, comment='范围类型')
    scope_id = Column(Integer, nullable=False, index=True, comment='范围ID')

    name = Column(String(128), nullable=False, comment='通道名称')
    channel_type = Column(String(16), nullable=False, comment='通道类型')

    enabled = Column(Boolean, nullable=False, default=True, comment='是否启用')
    is_default = Column(Boolean, nullable=False, default=False, comment='是否默认通道')
    events = Column(JSON, comment='订阅事件列表')
    config = Column(JSON, comment='通道配置')

    created_by_user_id = Column(Integer, nullable=False, index=True, comment='创建人用户ID')
    updated_by_user_id = Column(Integer, nullable=False, index=True, comment='更新人用户ID')

    def to_dict(self):
        data = super().to_dict()
        data['scope_type'] = self.scope_type.value if hasattr(self.scope_type, 'value') else str(self.scope_type or '').lower() or None
        data['channel_type'] = self.channel_type.value if hasattr(self.channel_type, 'value') else str(self.channel_type or '').lower() or None
        data['enabled'] = bool(self.enabled)
        data['is_default'] = bool(self.is_default)
        data['events'] = data.get('events') or []
        data['config'] = data.get('config') or {}
        return data
