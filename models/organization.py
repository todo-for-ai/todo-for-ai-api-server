"""
组织模型
"""

import enum
import re
from datetime import datetime
from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class OrganizationStatus(enum.Enum):
    """组织状态"""
    ACTIVE = 'active'
    ARCHIVED = 'archived'


class OrganizationRole(enum.Enum):
    """组织成员角色"""
    OWNER = 'owner'
    ADMIN = 'admin'
    MEMBER = 'member'
    VIEWER = 'viewer'


class OrganizationMemberStatus(enum.Enum):
    """组织成员状态"""
    ACTIVE = 'active'
    INVITED = 'invited'
    REMOVED = 'removed'


class Organization(BaseModel):
    """组织"""

    __tablename__ = 'organizations'

    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='组织拥有者')
    name = Column(String(255), nullable=False, comment='组织名称')
    slug = Column(String(255), unique=True, nullable=False, comment='组织唯一标识')
    description = Column(Text, comment='组织描述')
    status = Column(
        Enum(OrganizationStatus),
        default=OrganizationStatus.ACTIVE,
        nullable=False,
        comment='组织状态'
    )

    owner = relationship('User', back_populates='owned_organizations', foreign_keys=[owner_id])
    members = relationship(
        'OrganizationMember',
        back_populates='organization',
        cascade='all, delete-orphan',
        lazy='dynamic'
    )
    projects = relationship('Project', back_populates='organization', lazy='dynamic')

    @staticmethod
    def slugify(name: str) -> str:
        base = re.sub(r'[^a-zA-Z0-9]+', '-', (name or '').strip().lower()).strip('-')
        return base or f"org-{int(datetime.utcnow().timestamp())}"

    def to_dict(self, include_stats=False):
        result = super().to_dict()
        result['status'] = self.status.value if self.status else None
        if include_stats:
            result['member_count'] = self.members.filter(
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE
            ).count()
            result['project_count'] = self.projects.count()
        return result


class OrganizationMember(BaseModel):
    """组织成员"""

    __tablename__ = 'organization_members'
    __table_args__ = (
        UniqueConstraint('organization_id', 'user_id', name='uq_organization_member_org_user'),
    )

    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='组织ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='用户ID')
    role = Column(
        Enum(OrganizationRole),
        default=OrganizationRole.MEMBER,
        nullable=False,
        comment='成员角色'
    )
    status = Column(
        Enum(OrganizationMemberStatus),
        default=OrganizationMemberStatus.ACTIVE,
        nullable=False,
        comment='成员状态'
    )
    invited_by = Column(Integer, ForeignKey('users.id'), nullable=True, comment='邀请人用户ID')
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment='加入时间')

    organization = relationship('Organization', back_populates='members')
    user = relationship('User', foreign_keys=[user_id], back_populates='organization_memberships')
    inviter = relationship('User', foreign_keys=[invited_by])

    def to_dict(self, include_user=False):
        result = super().to_dict()
        result['role'] = self.role.value if self.role else None
        result['status'] = self.status.value if self.status else None
        if include_user and self.user:
            result['user'] = self.user.to_public_dict()
        return result
