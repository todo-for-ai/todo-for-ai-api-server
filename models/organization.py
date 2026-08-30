"""
组织模型
"""

import enum
import re
from datetime import datetime
from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, DateTime, UniqueConstraint, Boolean
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


class OrganizationRoleDefinition(BaseModel):
    """组织角色定义（支持自定义角色）"""

    __tablename__ = 'organization_role_definitions'
    __table_args__ = (
        UniqueConstraint('organization_id', 'key', name='uq_org_role_definition_org_key'),
    )

    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='组织ID')
    key = Column(String(64), nullable=False, comment='角色唯一键（组织内唯一）')
    name = Column(String(64), nullable=False, comment='角色显示名')
    description = Column(Text, nullable=True, comment='角色描述')
    content = Column(Text, nullable=True, comment='角色设定内容（Markdown）')
    is_system = Column(Boolean, nullable=False, default=False, comment='是否系统内置角色')
    is_active = Column(Boolean, nullable=False, default=True, comment='是否启用')

    organization = relationship('Organization', back_populates='role_definitions')
    member_bindings = relationship(
        'OrganizationMemberRole',
        back_populates='role',
        cascade='all, delete-orphan',
        lazy='dynamic'
    )

    def to_dict(self):
        result = super().to_dict()
        result['is_system'] = bool(self.is_system)
        result['is_active'] = bool(self.is_active)
        # 前端语义兼容：title 等同于 name
        result['title'] = result.get('name')
        return result


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
    role_definitions = relationship(
        'OrganizationRoleDefinition',
        back_populates='organization',
        cascade='all, delete-orphan',
        lazy='dynamic'
    )

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
    role_bindings = relationship(
        'OrganizationMemberRole',
        back_populates='member',
        cascade='all, delete-orphan',
        lazy='selectin'
    )

    def to_dict(self, include_user=False, include_roles=True):
        result = super().to_dict()
        result['role'] = self.role.value if self.role else None
        result['status'] = self.status.value if self.status else None
        if include_roles:
            result['roles'] = [
                binding.role.to_dict()
                for binding in self.role_bindings
                if binding.role and binding.role.is_active
            ]
        if include_user and self.user:
            result['user'] = self.user.to_public_dict()
        return result


class OrganizationMemberRole(BaseModel):
    """组织成员角色绑定（一个成员可绑定多个角色）"""

    __tablename__ = 'organization_member_roles'
    __table_args__ = (
        UniqueConstraint('member_id', 'role_id', name='uq_org_member_role_member_role'),
    )

    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='组织ID')
    member_id = Column(Integer, ForeignKey('organization_members.id'), nullable=False, index=True, comment='组织成员ID')
    role_id = Column(Integer, ForeignKey('organization_role_definitions.id'), nullable=False, index=True, comment='角色ID')

    organization = relationship('Organization', foreign_keys=[organization_id])
    member = relationship('OrganizationMember', back_populates='role_bindings')
    role = relationship('OrganizationRoleDefinition', back_populates='member_bindings')

    def to_dict(self, include_role=False):
        result = super().to_dict()
        if include_role and self.role:
            result['role'] = self.role.to_dict()
        return result
