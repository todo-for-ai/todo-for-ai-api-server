"""
Agent Secret 模型

支持多密钥版本、密钥轮换、KMS/Vault 集成
"""

import base64
import hashlib
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
    key_version = Column(String(32), nullable=False, default='primary', comment='加密密钥版本')
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

    def _get_encryption_manager(self):
        """获取加密管理器（延迟导入避免循环依赖）"""
        from core.secret_encryption import get_encryption_manager
        return get_encryption_manager()

    def encrypt(self, secret_value: str) -> tuple[str, str]:
        """
        加密 Secret 值

        Returns:
            (密文, 密钥版本)
        """
        manager = self._get_encryption_manager()
        ciphertext, key_version = manager.encrypt(secret_value)
        return ciphertext, key_version

    def decrypt(self) -> str:
        """解密 Secret 值"""
        manager = self._get_encryption_manager()
        return manager.decrypt(self.secret_encrypted, self.key_version)

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
        """从明文创建 Secret 实例"""
        normalized = str(secret_value)
        prefix = normalized[:8]
        secret_hash = hashlib.sha256(normalized.encode()).hexdigest()

        # 使用新的加密管理器
        from core.secret_encryption import get_encryption_manager
        manager = get_encryption_manager()
        encrypted, key_version = manager.encrypt(normalized)

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
            key_version=key_version,
            prefix=prefix,
            is_active=True,
            created_by_user_id=user_id,
            updated_by_user_id=user_id,
            created_by=created_by,
        )

    def reveal(self):
        """解密并返回 Secret 值"""
        return self.decrypt()

    def rotate_encryption(self, user_id: int):
        """
        轮换加密密钥

        使用当前密钥重新加密，用于密钥升级
        """
        # 解密旧值
        plaintext = self.reveal()

        # 使用新密钥重新加密
        from core.secret_encryption import get_encryption_manager
        manager = get_encryption_manager()
        self.secret_encrypted, self.key_version = manager.encrypt(plaintext)
        self.updated_by_user_id = user_id

    def to_dict(self, include_secret=False):
        data = super().to_dict()
        if not include_secret:
            data.pop('secret_hash', None)
            data.pop('secret_encrypted', None)
        return data
