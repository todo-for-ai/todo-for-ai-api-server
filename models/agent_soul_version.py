"""
Agent SOUL 版本模型
"""

from sqlalchemy import Column, Integer, Text, ForeignKey, UniqueConstraint, String
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentSoulVersion(BaseModel):
    """Agent 的 SOUL.md 版本快照"""

    __tablename__ = 'agent_soul_versions'
    __table_args__ = (
        UniqueConstraint('agent_id', 'version', name='uq_agent_soul_version_agent_ver'),
    )

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    version = Column(Integer, nullable=False, comment='版本号')
    soul_markdown = Column(Text, nullable=False, comment='SOUL.md 内容')
    change_summary = Column(String(255), comment='变更说明')
    edited_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='编辑者用户ID')

    agent = relationship('Agent', back_populates='soul_versions', foreign_keys=[agent_id])
    editor = relationship('User', foreign_keys=[edited_by_user_id])

    def to_dict(self):
        data = super().to_dict()
        data['editor'] = self.editor.to_public_dict() if self.editor else None
        return data
