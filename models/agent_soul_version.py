"""
Agent 记忆版本模型（P3.4 记忆治理）

统一版本快照表：memory_kind 区分记忆种类（soul=SOUL.md / skill_profile=技能画像），
同一 Agent 各类记忆独立版本号。结构化记忆（skill_profile）存 snapshot_json，
文本记忆（soul）存 soul_markdown。
"""

import json

from sqlalchemy import Column, Integer, Text, ForeignKey, UniqueConstraint, String
from sqlalchemy.orm import relationship
from .base import BaseModel

# 记忆种类（P3.4）
MEMORY_KIND_SOUL = 'soul'
MEMORY_KIND_SKILL_PROFILE = 'skill_profile'
MEMORY_KINDS = (MEMORY_KIND_SOUL, MEMORY_KIND_SKILL_PROFILE)


class AgentSoulVersion(BaseModel):
    """Agent 的记忆版本快照（SOUL.md / 技能画像等）"""

    __tablename__ = 'agent_soul_versions'
    __table_args__ = (
        UniqueConstraint('agent_id', 'memory_kind', 'version', name='uq_agent_memory_version'),
    )

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    version = Column(Integer, nullable=False, comment='版本号（按 memory_kind 独立递增）')
    memory_kind = Column(String(20), nullable=False, default=MEMORY_KIND_SOUL,
                         server_default=MEMORY_KIND_SOUL, comment='记忆种类: soul/skill_profile')
    soul_markdown = Column(Text, nullable=False, comment='SOUL.md 内容（memory_kind=soul）')
    snapshot_json = Column(Text, nullable=True, comment='结构化快照（memory_kind=skill_profile 时为画像 JSON）')
    change_summary = Column(String(255), comment='变更说明')
    edited_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='编辑者用户ID')

    agent = relationship('Agent', back_populates='soul_versions', foreign_keys=[agent_id])
    editor = relationship('User', foreign_keys=[edited_by_user_id])

    def snapshot_as_dict(self):
        """解析 snapshot_json；非结构化记忆返回 None。"""
        if not self.snapshot_json:
            return None
        try:
            return json.loads(self.snapshot_json)
        except (ValueError, TypeError):
            return None

    def to_dict(self):
        data = super().to_dict()
        data['editor'] = self.editor.to_public_dict() if self.editor else None
        data['snapshot'] = self.snapshot_as_dict()
        return data
