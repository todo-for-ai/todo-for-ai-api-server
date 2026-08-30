"""
Agent 运行记录模型（统一版）

`agent_runs` 表的唯一 ORM 映射：
- 触发引擎侧（agent_trigger_engine）：trigger_id/state/scheduled_at/lease 等字段
- 协作编排侧（api/agents）：assignment_id/status(AgentRunStatus)/output 等字段

2026-08-31 与 agent_core.AgentRun 合并为超集；触发侧 NOT NULL 字段放宽为可空，
见 migrations/unify_agent_collaboration_schema.py。
"""

import enum
from sqlalchemy import Column, String, Integer, BigInteger, ForeignKey, JSON, DateTime, Text, Enum
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentRunState(enum.Enum):
    """触发引擎侧运行状态（state 列，字符串存储小写值）"""
    QUEUED = 'queued'
    LEASED = 'leased'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


class AgentRunStatus(enum.Enum):
    """协作编排侧运行状态（status 列，按枚举名存储：RUNNING/FAILED/...）"""
    RUNNING = 'running'
    WAITING_HUMAN = 'waiting_human'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


class AgentRun(BaseModel):
    __tablename__ = 'agent_runs'

    # ── 触发引擎侧 ──
    run_id = Column(String(64), nullable=True, unique=True, index=True, comment='运行ID（协作侧运行可为空）')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True, comment='工作区ID（协作侧运行可为空）')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    trigger_id = Column(Integer, ForeignKey('agent_triggers.id'), nullable=True, index=True, comment='触发器ID（协作侧运行为空）')

    trigger_reason = Column(String(64), nullable=True, comment='触发原因（协作侧运行为空）')
    input_payload = Column(JSON, comment='触发上下文')

    state = Column(String(16), nullable=False, default=AgentRunState.QUEUED.value, comment='运行状态（触发引擎侧）')

    scheduled_at = Column(DateTime, comment='调度时间（协作侧运行为空）')
    started_at = Column(DateTime, comment='开始时间')
    ended_at = Column(DateTime, comment='结束时间')

    lease_id = Column(String(64), index=True, comment='租约ID')
    lease_expires_at = Column(DateTime, comment='租约过期时间')
    attempt_count = Column(Integer, comment='尝试次数')

    failure_code = Column(String(64), comment='失败码')
    failure_reason = Column(Text, comment='失败原因')

    idempotency_key = Column(String(128), nullable=True, unique=True, index=True, comment='幂等键（协作侧运行可为空）')

    # ── 协作编排侧（原 agent_core.AgentRun）──
    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=True, index=True, comment='Task ID（触发侧运行为空）')
    assignment_id = Column(Integer, ForeignKey('task_assignments.id'), nullable=True, index=True, comment='Assignment ID')
    status = Column(Enum(AgentRunStatus), comment='运行状态（协作侧，AgentRunStatus）')
    input_snapshot = Column(JSON, comment='任务/项目输入快照')
    output_summary = Column(Text, comment='执行输出摘要')
    error = Column(Text, comment='执行错误')
    run_metadata = Column(JSON, comment='Provider/运行时元数据')

    # ── 关系 ──
    agent = relationship('Agent', foreign_keys=[agent_id], back_populates='runs')
    trigger = relationship('AgentTrigger', foreign_keys=[trigger_id])
    assignment = relationship('TaskAssignment', foreign_keys=[assignment_id], back_populates='runs')

    def to_dict(self):
        data = super().to_dict()
        data['state'] = str(self.state or '').lower() or None
        data['status'] = self.status.name.lower() if self.status else None
        return data
