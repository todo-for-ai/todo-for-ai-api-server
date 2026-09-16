"""外部系统连接器配置模型（Phase 4 互操作写回侧）

每个工作区可按 provider（linear/gitlab/jira/lark/wecom/generic）配置一个连接器：
- secret：webhook 签名密钥/令牌/应用凭据（JSON 或明文，加密存储），用于入站事件验签
- config_json：非敏感路由与集成配置（IM 群→项目映射、API base、字段映射模板等）
- default_project_id：外部事项导入落地的默认项目
"""

from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime, JSON

from .base import BaseModel


class ExternalConnectorConfig(BaseModel):
    """外部同步连接器配置"""

    __tablename__ = 'external_connector_configs'

    PROVIDER_LINEAR = 'linear'
    PROVIDER_GITLAB = 'gitlab'
    PROVIDER_JIRA = 'jira'
    PROVIDER_LARK = 'lark'
    PROVIDER_WECOM = 'wecom'
    PROVIDER_GENERIC = 'generic'
    PROVIDERS = (PROVIDER_LINEAR, PROVIDER_GITLAB, PROVIDER_JIRA,
                 PROVIDER_LARK, PROVIDER_WECOM, PROVIDER_GENERIC)

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='工作区ID')
    provider = Column(String(20), nullable=False, index=True,
                      comment='连接器: linear/gitlab/jira')
    enabled = Column(Boolean, default=False, nullable=False, comment='是否启用')
    secret_encrypted = Column(String(2000), comment='webhook 签名密钥/令牌（加密存储）')
    default_project_id = Column(Integer, ForeignKey('projects.id'), nullable=True,
                                comment='导入任务落地的默认项目')
    config_json = Column(JSON, comment='非敏感集成配置（路由映射/API base/字段映射模板）')
    last_synced_at = Column(DateTime, comment='最近一次入站事件处理时间')

    def to_dict(self, include_secret: bool = False):
        data = super().to_dict()
        data['has_secret'] = bool(self.secret_encrypted)
        if not include_secret:
            data.pop('secret_encrypted', None)
        return data
