"""
Agent 接入链接模型
"""

from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text
from .base import BaseModel


class AgentConnectLink(BaseModel):
    __tablename__ = 'agent_connect_links'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    key_id = Column(Integer, ForeignKey('agent_keys.id'), nullable=False, index=True, comment='关联Key ID')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='创建者用户ID')

    url = Column(Text, nullable=False, comment='完整接入链接')
    signature = Column(String(128), nullable=False, index=True, comment='签名')
    expires_at = Column(DateTime, nullable=False, index=True, comment='链接过期时间')
