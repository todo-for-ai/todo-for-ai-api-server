"""
Agent 团队与项目关联模型
"""

from sqlalchemy import Column, Integer, ForeignKey, String, JSON
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentTeamProject(BaseModel):
    """
    团队与项目的关联

    一个团队可以关联多个项目，一个项目可以有多个团队
    """

    __tablename__ = 'agent_team_projects'

    team_id = Column(Integer, ForeignKey('agent_teams.id'), nullable=False, index=True,
                     comment='团队ID')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, index=True,
                        comment='项目ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='工作区ID')
    added_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False,
                              comment='添加者用户ID')

    # 关联配置
    config = Column(JSON, comment='团队在该项目中的特定配置')
    role = Column(String(32), default='collaborator',
                  comment='团队在项目中的角色：collaborator, owner, reviewer')

    # 关系
    team = relationship('AgentTeam', foreign_keys=[team_id])
    project = relationship('Project', foreign_keys=[project_id])
    workspace = relationship('Organization', foreign_keys=[workspace_id])
    added_by = relationship('User', foreign_keys=[added_by_user_id])

    def to_dict(self):
        data = super().to_dict()
        data['config'] = self.config or {}

        # 包含团队基本信息
        if self.team:
            data['team'] = {
                'id': self.team.id,
                'name': self.team.name,
                'description': self.team.description,
                'avatar_url': self.team.avatar_url,
                'member_count': self.team.member_count or 0,
            }

        # 包含项目基本信息
        if self.project:
            data['project'] = {
                'id': self.project.id,
                'name': self.project.name,
                'color': getattr(self.project, 'color', None),
            }

        return data
