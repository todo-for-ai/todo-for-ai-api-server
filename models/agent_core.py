"""
Agent core models: Agent, TaskAssignment, AgentRun, RunLog and their enums,
plus the mark_stale_agents_offline / has_live_agent_assignment helpers.
"""

import enum
from datetime import datetime, timedelta

from sqlalchemy import (
    and_,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    or_,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


class AgentStatus(enum.Enum):
    """Agent availability state."""

    ACTIVE = "active"
    PAUSED = "paused"
    OFFLINE = "offline"
    DISABLED = "disabled"


class AgentKind(enum.Enum):
    """Agent role/type in the collaboration platform."""

    ASSISTANT = "assistant"
    AUTONOMOUS = "autonomous"
    COORDINATOR = "coordinator"
    EXTERNAL = "external"


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


class AgentRunStatus(enum.Enum):
    """Execution run state."""

    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
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


class Agent(BaseModel):
    """First-class Agent identity."""

    __tablename__ = "agents"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Agent owner user ID")
    name = Column(String(255), nullable=False, comment="Agent display name")
    description = Column(Text, comment="Agent description")
    kind = Column(Enum(AgentKind), default=AgentKind.ASSISTANT, nullable=False, comment="Agent kind")
    status = Column(Enum(AgentStatus), default=AgentStatus.ACTIVE, nullable=False, index=True, comment="Agent status")
    provider = Column(String(100), comment="Provider, e.g. openai/anthropic/local")
    model = Column(String(255), comment="Default model or runtime name")
    capabilities = Column(JSON, comment="Capability descriptors")
    config = Column(JSON, comment="Non-secret runtime configuration")
    collaboration_role = Column(String(50), nullable=True, comment="Collaboration role: leader, follower, standalone (default: standalone)")
    last_seen_at = Column(DateTime, comment="Last heartbeat timestamp")
    is_system = Column(Boolean, default=False, nullable=False, comment="Whether this is a system-managed Agent")

    owner = relationship("User")
    assignments = relationship("TaskAssignment", back_populates="agent", lazy="dynamic")
    runs = relationship("AgentRun", back_populates="agent", lazy="dynamic")

    def to_dict(self, include_stats=False):
        result = super().to_dict()
        result["kind"] = self.kind.value if self.kind else None
        result["status"] = self.status.value if self.status else None
        result["capabilities"] = self.capabilities or []
        result["config"] = self.config or {}
        result["collaboration_role"] = self.collaboration_role or "standalone"

        if include_stats:
            now = datetime.utcnow()
            result["stats"] = {
                "active_assignments": self.assignments.filter(
                    or_(
                        and_(
                            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                            or_(TaskAssignment.lease_expires_at.is_(None), TaskAssignment.lease_expires_at >= now),
                        ),
                        TaskAssignment.state.in_(HUMAN_BLOCKING_ASSIGNMENT_STATES),
                    ),
                ).count(),
                "total_runs": self.runs.count(),
            }

        return result

    def heartbeat(self):
        self.last_seen_at = datetime.utcnow()
        if self.status == AgentStatus.OFFLINE:
            self.status = AgentStatus.ACTIVE

    def adapt_capabilities_from_experiences(self):
        """Auto-adjust the agent's capabilities based on accumulated experiences.

        Analyzes the agent's experience records and suggests capability
        additions/removals:
        - Successful patterns in domains not in current capabilities → suggest additions
        - Failed patterns with capabilities that consistently fail → suggest removals
        - High-reuse experiences → strengthen existing capabilities

        Returns a dict of suggested changes for review/approval.
        """
        current_caps = set(self.capabilities or [])

        # Analyze successful experiences for potential new capabilities
        success_experiences = AgentExperience.query.filter_by(
            agent_id=self.id,
            experience_type="success_pattern",
            is_valid=True,
        ).filter(
            AgentExperience.confidence >= 0.7,
        ).all()

        # Analyze failure experiences for potential capability removals
        failure_experiences = AgentExperience.query.filter_by(
            agent_id=self.id,
            experience_type="failure_pattern",
            is_valid=True,
        ).filter(
            AgentExperience.confidence >= 0.5,
        ).all()

        suggested_additions = {}
        suggested_removals = {}

        # Find domains where agent succeeded but doesn't list the capability
        for exp in success_experiences:
            domain = exp.domain
            caps_used = set(exp.capabilities_used or [])
            if domain and domain not in current_caps:
                # Count successful experiences in this domain
                domain_success_count = AgentExperience.query.filter_by(
                    agent_id=self.id,
                    experience_type="success_pattern",
                    domain=domain,
                    is_valid=True,
                ).filter(
                    AgentExperience.confidence >= 0.6,
                ).count()
                if domain_success_count >= 2:  # Need at least 2 successes to suggest
                    suggested_additions[domain] = {
                        "reason": f"{domain_success_count} successful experiences in '{domain}' domain",
                        "confidence": min(0.9, domain_success_count * 0.15 + 0.5),
                        "source_experience_ids": [exp.id],
                    }

            # Also suggest capabilities_used that aren't in current caps
            for cap in caps_used:
                if cap not in current_caps and cap not in suggested_additions:
                    cap_success_count = AgentExperience.query.filter(
                        AgentExperience.agent_id == self.id,
                        AgentExperience.experience_type == "success_pattern",
                        AgentExperience.is_valid == True,
                        AgentExperience.confidence >= 0.6,
                    ).filter(
                        AgentExperience.capabilities_used.contains([cap]),
                    ).count()
                    if cap_success_count >= 2:
                        suggested_additions[cap] = {
                            "reason": f"{cap_success_count} successful experiences using '{cap}'",
                            "confidence": min(0.85, cap_success_count * 0.1 + 0.5),
                            "source_experience_ids": [exp.id],
                        }

        # Find capabilities that consistently lead to failures
        for exp in failure_experiences:
            caps_used = set(exp.capabilities_used or [])
            for cap in caps_used:
                if cap in current_caps:
                    cap_failure_count = AgentExperience.query.filter(
                        AgentExperience.agent_id == self.id,
                        AgentExperience.experience_type == "failure_pattern",
                        AgentExperience.is_valid == True,
                    ).filter(
                        AgentExperience.capabilities_used.contains([cap]),
                    ).count()
                    cap_success_count = AgentExperience.query.filter(
                        AgentExperience.agent_id == self.id,
                        AgentExperience.experience_type == "success_pattern",
                        AgentExperience.is_valid == True,
                    ).filter(
                        AgentExperience.capabilities_used.contains([cap]),
                    ).count()

                    # Only suggest removal if failures significantly outweigh successes
                    if cap_failure_count > cap_success_count * 2 and cap_failure_count >= 3:
                        suggested_removals[cap] = {
                            "reason": f"{cap_failure_count} failures vs {cap_success_count} successes with '{cap}'",
                            "confidence": min(0.9, cap_failure_count * 0.15),
                        }

        return {
            "current_capabilities": sorted(current_caps),
            "suggested_additions": suggested_additions,
            "suggested_removals": suggested_removals,
            "net_change": len(suggested_additions) - len(suggested_removals),
        }

    def apply_capability_adaptation(self, additions: list = None, removals: list = None):
        """Apply suggested capability changes to the agent.

        Args:
            additions: List of capability names to add
            removals: List of capability names to remove
        """
        current_caps = list(self.capabilities or [])
        current_set = set(current_caps)

        if additions:
            for cap in additions:
                if cap not in current_set:
                    current_caps.append(cap)

        if removals:
            current_caps = [c for c in current_caps if c not in removals]

        self.capabilities = sorted(current_caps)
        db.session.flush()
        return self.capabilities


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
                {"id": r.id, "status": r.status.value if r.status else None}
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
        Agent.status == AgentStatus.ACTIVE,
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


class AgentRun(BaseModel):
    """A concrete execution attempt by an Agent."""

    __tablename__ = "agent_runs"

    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=False, index=True, comment="Task ID")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent ID")
    assignment_id = Column(Integer, ForeignKey("task_assignments.id"), nullable=True, index=True, comment="Assignment ID")
    status = Column(Enum(AgentRunStatus), default=AgentRunStatus.RUNNING, nullable=False, index=True, comment="Run status")
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment="Run start time")
    ended_at = Column(DateTime, comment="Run end time")
    input_snapshot = Column(JSON, comment="Task/project input snapshot")
    output_summary = Column(Text, comment="Execution output summary")
    error = Column(Text, comment="Execution error")
    run_metadata = Column(JSON, comment="Provider/runtime metadata")

    task = relationship("Task")
    agent = relationship("Agent", back_populates="runs")
    assignment = relationship("TaskAssignment", back_populates="runs")

    def to_dict(self, include_task=False, include_agent=False):
        result = super().to_dict()
        result["status"] = self.status.value if self.status else None
        result["input_snapshot"] = self.input_snapshot or {}
        result["run_metadata"] = self.run_metadata or {}

        if include_task and self.task:
            result["task"] = self.task.to_dict(include_project=True)

        if include_agent and self.agent:
            result["agent"] = {
                "id": self.agent.id,
                "name": self.agent.name,
                "kind": self.agent.kind.value if self.agent.kind else None,
            }

        return result


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

