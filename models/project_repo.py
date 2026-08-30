"""
项目仓库绑定模型（P1.1 代码平面）

把平台项目绑定到 Git 托管商仓库，是"任务 → 代码 → PR → 合并回写"闭环的起点。
"""

from sqlalchemy import Column, String, Integer, ForeignKey
from .base import BaseModel


class ProjectRepoBinding(BaseModel):
    """项目与 Git 仓库的绑定关系（当前仅支持 GitHub）"""

    __tablename__ = 'project_repo_bindings'

    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, unique=True, index=True, comment='项目ID（一对一）')
    provider = Column(String(20), nullable=False, default='github', comment='托管商: github')
    repo_owner = Column(String(255), nullable=False, comment='仓库归属（用户名/组织）')
    repo_name = Column(String(255), nullable=False, comment='仓库名')
    default_branch = Column(String(255), nullable=False, default='main', comment='默认分支（PR 的 base）')
    # 绑定级 token（加密存储）；为空时回退到部署级 GITHUB_TOKEN 环境变量
    token_encrypted = Column(String(2000), comment='仓库访问 token（加密）')

    def __repr__(self):
        return f'<ProjectRepoBinding {self.id}: project={self.project_id} {self.repo_owner}/{self.repo_name}>'

    @property
    def repo_full_name(self):
        return f'{self.repo_owner}/{self.repo_name}'

    def to_dict(self):
        result = super().to_dict(exclude=['token_encrypted'])
        result['repo_full_name'] = self.repo_full_name
        result['has_binding_token'] = bool(self.token_encrypted)
        return result
