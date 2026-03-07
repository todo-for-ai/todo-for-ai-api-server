"""
Agent Session 模型
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from sqlalchemy import Column, String, DateTime, Boolean, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentSession(BaseModel):
    """Agent 运行时短期会话"""

    __tablename__ = 'agent_sessions'

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    token_hash = Column(String(64), nullable=False, unique=True, index=True, comment='会话令牌哈希')
    token_prefix = Column(String(16), nullable=False, comment='会话令牌前缀')

    expires_at = Column(DateTime, nullable=False, comment='过期时间')
    revoked_at = Column(DateTime, comment='撤销时间')
    is_active = Column(Boolean, nullable=False, default=True, comment='是否激活')

    agent = relationship('Agent', foreign_keys=[agent_id])

    @classmethod
    def create_session(cls, agent_id, workspace_id, ttl_seconds=900):
        raw = f"ags_{secrets.token_urlsafe(32)}"
        now = datetime.utcnow()
        row = cls(
            agent_id=agent_id,
            workspace_id=workspace_id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            token_prefix=raw[:12],
            expires_at=now + timedelta(seconds=ttl_seconds),
            is_active=True,
        )
        return row, raw

    @classmethod
    def verify_session_token(cls, raw_token):
        if not raw_token:
            return None

        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        row = cls.query.filter_by(token_hash=token_hash, is_active=True).first()
        if not row:
            return None

        now = datetime.utcnow()
        if row.revoked_at is not None or row.expires_at <= now:
            return None
        return row
