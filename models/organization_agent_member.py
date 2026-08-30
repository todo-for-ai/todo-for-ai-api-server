"""
组织 Agent 成员关系模型
"""

import enum
from datetime import datetime
from sqlalchemy import Column, Integer, ForeignKey, Enum, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class OrganizationAgentMemberStatus(enum.Enum):
    """组织 Agent 成员状态"""

    INVITED = 'invited'
    ACTIVE = 'active'
    REMOVED = 'removed'
    REJECTED = 'rejected'


class OrganizationAgentMember(BaseModel):
    """组织与 Agent 的成员关系"""

    __tablename__ = 'organization_agent_members'
    __table_args__ = (
        UniqueConstraint('organization_id', 'agent_id', name='uq_org_agent_member_org_agent'),
    )

    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='组织ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    invited_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, comment='邀请人用户ID')
    status = Column(
        Enum(OrganizationAgentMemberStatus),
        default=OrganizationAgentMemberStatus.ACTIVE,
        nullable=False,
        comment='成员状态'
    )
    joined_at = Column(DateTime, nullable=True, comment='加入时间')
    responded_at = Column(DateTime, nullable=True, comment='响应邀请时间')

    organization = relationship('Organization', foreign_keys=[organization_id])
    agent = relationship('Agent', foreign_keys=[agent_id])
    inviter = relationship('User', foreign_keys=[invited_by_user_id])

    def to_dict(self, include_agent=False):
        result = super().to_dict()
        result['status'] = self.status.value if self.status else None
        if include_agent and self.agent:
            result['agent'] = self.agent.to_dict()
        return result

    def mark_active(self):
        self.status = OrganizationAgentMemberStatus.ACTIVE
        self.joined_at = datetime.utcnow()
        self.responded_at = datetime.utcnow()

    def mark_rejected(self):
        self.status = OrganizationAgentMemberStatus.REJECTED
        self.responded_at = datetime.utcnow()
