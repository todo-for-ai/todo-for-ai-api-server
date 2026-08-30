"""
GitHub App 配置模型（GitHub App 化代码侧准备）

单例表：平台级 GitHub App 的凭据与安装状态。
App 化后仓库访问使用 installation token（短期、按安装授权），
替代单一 token 绑定；private_key / webhook_secret 均加密存储。
"""

from sqlalchemy import Column, String, Integer, Boolean, Text
from .base import BaseModel


class GitHubAppConfig(BaseModel):
    """GitHub App 平台级配置（约定 id=1 单例行）"""

    __tablename__ = 'github_app_configs'

    app_id = Column(String(64), comment='GitHub App ID（数值，以字符串存储）')
    slug = Column(String(255), comment='App slug（用于构造 App 页面 URL）')
    installation_id = Column(String(64), comment='安装实例 ID（安装事件回填）')
    account_login = Column(String(255), comment='安装归属账号（组织/用户 login）')
    private_key_encrypted = Column(Text, comment='App 私钥 PEM（加密存储）')
    webhook_secret_encrypted = Column(String(2000), comment='Webhook HMAC secret（加密存储）')
    installed = Column(Boolean, default=False, nullable=False, comment='是否已完成安装')

    def to_dict(self):
        result = super().to_dict(exclude=['private_key_encrypted', 'webhook_secret_encrypted'])
        result['has_private_key'] = bool(self.private_key_encrypted)
        result['has_webhook_secret'] = bool(self.webhook_secret_encrypted)
        return result
