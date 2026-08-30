"""
Agent 协作核心模型：TaskAssignment、RunLog 与相关枚举/辅助函数。

历史上本文件还定义了 Agent / AgentStatus / AgentRun。2026-08-31 合并冲突
收敛后，`agents` / `agent_runs` 表的唯一映射分别位于 models/agent.py 与
models/agent_run.py（超集统一版），此处仅做兼容再导出。
"""

import enum
from datetime import datetime, timedelta

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    or_,
    String,
    Text,
    BigInteger,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db
from .agent import Agent, AgentStatus, AgentKind  # noqa: F401  (兼容再导出)
from .agent_run import AgentRun, AgentRunStatus  # noqa: F401  (兼容再导出)


class TaskAssignmentState(enum.Enum):
    """Task execution state from the assignment/runtime perspective."""

    ASSIGNED = "assigned"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Assignment states that represent a live, leased hold on a task.
LEASED_EXECUTION_STATES = [
    TaskAssignmentState.ASSIGNED,
    TaskAssignmentState.CLAIMED,
    TaskAssignmentState.RUNNING,
]

# Assignment states where a human is the active blocker (waiting or reviewing).
HUMAN_BLOCKING_ASSIGNMENT_STATES = [
    TaskAssignmentState.WAITING_HUMAN,
    TaskAssignmentState.REVIEW,
]

ACTIVE_ASSIGNMENT_STATES = LEASED_EXECUTION_STATES + HUMAN_BLOCKING_ASSIGNMENT_STATES

# An Agent is considered offline if no heartbeat has been received within
# this many seconds.
AGENT_OFFLINE_AFTER_SECONDS = 30 * 60


class TaskAssignment(BaseModel):
    """A claimable task assignment for an Agent."""

    __tablename__ = "task_assignments"

    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=False, index=True, comment="Task ID")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Assigned Agent ID")
    assigned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="Assigning user ID")
    state = Column(
        Enum(TaskAssignmentState),
        default=TaskAssignmentState.ASSIGNED,
        nullable=False,
        index=True,
        comment="Assignment state",
    )
    lease_expires_at = Column(DateTime, index=True, comment="Lease expiry time")
    claimed_at = Column(DateTime, comment="Claim timestamp")
    completed_at = Column(DateTime, comment="Completion timestamp")
    last_heartbeat_at = Column(DateTime, comment="Last execution heartbeat")
    progress_rate = Column(Integer, default=0, nullable=False, comment="Progress percent 0-100")
    notes = Column(Text, comment="Assignment notes")

    task = relationship("Task")
    agent = relationship("Agent", back_populates="assignments")
    assigned_by = relationship("User")
    runs = relationship("AgentRun", back_populates="assignment", lazy="dynamic")

    def to_dict(self, include_task=False, include_agent=False, include_runs=False):
        result = super().to_dict()
        result["state"] = self.state.value if self.state else None

        if include_task and self.task:
            result["task"] = self.task.to_dict(include_project=True)

        if include_agent and self.agent:
            result["agent"] = {
                "id": self.agent.id,
                "name": self.agent.name,
                "kind": self.agent.kind.value if self.agent.kind else None,
                "status": self.agent.status.value if self.agent.status else None,
            }

        if include_runs:
            result["runs"] = [
                {"id": r.id, "status": r.status.name.lower() if r.status else None}
                for r in self.runs.order_by(AgentRun.id.desc()).limit(10).all()
            ]

        return result

    @property
    def is_terminal(self):
        return self.state in {
            TaskAssignmentState.DONE,
            TaskAssignmentState.FAILED,
            TaskAssignmentState.CANCELLED,
            TaskAssignmentState.EXPIRED,
        }

    @property
    def is_lease_expired(self):
        return bool(
            self.state in LEASED_EXECUTION_STATES
            and self.lease_expires_at
            and self.lease_expires_at < datetime.utcnow()
        )


def has_live_agent_assignment(agent_id, now=None):
    now = now or datetime.utcnow()
    return TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent_id,
        TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
        or_(TaskAssignment.lease_expires_at.is_(None), TaskAssignment.lease_expires_at >= now),
    ).first() is not None


def mark_stale_agents_offline(owner_id=None, offline_after_seconds=AGENT_OFFLINE_AFTER_SECONDS):
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=offline_after_seconds)
    query = Agent.query.filter(
        Agent.status.in_((AgentStatus.ACTIVE, AgentStatus.PAUSED)),
        Agent.last_seen_at.isnot(None),
        Agent.last_seen_at < cutoff,
    )

    if owner_id:
        query = query.filter(Agent.owner_id == owner_id)

    stale_agents = []
    for agent in query.all():
        # Check for live assignments — if the agent has them, expire them too
        live_assignments = TaskAssignment.query.filter(
            TaskAssignment.agent_id == agent.id,
            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
        ).all()

        for assignment in live_assignments:
            # Mark assignment as expired
            assignment.state = TaskAssignmentState.EXPIRED
            assignment.completed_at = now
            # Mark running runs as expired
            for run in AgentRun.query.filter_by(
                assignment_id=assignment.id,
                status=AgentRunStatus.RUNNING,
            ).all():
                run.status = AgentRunStatus.EXPIRED
                run.ended_at = now

        agent.status = AgentStatus.OFFLINE
        db.session.add(agent)
        stale_agents.append(agent)

    return stale_agents


class RunLog(BaseModel):
    """Append-only log entry for an Agent run — supports structured execution replay."""

    __tablename__ = "run_logs"

    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False, index=True, comment="AgentRun ID")
    level = Column(String(20), nullable=False, default="info", index=True, comment="Log level: debug/info/warn/error")
    message = Column(Text, nullable=False, comment="Log message")
    meta = Column(JSON, comment="Optional structured metadata (e.g. tool_call, duration)")

    run = relationship("AgentRun")

    def to_dict(self):
        result = super().to_dict()
        result["meta"] = self.meta or {}
        return result
