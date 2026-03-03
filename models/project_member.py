"""
项目成员模型
"""

import enum
from datetime import datetime
from sqlalchemy import Column, Enum, Integer, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class ProjectMemberRole(enum.Enum):
    """项目成员角色"""
    OWNER = 'owner'
    MAINTAINER = 'maintainer'
    MEMBER = 'member'
    VIEWER = 'viewer'


class ProjectMemberStatus(enum.Enum):
    """项目成员状态"""
    ACTIVE = 'active'
    INVITED = 'invited'
    REMOVED = 'removed'


class ProjectMember(BaseModel):
    """项目成员"""

    __tablename__ = 'project_members'
    __table_args__ = (
        UniqueConstraint('project_id', 'user_id', name='uq_project_member_project_user'),
    )

    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, index=True, comment='项目ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='用户ID')
    role = Column(
        Enum(ProjectMemberRole),
        default=ProjectMemberRole.MEMBER,
        nullable=False,
        comment='成员角色'
    )
    status = Column(
        Enum(ProjectMemberStatus),
        default=ProjectMemberStatus.ACTIVE,
        nullable=False,
        comment='成员状态'
    )
    invited_by = Column(Integer, ForeignKey('users.id'), nullable=True, comment='邀请人用户ID')
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment='加入时间')

    project = relationship('Project', back_populates='members')
    user = relationship('User', foreign_keys=[user_id], back_populates='project_memberships')
    inviter = relationship('User', foreign_keys=[invited_by])

    def to_dict(self, include_user=False):
        result = super().to_dict()
        result['role'] = self.role.value if self.role else None
        result['status'] = self.status.value if self.status else None
        if include_user and self.user:
            result['user'] = self.user.to_public_dict()
        return result
