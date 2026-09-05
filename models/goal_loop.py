"""
GoalLoop 目标循环模型

一个循环 = 某 agent 在某项目上朝一个目标逐轮做任务，直到规划器宣告完成
或触发护栏（轮数上限/连续受阻/人工停止）。任务以 tags 中的 `goal-loop:{id}`
标记关联到循环，由平台驱动器在任务终态后自动推进下一轮。
"""

import enum

from sqlalchemy import Column, String, Integer, Text, DateTime, Enum, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class GoalLoopStatus(enum.Enum):
    RUNNING = 'running'
    PAUSED = 'paused'
    DONE = 'done'                # 规划器宣告目标达成
    LIMIT_REACHED = 'limit_reached'  # 轮数上限耗尽（目标未宣告完成）
    STALLED = 'stalled'          # 连续受阻（规划器失败/blocked）
    STOPPED = 'stopped'          # 人工停止


TERMINAL_LOOP_STATUSES = {
    GoalLoopStatus.DONE,
    GoalLoopStatus.LIMIT_REACHED,
    GoalLoopStatus.STALLED,
    GoalLoopStatus.STOPPED,
}

GOAL_LOOP_TAG_PREFIX = 'goal-loop:'


class GoalLoop(BaseModel):
    """目标循环"""

    __tablename__ = 'goal_loops'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, index=True, comment='所属项目ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='绑定执行Agent ID')

    title = Column(String(500), nullable=False, comment='循环标题')
    goal_text = Column(Text, nullable=False, comment='目标描述（规划器的驱动源）')
    done_definition = Column(Text, comment='完成标准（规划器判断目标达成的依据）')

    status = Column(Enum(GoalLoopStatus), nullable=False, default=GoalLoopStatus.RUNNING, index=True, comment='循环状态')
    advancing = Column(Integer, nullable=False, default=0, comment='推进中标记（CAS 防并发双发任务）')
    rounds_limit = Column(Integer, nullable=False, default=10, comment='最大轮数护栏')
    stall_limit = Column(Integer, nullable=False, default=2, comment='连续受阻容忍次数')
    stall_count = Column(Integer, nullable=False, default=0, comment='当前连续受阻计数')

    last_error = Column(Text, comment='最近一次受阻/失败原因')
    completion_summary = Column(Text, comment='目标达成时的总结（规划器给出）')
    last_task_id = Column(Integer, comment='最近一轮生成的任务ID')
    started_at = Column(DateTime, comment='首次推进时间')
    finished_at = Column(DateTime, comment='进入终态时间')

    created_by = Column(Integer, ForeignKey('users.id'), nullable=True, comment='创建人用户ID')

    agent = relationship('Agent')

    @property
    def tag(self) -> str:
        return f'{GOAL_LOOP_TAG_PREFIX}{self.id}'

    def to_dict(self):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        data['tag'] = self.tag
        return data
