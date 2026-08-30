"""
产品目标层模型（P2.1）

Goal（产品目标）→ Epic（特性/里程碑）→ 带 DoD 的任务图。
Agent 可提议 Epic（status=proposed），人类单条/批量裁决（accepted/dropped）；
Epic 展开为带 DoD 的任务图（tasks.epic_id 关联）。
"""

import enum

from sqlalchemy import Column, String, Integer, Text, DateTime, Enum, JSON, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class GoalStatus(enum.Enum):
    DRAFT = 'draft'
    ACTIVE = 'active'
    PAUSED = 'paused'
    ACHIEVED = 'achieved'
    ARCHIVED = 'archived'


class EpicStatus(enum.Enum):
    PROPOSED = 'proposed'      # Agent 提议，等待人类裁决
    ACCEPTED = 'accepted'      # 已裁决采纳
    IN_PROGRESS = 'in_progress'
    DONE = 'done'
    DROPPED = 'dropped'        # 已裁决放弃


class Goal(BaseModel):
    """产品目标"""

    __tablename__ = 'goals'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    title = Column(String(500), nullable=False, comment='目标标题')
    description = Column(Text, comment='目标描述/背景')
    metrics = Column(JSON, comment='成功指标 (JSON数组, 如 ["DAU 10k", "错误率 < 1%"])')
    status = Column(Enum(GoalStatus), nullable=False, default=GoalStatus.DRAFT, comment='目标状态')
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True, comment='目标负责人')
    due_date = Column(DateTime, comment='目标截止时间')

    epics = relationship('Epic', back_populates='goal', cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self, include_epics=False):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        if include_epics:
            data['epics'] = [e.to_dict() for e in self.epics.order_by(Epic.order_index)]
        return data


class Epic(BaseModel):
    """Epic（目标下的特性/里程碑，可由 Agent 提议）"""

    __tablename__ = 'epics'

    goal_id = Column(Integer, ForeignKey('goals.id'), nullable=False, index=True, comment='所属目标ID')
    title = Column(String(500), nullable=False, comment='Epic 标题')
    description = Column(Text, comment='Epic 描述/验收说明')
    status = Column(Enum(EpicStatus), nullable=False, default=EpicStatus.PROPOSED, comment='Epic 状态（proposed=Agent 提议待裁决）')
    order_index = Column(Integer, nullable=False, default=0, comment='排序')
    agent_proposed = Column(Boolean, nullable=False, default=False, comment='是否 Agent 提议')
    proposed_by_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, index=True, comment='提议的 Agent ID')
    decided_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, comment='裁决人')
    decided_at = Column(DateTime, comment='裁决时间')

    goal = relationship('Goal', back_populates='epics')

    def to_dict(self):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        return data
