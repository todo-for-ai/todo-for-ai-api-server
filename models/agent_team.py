"""
Agent 团队模型

支持多 Agent 组队协作
"""

import enum
from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentTeamStatus(enum.Enum):
    """团队状态"""

    ACTIVE = 'active'
    INACTIVE = 'inactive'
    ARCHIVED = 'archived'


class AgentTeam(BaseModel):
    """
    Agent 团队

    一个团队包含多个 Agent 成员，可以共同协作处理任务
    """

    __tablename__ = 'agent_teams'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='所属工作区ID')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True,
                                comment='创建者用户ID')

    # 基本信息
    name = Column(String(128), nullable=False, comment='团队名称')
    description = Column(Text, comment='团队描述')
    avatar_url = Column(String(512), comment='团队头像URL')

    # 配置
    config = Column(JSON, comment='团队配置（策略、规则等）')
    default_strategy = Column(String(32), default='sequential',
                              comment='默认编排策略：sequential, parallel, map_reduce, debate')

    # 状态
    status = Column(Enum(AgentTeamStatus), default=AgentTeamStatus.ACTIVE,
                    nullable=False, comment='状态')

    # 统计
    member_count = Column(Integer, default=0, comment='成员数量缓存')
    task_count = Column(Integer, default=0, comment='关联任务数量缓存')

    # 关系
    workspace = relationship('Organization', foreign_keys=[workspace_id])
    creator = relationship('User', foreign_keys=[created_by_user_id])
    members = relationship('AgentTeamMember', back_populates='team',
                           cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self, include_members=False):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        data['config'] = self.config or {}
        data['member_count'] = self.member_count or 0
        data['task_count'] = self.task_count or 0

        if include_members:
            data['members'] = [m.to_dict() for m in self.members.order_by(AgentTeamMember.order_index).all()]

        return data


class AgentTeamMemberRole(enum.Enum):
    """团队成员角色"""

    LEADER = 'leader'          # 团队负责人
    MEMBER = 'member'          # 普通成员
    SPECIALIST = 'specialist'  # 专家角色
    OBSERVER = 'observer'      # 观察员（只读）


class AgentTeamMember(BaseModel):
    """
    团队成员关系

    一个 Agent 可以加入多个团队，一个团队可以有多个 Agent
    """

    __tablename__ = 'agent_team_members'

    team_id = Column(Integer, ForeignKey('agent_teams.id'), nullable=False, index=True,
                     comment='团队ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True,
                      comment='Agent ID')
    added_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False,
                              comment='添加者用户ID')

    # 角色配置
    role = Column(Enum(AgentTeamMemberRole), default=AgentTeamMemberRole.MEMBER,
                  nullable=False, comment='角色')
    order_index = Column(Integer, default=0, comment='排序索引（用于顺序编排）')

    # 职责描述
    responsibility = Column(String(255), comment='在该团队中的职责描述')

    # 自定义配置
    config = Column(JSON, comment='成员特定配置（覆盖团队默认值）')

    # 是否接收团队通知
    notifications_enabled = Column(Boolean, default=True, comment='是否接收通知')

    # 关系
    team = relationship('AgentTeam', foreign_keys=[team_id], back_populates='members')
    agent = relationship('Agent', foreign_keys=[agent_id])
    added_by = relationship('User', foreign_keys=[added_by_user_id])

    def to_dict(self):
        data = super().to_dict()
        data['role'] = self.role.value if self.role else None
        data['config'] = self.config or {}
        data['notifications_enabled'] = bool(self.notifications_enabled)

        # 包含 Agent 基本信息
        if self.agent:
            data['agent'] = {
                'id': self.agent.id,
                'name': self.agent.name,
                'display_name': self.agent.display_name,
                'avatar_url': self.agent.avatar_url,
                'status': self.agent.status.value if self.agent.status else None,
                'capability_tags': self.agent.capability_tags or [],
            }

        return data
