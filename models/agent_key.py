"""
Agent Key 模型
"""

import hashlib
import secrets
from datetime import datetime
from sqlalchemy import Column, String, Text, Boolean, DateTime, Integer, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from .base import BaseModel
from .api_token import ApiToken


class AgentKey(BaseModel):
    """Agent 长期凭证"""

    __tablename__ = 'agent_keys'

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='创建者用户ID')

    name = Column(String(128), nullable=False, comment='Key 名称')
    prefix = Column(String(16), nullable=False, index=True, comment='Key 前缀')
    key_hash = Column(String(64), nullable=False, unique=True, comment='Key 哈希')
    key_encrypted = Column(Text, nullable=False, comment='加密后的Key')

    is_active = Column(Boolean, nullable=False, default=True, comment='是否激活')
    revoked_at = Column(DateTime, comment='撤销时间')
    last_used_at = Column(DateTime, comment='最后使用时间')
    usage_count = Column(BigInteger, nullable=False, default=0, comment='使用次数')

    agent = relationship('Agent', back_populates='keys')

    @classmethod
    def generate_key(cls, name, workspace_id, agent_id, created_by_user_id):
        """生成新的 Agent Key"""
        raw = f"agk_{secrets.token_urlsafe(36)}"
        key_hash = hashlib.sha256(raw.encode()).hexdigest()

        row = cls(
            name=name,
            workspace_id=workspace_id,
            agent_id=agent_id,
            created_by_user_id=created_by_user_id,
            prefix=raw[:12],
            key_hash=key_hash,
            key_encrypted=ApiToken._encrypt_token(raw),
            is_active=True,
        )
        return row, raw

    @classmethod
    def verify_key(cls, raw_key):
        """验证 Agent Key"""
        if not raw_key:
            return None

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        row = cls.query.filter_by(key_hash=key_hash, is_active=True).first()
        if not row:
            return None

        if row.revoked_at is not None:
            return None

        row.last_used_at = datetime.utcnow()
        row.usage_count = (row.usage_count or 0) + 1
        row.save()
        return row

    def reveal(self):
        """返回明文 key"""
        return ApiToken._decrypt_token(self.key_encrypted)

    def revoke(self):
        self.is_active = False
        self.revoked_at = datetime.utcnow()
        self.save()

    def to_dict(self):
        data = super().to_dict()
        data.pop('key_hash', None)
        data.pop('key_encrypted', None)
        return data
