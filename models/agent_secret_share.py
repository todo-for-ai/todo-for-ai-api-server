"""
Agent Secret Share 模型
"""

from sqlalchemy import Column, Integer, String, Boolean, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentSecretShare(BaseModel):
    """Agent 机密共享关系"""

    __tablename__ = 'agent_secret_shares'

    secret_id = Column(Integer, ForeignKey('agent_secrets.id'), nullable=False, index=True, comment='机密ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    owner_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='拥有方Agent ID')
    target_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='目标Agent ID')
    access_mode = Column(String(32), nullable=False, default='read', comment='访问模式')
    expires_at = Column(DateTime, comment='过期时间')
    is_active = Column(Boolean, nullable=False, default=True, comment='是否有效')
    granted_reason = Column(Text, comment='授权原因')
    granted_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='授权人')
    revoked_by_user_id = Column(Integer, ForeignKey('users.id'), comment='撤销人')

    secret = relationship('AgentSecret', back_populates='shares', foreign_keys=[secret_id])
    owner_agent = relationship('Agent', foreign_keys=[owner_agent_id])
    target_agent = relationship('Agent', foreign_keys=[target_agent_id])
    granter = relationship('User', foreign_keys=[granted_by_user_id])
    revoker = relationship('User', foreign_keys=[revoked_by_user_id])

    def to_dict(self, include_agents=False):
        data = super().to_dict()
        if include_agents:
            owner_name = None
            target_name = None
            if self.owner_agent:
                owner_name = self.owner_agent.display_name or self.owner_agent.name
            if self.target_agent:
                target_name = self.target_agent.display_name or self.target_agent.name
            data['owner_agent_name'] = owner_name
            data['target_agent_name'] = target_name
        return data
