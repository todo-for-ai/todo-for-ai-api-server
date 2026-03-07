"""
Agent Secret 模型
"""

import base64
import hashlib
import os
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, String, Boolean, Text, ForeignKey, BigInteger, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentSecret(BaseModel):
    """Agent 私密配置"""

    __tablename__ = 'agent_secrets'

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    name = Column(String(128), nullable=False, comment='配置名')
    secret_type = Column(String(32), nullable=False, default='api_key', comment='机密类型')
    scope_type = Column(String(32), nullable=False, default='agent_private', comment='作用域类型')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, index=True, comment='作用域项目ID')
    description = Column(Text, comment='机密说明')
    secret_hash = Column(String(64), nullable=False, comment='配置哈希')
    secret_encrypted = Column(Text, nullable=False, comment='密文')
    prefix = Column(String(12), nullable=False, comment='展示前缀')
    is_active = Column(Boolean, nullable=False, default=True, comment='是否有效')
    last_used_at = Column(DateTime, comment='最后使用时间')
    usage_count = Column(BigInteger, nullable=False, default=0, comment='使用次数')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='创建人')
    updated_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='更新人')

    agent = relationship('Agent', back_populates='secrets', foreign_keys=[agent_id])
    creator = relationship('User', foreign_keys=[created_by_user_id])
    updater = relationship('User', foreign_keys=[updated_by_user_id])
    project = relationship('Project', foreign_keys=[project_id])
    shares = relationship('AgentSecretShare', back_populates='secret', cascade='all, delete-orphan', lazy='dynamic')

    @staticmethod
    def _get_encryption_key():
        key = os.environ.get('TOKEN_ENCRYPTION_KEY')
        if not key:
            key = '2_e0DDiXi8afz4S1PIBTTHEUJkzxWbFWFtS-CMUGoY0='
        if isinstance(key, str):
            key = key.encode()
        return key

    @classmethod
    def _encrypt_secret(cls, secret_value):
        f = Fernet(cls._get_encryption_key())
        encrypted = f.encrypt(secret_value.encode())
        return base64.b64encode(encrypted).decode()

    @classmethod
    def _decrypt_secret(cls, encrypted_secret):
        f = Fernet(cls._get_encryption_key())
        encrypted_bytes = base64.b64decode(encrypted_secret.encode())
        return f.decrypt(encrypted_bytes).decode()

    @classmethod
    def from_plaintext(
        cls,
        *,
        agent_id,
        workspace_id,
        name,
        secret_value,
        user_id,
        created_by,
        secret_type='api_key',
        scope_type='agent_private',
        project_id=None,
        description=None,
    ):
        normalized = str(secret_value)
        prefix = normalized[:8]
        secret_hash = hashlib.sha256(normalized.encode()).hexdigest()
        encrypted = cls._encrypt_secret(normalized)
        return cls(
            agent_id=agent_id,
            workspace_id=workspace_id,
            name=name,
            secret_type=secret_type,
            scope_type=scope_type,
            project_id=project_id,
            description=description,
            secret_hash=secret_hash,
            secret_encrypted=encrypted,
            prefix=prefix,
            is_active=True,
            created_by_user_id=user_id,
            updated_by_user_id=user_id,
            created_by=created_by,
        )

    def reveal(self):
        return self._decrypt_secret(self.secret_encrypted)

    def to_dict(self, include_secret=False):
        data = super().to_dict()
        if not include_secret:
            data.pop('secret_hash', None)
            data.pop('secret_encrypted', None)
        return data
