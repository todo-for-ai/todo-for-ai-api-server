"""工作区 SSO 配置模型（Phase 4 企业能力）

配置化单点登录（OIDC 完整骨架 / SAML 预留），workspace 维度一对一：
- OIDC：issuer/authorize/token/userinfo 端点 + client 凭据（secret 加密存储）
- SAML：IdP 元数据字段预留（登录链路暂未实现，配置可存）
"""

from sqlalchemy import Column, String, Integer, ForeignKey, Boolean

from .base import BaseModel


class WorkspaceSSOConfig(BaseModel):
    """工作区单点登录配置"""

    __tablename__ = 'workspace_sso_configs'

    PROVIDER_OIDC = 'oidc'
    PROVIDER_SAML = 'saml'
    PROVIDERS = (PROVIDER_OIDC, PROVIDER_SAML)

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, unique=True,
                          index=True, comment='工作区ID（一对一）')
    provider = Column(String(20), nullable=False, default=PROVIDER_OIDC,
                      comment='SSO 协议: oidc/saml')
    enabled = Column(Boolean, default=False, nullable=False, comment='是否启用')

    # ── OIDC ──
    issuer = Column(String(500), comment='OIDC issuer')
    client_id = Column(String(200), comment='OIDC client_id')
    client_secret_encrypted = Column(String(2000), comment='OIDC client_secret（加密存储）')
    authorize_url = Column(String(500), comment='授权端点')
    token_url = Column(String(500), comment='Token 端点')
    userinfo_url = Column(String(500), comment='用户信息端点')
    redirect_uri = Column(String(500), comment='回调地址')

    # ── SAML（骨架预留）──
    idp_metadata_url = Column(String(500), comment='IdP 元数据地址（SAML 预留）')
    idp_entity_id = Column(String(255), comment='IdP entity ID（SAML 预留）')

    # ── 成员映射 ──
    default_role = Column(String(32), default='member', nullable=False,
                          comment='SSO 新用户加入工作区的默认角色（预留）')

    def to_dict(self, include_secret: bool = False):
        data = super().to_dict()
        data['has_client_secret'] = bool(self.client_secret_encrypted)
        if not include_secret:
            data.pop('client_secret_encrypted', None)
        return data
