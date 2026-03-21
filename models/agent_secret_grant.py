"""
Agent secret grant model.
"""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .base import BaseModel


class AgentSecretGrant(BaseModel):
    __tablename__ = 'agent_secret_grants'

    grant_id = Column(String(64), nullable=False, unique=True, index=True, comment='Grant unique ID')
    secret_id = Column(Integer, ForeignKey('agent_secrets.id'), nullable=False, index=True, comment='Secret ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='Workspace ID')
    from_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Grant source agent ID')
    to_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Grant target agent ID')

    chain_id = Column(Integer, index=True, comment='Optional chain ID')
    task_id = Column(BigInteger, ForeignKey('tasks.id'), index=True, comment='Optional task ID')
    attempt_id = Column(String(64), index=True, comment='Optional attempt ID')

    grant_mode = Column(String(32), nullable=False, default='ephemeral', comment='Grant mode')
    max_uses = Column(Integer, comment='Maximum allowed consume count')
    used_count = Column(Integer, nullable=False, default=0, comment='Current consume count')
    expires_at = Column(DateTime, index=True, comment='Grant expiration time')
    status = Column(String(16), nullable=False, default='active', index=True, comment='Grant status')
    granted_reason = Column(Text, comment='Grant reason')
    last_used_at = Column(DateTime, comment='Last consume timestamp')

    granted_by_user_id = Column(Integer, ForeignKey('users.id'), index=True, comment='Granted by user ID')
    revoked_by_user_id = Column(Integer, ForeignKey('users.id'), index=True, comment='Revoked by user ID')
    revoked_by_agent_id = Column(Integer, ForeignKey('agents.id'), index=True, comment='Revoked by agent ID')

    secret = relationship('AgentSecret', foreign_keys=[secret_id])
    from_agent = relationship('Agent', foreign_keys=[from_agent_id])
    to_agent = relationship('Agent', foreign_keys=[to_agent_id])
    granter = relationship('User', foreign_keys=[granted_by_user_id])
    user_revoker = relationship('User', foreign_keys=[revoked_by_user_id])
    agent_revoker = relationship('Agent', foreign_keys=[revoked_by_agent_id])

