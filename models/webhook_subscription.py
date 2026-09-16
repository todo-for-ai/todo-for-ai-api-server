"""Webhook 订阅（出站推送）：平台事件 → 外部系统，与 connectors 入站构成双向闭环。

- workspace + url 一条订阅，events 声明感兴趣的事件类型（'*' 全量）；
- secret 加密存储，派发时以 `t=<ts>,v1=<hmac_sha256(secret, f"{ts}.{body}")>`
  签名（X-Todo4AI-Signature 头）；
- 每次派发（含重试终态）落 WebhookDelivery 供可观测与排障。
"""

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, BigInteger, ForeignKey

from .base import BaseModel


class WebhookSubscription(BaseModel):
    """工作区级出站 Webhook 订阅"""

    __tablename__ = 'webhook_subscriptions'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='工作区ID')
    url = Column(String(512), nullable=False, comment='推送目标URL（必须 https 或 http）')
    events = Column(JSON, nullable=False, comment='订阅事件类型列表，["*"] 表示全量')
    secret_encrypted = Column(String(2000), comment='HMAC 签名密钥（加密存储）')
    active = Column(Boolean, default=True, nullable=False, comment='是否启用')
    description = Column(String(200), comment='用途描述')

    def to_dict(self, include_secret: bool = False):
        data = super().to_dict()
        data['has_secret'] = bool(self.secret_encrypted)
        if not include_secret:
            data.pop('secret_encrypted', None)
        return data


class WebhookDelivery(BaseModel):
    """一次 webhook 派发的终态记录（含重试聚合）"""

    __tablename__ = 'webhook_deliveries'

    subscription_id = Column(Integer, ForeignKey('webhook_subscriptions.id'), nullable=False,
                             index=True, comment='订阅ID')
    event_type = Column(String(64), nullable=False, index=True, comment='事件类型')
    ok = Column(Boolean, nullable=False, comment='最终是否成功')
    status_code = Column(Integer, comment='最后一次 HTTP 状态码')
    attempts = Column(Integer, nullable=False, default=1, comment='尝试次数')
    error = Column(String(500), comment='最后一次错误摘要')
    duration_ms = Column(Integer, comment='总耗时（毫秒）')

    def to_dict(self, include_secret: bool = False):
        return super().to_dict()
