"""外部系统连接器配置模型（Phase 4 互操作写回侧）

每个工作区可按 provider（linear/gitlab/jira）配置一个连接器：
- secret：webhook 签名/令牌（加密存储），用于入站事件验签
- default_project_id：外部事项导入落地的默认项目
"""

from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime

from .base import BaseModel


class ExternalConnectorConfig(BaseModel):
    """外部同步连接器配置"""

    __tablename__ = 'external_connector_configs'

    PROVIDER_LINEAR = 'linear'
    PROVIDER_GITLAB = 'gitlab'
    PROVIDER_JIRA = 'jira'
    PROVIDERS = (PROVIDER_LINEAR, PROVIDER_GITLAB, PROVIDER_JIRA)

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='工作区ID')
    provider = Column(String(20), nullable=False, index=True,
                      comment='连接器: linear/gitlab/jira')
    enabled = Column(Boolean, default=False, nullable=False, comment='是否启用')
    secret_encrypted = Column(String(2000), comment='webhook 签名密钥/令牌（加密存储）')
    default_project_id = Column(Integer, ForeignKey('projects.id'), nullable=True,
                                comment='导入任务落地的默认项目')
    last_synced_at = Column(DateTime, comment='最近一次入站事件处理时间')

    def to_dict(self, include_secret: bool = False):
        data = super().to_dict()
        data['has_secret'] = bool(self.secret_encrypted)
        if not include_secret:
            data.pop('secret_encrypted', None)
        return data
