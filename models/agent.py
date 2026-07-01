"""
Agent collaboration models.
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


LEASED_EXECUTION_STATES = [
    TaskAssignmentState.ASSIGNED,
    TaskAssignmentState.CLAIMED,
    TaskAssignmentState.RUNNING,
]

HUMAN_BLOCKING_ASSIGNMENT_STATES = [
    TaskAssignmentState.WAITING_HUMAN,
    TaskAssignmentState.REVIEW,
]

ACTIVE_ASSIGNMENT_STATES = LEASED_EXECUTION_STATES + HUMAN_BLOCKING_ASSIGNMENT_STATES

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


class TaskTemplate(BaseModel):
    """Reusable task template — users can create new tasks from a template."""

    __tablename__ = "task_templates"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="User who owns this template")
    name = Column(String(255), nullable=False, comment="Template name")
    description = Column(Text, nullable=False, default="", comment="Template description")
    title_template = Column(String(500), nullable=False, default="", comment="Default task title (may contain placeholders)")
    content_template = Column(Text, nullable=False, default="", comment="Default task content/description")
    priority = Column(String(20), nullable=False, default="medium", comment="Default priority")
    tags = Column(JSON, comment="Default tags")
    is_ai_task = Column(Boolean, nullable=False, default=False, comment="Default is_ai_task flag")
    capabilities = Column(JSON, comment="Required agent capabilities for this template")

    owner = relationship("User")

    def to_dict(self):
        result = super().to_dict()
        result["tags"] = self.tags or []
        result["capabilities"] = self.capabilities or []
        return result


class TaskEvent(BaseModel):
    """Append-only collaboration event for a task."""

    __tablename__ = "task_events"

    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=False, index=True, comment="Task ID")
    actor_type = Column(String(20), nullable=False, index=True, comment="human/agent/system")
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="Actor user ID")
    actor_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Actor Agent ID")
    event_type = Column(String(100), nullable=False, index=True, comment="Event type")
    payload = Column(JSON, comment="Event payload")

    task = relationship("Task")
    actor_user = relationship("User")
    actor_agent = relationship("Agent")

    def to_dict(self):
        result = super().to_dict()
        result["payload"] = self.payload or {}
        if self.actor_agent:
            result["actor_agent"] = {
                "id": self.actor_agent.id,
                "name": self.actor_agent.name,
                "kind": self.actor_agent.kind.value if self.actor_agent.kind else None,
                "status": self.actor_agent.status.value if self.actor_agent.status else None,
            }
        if self.actor_user:
            result["actor_user"] = {
                "id": self.actor_user.id,
                "name": self.actor_user.name or self.actor_user.username or self.actor_user.email,
                "email": self.actor_user.email,
            }
        return result

    @classmethod
    def record(cls, task_id, event_type, actor_type="system", actor_user_id=None, actor_agent_id=None, payload=None):
        event = cls(
            task_id=task_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            payload=payload or {},
        )
        return event


class Notification(BaseModel):
    """Persistent notification for a user — survives across sessions."""

    __tablename__ = "notifications"

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Owning user")
    event_type = Column(String(100), nullable=False, index=True, comment="Notification category")
    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Related task")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Related agent")
    payload = Column(JSON, comment="Arbitrary payload")
    is_read = Column(Boolean, nullable=False, default=False, index=True, comment="Read flag")
    read_at = Column(DateTime, nullable=True, comment="When the notification was read")

    user = relationship("User")
    task = relationship("Task")
    agent = relationship("Agent")

    def to_dict(self):
        result = super().to_dict()
        result["payload"] = self.payload or {}
        if self.agent:
            result["agent_name"] = self.agent.name
        if self.task:
            result["task_title"] = self.task.title
        return result

    @classmethod
    def create_notification(cls, user_id, event_type, task_id=None, agent_id=None, payload=None):
        """Create and return a new Notification (not yet committed)."""
        n = cls(
            user_id=user_id,
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            payload=payload or {},
        )
        db.session.add(n)
        return n


class SharedContext(BaseModel):
    """Key-value context entries shared across Agents working on the same task.

    Agents use this to persist intermediate results, references, scratch notes,
    or any structured data that other Agents (or a later run of the same Agent)
    need to read — without flooding the event timeline.
    """

    __tablename__ = "shared_context"

    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=False, index=True, comment="Task ID")
    key = Column(String(255), nullable=False, index=True, comment="Context key (e.g. 'research_summary', 'code_plan')")
    value = Column(Text, nullable=False, default="", comment="Context value (Markdown or JSON string)")
    author_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Agent that wrote this entry")
    author_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="User that wrote this entry")

    task = relationship("Task")
    author_agent = relationship("Agent")
    author_user = relationship("User")

    __table_args__ = (
        # Unique key per task — upsert semantics
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        if self.author_agent:
            result["author_agent_name"] = self.author_agent.name
        if self.author_user:
            result["author_user_name"] = self.author_user.name or self.author_user.email
        return result


# ---------------------------------------------------------------------------
# Workflow definition & execution
# ---------------------------------------------------------------------------


class WorkflowStatus(enum.Enum):
    """Workflow run status."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(enum.Enum):
    """Workflow step run status."""

    PENDING = "pending"
    WAITING = "waiting"       # waiting for dependencies
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class Workflow(BaseModel):
    """Reusable workflow definition — a DAG of steps that coordinate multiple Agents.

    Users define a workflow template (e.g. "code_review → test → deploy") and
    then launch instances tied to a specific root task. Each step can specify
    required capabilities and the system picks a matching Agent at runtime.
    """

    __tablename__ = "workflows"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Owner user ID")
    name = Column(String(255), nullable=False, comment="Workflow display name")
    description = Column(Text, comment="Workflow description")
    version = Column(Integer, nullable=False, default=1, comment="Schema version for the definition")
    definition = Column(JSON, nullable=False, comment="Full workflow definition (steps, edges, params)")
    is_active = Column(Boolean, nullable=False, default=True, comment="Whether this workflow can be launched")
    max_parallel_steps = Column(Integer, default=0, comment="Max steps running concurrently (0 = unlimited)")

    owner = relationship("User")
    steps = relationship("WorkflowStep", back_populates="workflow", cascade="all, delete-orphan")

    def to_dict(self, include_steps=False):
        result = super().to_dict()
        result["definition"] = self.definition or {}
        result["max_parallel_steps"] = self.max_parallel_steps or 0
        if include_steps:
            result["steps"] = [s.to_dict() for s in self.steps]
        return result


class WorkflowStep(BaseModel):
    """A single step within a workflow definition.

    Each step specifies the *role* an Agent should play (via capabilities or
    a specific agent_id), what task template to instantiate, and which
    preceding steps must complete before this step can start.
    """

    __tablename__ = "workflow_steps"

    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True, comment="Parent workflow")
    step_key = Column(String(100), nullable=False, comment="Unique key within the workflow (e.g. 'review')")
    name = Column(String(255), nullable=False, comment="Step display name")
    description = Column(Text, comment="Step description")
    order = Column(Integer, nullable=False, default=0, comment="Display / execution order hint")
    # Agent matching — at runtime the system picks an agent that satisfies these
    required_capabilities = Column(JSON, comment="Capabilities the Agent must have")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, comment="Specific Agent to use (overrides capability matching)")
    task_template_id = Column(Integer, ForeignKey("task_templates.id"), nullable=True, comment="Task template to instantiate for this step")
    # Dependency edges — list of step_key values that must succeed before this step starts
    depends_on = Column(JSON, comment="List of step_key values this step depends on")
    # Conditional execution — only run this step if the condition evaluates to true
    condition = Column(JSON, comment="Condition for execution: {step_key, operator, value} e.g. {step_key: 'review', operator: 'succeeded', value: true}")
    # Sub-workflow: this step launches another workflow instead of a single task
    sub_workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=True, comment="Sub-workflow to launch (instead of creating a single task)")
    # Execution config
    timeout_seconds = Column(Integer, comment="Step-level timeout (0 = no timeout)")
    retry_count = Column(Integer, default=0, comment="Number of automatic retries on failure")
    on_failure = Column(String(20), default="abort", comment="What to do on failure: abort|skip|continue")

    workflow = relationship("Workflow", back_populates="steps")
    agent = relationship("Agent")
    task_template = relationship("TaskTemplate")
    sub_workflow = relationship("Workflow", foreign_keys=[sub_workflow_id])

    __table_args__ = (
        # step_key is unique within a workflow
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        result["required_capabilities"] = self.required_capabilities or []
        result["depends_on"] = self.depends_on or []
        result["condition"] = self.condition or None
        result["sub_workflow_id"] = self.sub_workflow_id
        return result


class WorkflowRun(BaseModel):
    """A concrete execution of a Workflow — tied to a root task and project."""

    __tablename__ = "workflow_runs"

    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True, comment="Workflow definition")
    root_task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Root task this run is attached to")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True, comment="Project for spawned tasks")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="User who launched this run")
    status = Column(Enum(WorkflowStatus), default=WorkflowStatus.PENDING, nullable=False, index=True, comment="Run status")
    context = Column(JSON, comment="Run-level context / parameters passed to steps")
    error = Column(Text, comment="Error message if the workflow failed")
    started_at = Column(DateTime, comment="When the first step started")
    finished_at = Column(DateTime, comment="When the last step finished")

    workflow = relationship("Workflow")
    root_task = relationship("Task")
    project = relationship("Project")
    owner = relationship("User")
    step_runs = relationship("WorkflowStepRun", back_populates="run", cascade="all, delete-orphan")

    def to_dict(self, include_step_runs=False):
        result = super().to_dict()
        result["status"] = self.status.value if self.status else None
        result["context"] = self.context or {}
        if include_step_runs:
            result["step_runs"] = [sr.to_dict() for sr in self.step_runs]
        return result


class WorkflowStepRun(BaseModel):
    """Execution record for one step within a WorkflowRun."""

    __tablename__ = "workflow_step_runs"

    run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False, index=True, comment="WorkflowRun ID")
    step_key = Column(String(100), nullable=False, index=True, comment="Step key from definition")
    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Task created for this step")
    assignment_id = Column(Integer, ForeignKey("task_assignments.id"), nullable=True, comment="TaskAssignment for this step")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Agent that executed this step")
    status = Column(Enum(StepStatus), default=StepStatus.PENDING, nullable=False, index=True, comment="Step run status")
    started_at = Column(DateTime, comment="When this step started")
    finished_at = Column(DateTime, comment="When this step finished")
    error = Column(Text, comment="Error message if this step failed")
    attempt = Column(Integer, default=1, comment="Attempt number (for retries)")
    # Runtime overrides — applied on top of the step definition for this run only.
    # Allows dynamic reconfiguration of a not-yet-started step without altering
    # the workflow definition. Keys: agent_id, required_capabilities, timeout_seconds,
    # retry_count, on_failure, condition, task_template_id, sub_workflow_id.
    runtime_overrides = Column(JSON, comment="Per-run step parameter overrides (applied at start time)")

    run = relationship("WorkflowRun", back_populates="step_runs")
    task = relationship("Task")
    assignment = relationship("TaskAssignment")
    agent = relationship("Agent")

    def get_effective_param(self, key, default=None):
        """Return the effective value for a step parameter, preferring a runtime
        override over the step definition."""
        overrides = self.runtime_overrides or {}
        if key in overrides:
            return overrides[key]
        # Fall back to the workflow step definition
        if self.run and self.run.workflow:
            for s in self.run.workflow.steps:
                if s.step_key == self.step_key:
                    return getattr(s, key, default)
        return default

    def to_dict(self):
        result = super().to_dict()
        result["status"] = self.status.value if self.status else None
        result["runtime_overrides"] = self.runtime_overrides or {}
        # Enrich with depends_on from workflow definition
        if self.run and self.run.workflow:
            definition = self.run.workflow.definition or {}
            step_defs = definition.get("steps", [])
            for sd in step_defs:
                if sd.get("step_key") == self.step_key:
                    result["depends_on"] = sd.get("depends_on", [])
                    result["name"] = sd.get("name", self.step_key)
                    break
        return result


class WorkflowTrigger(BaseModel):
    """Scheduled trigger for automatic workflow execution.

    Supports cron expressions for recurring runs and one-shot schedules.
    When ``is_active`` is True and the next fire time is due, the scheduler
    launches a new WorkflowRun.
    """

    __tablename__ = "workflow_triggers"

    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True, comment="Workflow to trigger")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="User who owns the trigger")
    name = Column(String(200), nullable=False, comment="Human-readable trigger name")
    cron_expr = Column(String(100), nullable=True, comment="Cron expression (e.g. '0 9 * * 1-5' for weekdays 9am)")
    one_shot_at = Column(DateTime, nullable=True, comment="If set, fire once at this time then deactivate")
    is_active = Column(Boolean, default=True, nullable=False, index=True, comment="Whether the trigger is enabled")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Default project_id for runs")
    root_task_id = Column(BigInteger, nullable=True, comment="Default root_task_id for runs")
    context_override = Column(JSON, nullable=True, comment="Optional context JSON merged into each run")
    last_fired_at = Column(DateTime, nullable=True, comment="When the trigger last fired")
    next_fire_at = Column(DateTime, nullable=True, comment="Computed next fire time")
    fire_count = Column(Integer, default=0, nullable=False, comment="Number of times this trigger has fired")

    workflow = relationship("Workflow")
    owner = relationship("User")

    def to_dict(self):
        result = super().to_dict()
        result["is_active"] = self.is_active
        return result


class AuditLog(BaseModel):
    """Immutable audit trail for significant platform operations.

    Captures who did what, to which resource, with what result. Designed for
    compliance, debugging, and analytics — not for real-time UI.
    """

    __tablename__ = "audit_logs"

    actor_type = Column(String(20), nullable=False, index=True, comment="human / agent / system")
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="User who performed the action")
    actor_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Agent that performed the action")
    action = Column(String(100), nullable=False, index=True, comment="Action identifier (e.g. agent.created, workflow.launched)")
    resource_type = Column(String(50), nullable=False, index=True, comment="Target resource type (agent, task, workflow, etc.)")
    resource_id = Column(BigInteger, nullable=False, index=True, comment="Target resource ID")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True, comment="Project context")
    detail = Column(JSON, comment="Arbitrary action details (before/after diff, params, etc.)")
    ip_address = Column(String(45), comment="Client IP (for human actors)")

    actor_user = relationship("User")
    actor_agent = relationship("Agent")
    project = relationship("Project")

    def to_dict(self):
        result = super().to_dict()
        result["detail"] = self.detail or {}
        if self.actor_agent:
            result["actor_agent_name"] = self.actor_agent.name
        if self.actor_user:
            result["actor_user_email"] = self.actor_user.email
        return result

    @classmethod
    def record(cls, action, resource_type, resource_id, actor_type="system",
               actor_user_id=None, actor_agent_id=None, project_id=None,
               detail=None, ip_address=None):
        """Create and add an audit log entry (not yet committed)."""
        entry = cls(
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            project_id=project_id,
            detail=detail or {},
            ip_address=ip_address,
        )
        db.session.add(entry)
        return entry


class ProjectRole(enum.Enum):
    """Project-level role for RBAC."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ProjectMember(BaseModel):
    """Project membership with role-based access control.

    Controls who can view / edit / manage tasks and agents within a project.
    The project owner is always a member with the OWNER role (created automatically).
    """

    __tablename__ = "project_members"

    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True, comment="Project ID")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="User ID")
    role = Column(Enum(ProjectRole), default=ProjectRole.MEMBER, nullable=False, index=True, comment="Role within the project")
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True, comment="User who sent the invitation")
    accepted_at = Column(DateTime, comment="When the invitee accepted the invitation")

    project = relationship("Project")
    user = relationship("User", foreign_keys=[user_id])
    inviter = relationship("User", foreign_keys=[invited_by])

    __table_args__ = (
        # One membership per user per project
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        result["role"] = self.role.value if self.role else None
        if self.user:
            result["user_email"] = self.user.email
            result["user_name"] = self.user.name or self.user.username or self.user.email
        return result

    @classmethod
    def get_role(cls, project_id, user_id):
        """Return the user's role in the project, or None if not a member."""
        m = cls.query.filter_by(project_id=project_id, user_id=user_id).first()
        return m.role if m else None

    @classmethod
    def can(cls, project_id, user_id, action):
        """Check if a user can perform an action in a project.

        Action hierarchy:
          - view: VIEWER+
          - edit: MEMBER+
          - manage: ADMIN+
          - admin: OWNER only
        """
        role = cls.get_role(project_id, user_id)
        if role is None:
            return False
        if action == "view":
            return role in (ProjectRole.OWNER, ProjectRole.ADMIN, ProjectRole.MEMBER, ProjectRole.VIEWER)
        if action == "edit":
            return role in (ProjectRole.OWNER, ProjectRole.ADMIN, ProjectRole.MEMBER)
        if action == "manage":
            return role in (ProjectRole.OWNER, ProjectRole.ADMIN)
        if action == "admin":
            return role == ProjectRole.OWNER
        return False


class AgentChannel(BaseModel):
    """A collaboration channel where multiple Agents can discuss and coordinate.

    Channels can be tied to a specific task (task-scoped) or be standalone
    (project-scoped). Agents join channels and exchange messages in real-time.
    """

    __tablename__ = "agent_channels"

    name = Column(String(255), nullable=False, comment="Channel display name")
    description = Column(Text, comment="Channel description/purpose")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True, comment="Project scope (null = global)")
    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Task scope (null = project/global)")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Channel creator")
    is_active = Column(Boolean, nullable=False, default=True, comment="Whether the channel is active")

    project = relationship("Project")
    task = relationship("Task")
    owner = relationship("User")
    members = relationship("AgentChannelMember", back_populates="channel", cascade="all, delete-orphan")
    messages = relationship("AgentChannelMessage", back_populates="channel", cascade="all, delete-orphan")

    def to_dict(self, include_members=False, include_last_message=False):
        result = super().to_dict()
        if include_members:
            result["members"] = [m.to_dict() for m in self.members]
        if include_last_message:
            last = AgentChannelMessage.query.filter_by(channel_id=self.id).order_by(AgentChannelMessage.id.desc()).first()
            result["last_message"] = last.to_dict() if last else None
        return result


class AgentChannelMember(BaseModel):
    """An Agent's membership in a channel."""

    __tablename__ = "agent_channel_members"

    channel_id = Column(Integer, ForeignKey("agent_channels.id"), nullable=False, index=True, comment="Channel ID")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent ID")
    role = Column(String(20), default="member", comment="Channel role: owner, member")

    channel = relationship("AgentChannel", back_populates="members")
    agent = relationship("Agent")

    __table_args__ = (
        # Unique agent per channel
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        if self.agent:
            result["agent_name"] = self.agent.name
            result["agent_kind"] = self.agent.kind.value if self.agent.kind else None
        return result


class AgentChannelMessage(BaseModel):
    """A message in an Agent collaboration channel."""

    __tablename__ = "agent_channel_messages"

    channel_id = Column(Integer, ForeignKey("agent_channels.id"), nullable=False, index=True, comment="Channel ID")
    sender_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Sender Agent ID (null if human)")
    sender_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="Sender User ID (null if agent)")
    content = Column(Text, nullable=False, comment="Message content")
    message_type = Column(String(50), default="text", comment="Message type: text, system, action")
    metadata = Column(JSON, comment="Extra structured metadata")

    channel = relationship("AgentChannel", back_populates="messages")
    sender_agent = relationship("Agent")
    sender_user = relationship("User")

    def to_dict(self):
        result = super().to_dict()
        if self.sender_agent:
            result["sender_name"] = self.sender_agent.name
            result["sender_type"] = "agent"
        elif self.sender_user:
            result["sender_name"] = self.sender_user.name or self.sender_user.email
            result["sender_type"] = "human"
        else:
            result["sender_name"] = "system"
            result["sender_type"] = "system"
        return result


class CollaborationTemplate(BaseModel):
    """A reusable pattern for assembling a team of Agents.

    Defines the agent roles (kind, capabilities, collaboration_role) and
    optionally a workflow to execute. Users can instantiate a template to
    quickly set up a working multi-Agent team.
    """

    __tablename__ = "collaboration_templates"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Template creator")
    name = Column(String(255), nullable=False, comment="Template display name")
    description = Column(Text, comment="Template description")
    category = Column(String(100), comment="Category tag (e.g. devops, research, review)")
    agent_specs = Column(JSON, nullable=False, comment="List of agent spec dicts: {name, kind, capabilities, collaboration_role, provider, model}")
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=True, comment="Optional workflow to attach")
    is_builtin = Column(Boolean, default=False, comment="Whether this is a built-in template")

    owner = relationship("User")
    workflow = relationship("Workflow")

    def to_dict(self):
        result = super().to_dict()
        result["agent_specs"] = self.agent_specs or []
        return result


class KnowledgeEntry(BaseModel):
    """A piece of knowledge learned or stored by an Agent.

    Agents can persist insights, patterns, solutions, or any structured
    information that can be reused across tasks. Entries are keyed by
    domain/tag and support full-text search.
    """

    __tablename__ = "knowledge_entries"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Owning Agent")
    title = Column(String(500), nullable=False, comment="Short descriptive title")
    content = Column(Text, nullable=False, comment="Knowledge content (can be markdown, JSON, etc.)")
    domain = Column(String(100), comment="Knowledge domain (e.g. 'python', 'frontend', 'devops')")
    tags = Column(JSON, default=list, comment="List of tags for categorization")
    entry_type = Column(String(50), default="insight", comment="Type: insight, pattern, solution, reference, rule")
    source_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, comment="Task that generated this knowledge")
    source_type = Column(String(50), default="manual", comment="Source: manual, auto_extracted, imported, shared")
    confidence = Column(Float, default=1.0, comment="Confidence score 0.0-1.0")
    access_count = Column(Integer, default=0, comment="How many times this entry has been accessed")
    is_valid = Column(Boolean, default=True, comment="Whether this entry is still considered valid")
    shared_with_project = Column(Boolean, default=False, comment="Whether shared with all project members")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Project scope if shared")

    agent = relationship("Agent", backref="knowledge_entries")
    source_task = relationship("Task")
    project = relationship("Project")

    def to_dict(self, include_content=True):
        result = super().to_dict()
        if not include_content:
            result.pop("content", None)
        result["tags"] = self.tags or []
        return result


class WorkflowVersion(BaseModel):
    """A snapshot of a workflow definition at a specific version.

    When a workflow is updated, the previous definition is saved as a
    WorkflowVersion so that running instances continue to use the version
    they were launched with.
    """

    __tablename__ = "workflow_versions"

    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True, comment="Parent workflow")
    version_number = Column(Integer, nullable=False, comment="Version number (matches Workflow.version at snapshot time)")
    definition = Column(JSON, nullable=False, comment="Full workflow definition snapshot")
    steps_snapshot = Column(JSON, nullable=False, comment="Snapshot of steps at this version")
    change_summary = Column(Text, comment="Brief description of what changed in this version")
    created_by = Column(String(255), comment="User who created this version")

    workflow = relationship("Workflow", backref="versions")

    def to_dict(self):
        result = super().to_dict()
        result["definition"] = self.definition or {}
        result["steps_snapshot"] = self.steps_snapshot or []
        return result


class ProtocolType(enum.Enum):
    """Types of structured collaboration protocols between Agents."""
    PROPOSAL = "proposal"       # One agent proposes, others vote
    VOTE = "vote"              # Simple majority vote
    CONSENSUS = "consensus"     # All must agree
    AUCTION = "auction"         # Competitive bidding for task assignment
    HANDOFF = "handoff"         # Structured task handoff with context
    DELIBERATION = "deliberation"  # Multi-round deliberation before final vote
    RANKED_VOTE = "ranked_vote"    # Ranked-choice voting (instant runoff)
    WEIGHTED_VOTE = "weighted_vote"  # Vote weighted by agent reputation


class ProtocolStatus(enum.Enum):
    """Status of a collaboration protocol instance."""
    OPEN = "open"
    VOTING = "voting"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class CollaborationProtocol(BaseModel):
    """A structured collaboration protocol instance between Agents.

    Supports proposal/vote/consensus/auction/handoff patterns for
    structured multi-Agent decision making.
    """

    __tablename__ = "collaboration_protocols"

    protocol_type = Column(String(30), nullable=False, comment="Protocol type: proposal, vote, consensus, auction, handoff")
    status = Column(String(20), nullable=False, default="open", comment="Current status")
    title = Column(String(500), nullable=False, comment="Protocol title / proposal subject")
    description = Column(Text, comment="Detailed description of the proposal/protocol")
    initiator_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, comment="Agent who initiated")
    channel_id = Column(Integer, ForeignKey("agent_channels.id"), nullable=True, comment="Associated channel")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Project scope")
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, comment="Related task")
    # Protocol config
    config = Column(JSON, comment="Protocol-specific config (e.g. quorum, timeout, auction rules)")
    # Results
    result = Column(JSON, comment="Protocol result (e.g. vote counts, winning bid, consensus outcome)")
    deadline = Column(DateTime, comment="Optional deadline for voting/response")
    resolved_at = Column(DateTime, comment="When the protocol was resolved")

    initiator = relationship("Agent", foreign_keys=[initiator_agent_id])
    channel = relationship("AgentChannel")
    project = relationship("Project")
    task = relationship("Task")
    messages = relationship("ProtocolMessage", back_populates="protocol", cascade="all, delete-orphan")

    def to_dict(self, include_messages=False):
        result = super().to_dict()
        result["protocol_type"] = self.protocol_type
        result["status"] = self.status
        result["config"] = self.config or {}
        result["result"] = self.result or {}
        if include_messages:
            result["messages"] = [m.to_dict() for m in self.messages]
        return result


class ProtocolMessage(BaseModel):
    """A message within a collaboration protocol (vote, bid, response, etc.)."""

    __tablename__ = "protocol_messages"

    protocol_id = Column(Integer, ForeignKey("collaboration_protocols.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, comment="Agent who sent this message")
    message_type = Column(String(30), nullable=False, comment="Type: vote, bid, accept, reject, comment, counter_proposal")
    content = Column(Text, comment="Message content")
    payload = Column(JSON, comment="Structured data (e.g. vote choice, bid amount, counter-proposal)")

    protocol = relationship("CollaborationProtocol", back_populates="messages")
    agent = relationship("Agent")

    def to_dict(self):
        result = super().to_dict()
        result["payload"] = self.payload or {}
        return result


class AgentExperience(BaseModel):
    """An experience record capturing what an Agent learned from executing a task.

    Experiences are automatically extracted from task outcomes (success/failure
    patterns) and can be shared across agents for collective learning. Each
    experience captures the task context, the strategy used, and the outcome
    pattern so that similar future tasks can benefit.
    """

    __tablename__ = "agent_experiences"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent that had this experience")
    experience_type = Column(String(50), nullable=False, default="success_pattern",
                             comment="Type: success_pattern, failure_pattern, strategy, optimization, anti_pattern")
    domain = Column(String(100), comment="Knowledge domain (e.g. 'python', 'frontend', 'devops')")
    task_type = Column(String(100), comment="Category of task (e.g. 'code_review', 'bug_fix', 'deployment')")
    capabilities_used = Column(JSON, default=list, comment="Capabilities that were relevant to this task")
    strategy = Column(Text, comment="Strategy or approach used (what was done)")
    outcome_pattern = Column(Text, comment="What happened — success factors or failure reasons")
    key_learnings = Column(Text, comment="Concise takeaways for future similar tasks")
    confidence = Column(Float, default=0.7, comment="Confidence in this experience (0.0-1.0)")
    applicability_score = Column(Float, default=0.5, comment="How broadly applicable this experience is (0.0-1.0)")
    source_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, comment="Task that generated this experience")
    source_step_key = Column(String(100), comment="Workflow step key that generated this experience")
    source_workflow_run_id = Column(Integer, comment="Workflow run that generated this experience")
    is_shared = Column(Boolean, default=False, comment="Whether this experience is shared with other agents")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Project scope")
    times_reused = Column(Integer, default=0, comment="How many times this experience was recommended/reused")
    last_reused_at = Column(DateTime, comment="Last time this experience was referenced")
    is_valid = Column(Boolean, default=True, comment="Whether this experience is still considered valid")

    agent = relationship("Agent", backref="experiences")
    source_task = relationship("Task")
    project = relationship("Project")

    def to_dict(self):
        result = super().to_dict()
        result["capabilities_used"] = self.capabilities_used or []
        return result

    @classmethod
    def extract_from_step_outcome(cls, agent_id, step_run, step_def, task=None):
        """Auto-extract an experience from a completed workflow step.

        Creates a structured experience record based on the step outcome,
        capturing what worked or what went wrong.
        """
        success = step_run.status == StepStatus.SUCCEEDED
        experience_type = "success_pattern" if success else "failure_pattern"

        # Determine domain from step capabilities
        capabilities_used = step_def.required_capabilities if step_def else []
        domain = capabilities_used[0] if capabilities_used else None

        # Build strategy description
        strategy_parts = []
        if step_def and step_def.name:
            strategy_parts.append(f"Step: {step_def.name}")
        if step_def and step_def.on_failure:
            strategy_parts.append(f"Failure strategy: {step_def.on_failure}")
        if step_run.agent_id:
            strategy_parts.append(f"Executed by agent #{step_run.agent_id}")
        strategy = "; ".join(strategy_parts) if strategy_parts else None

        # Build outcome pattern
        if success:
            outcome_parts = ["Task completed successfully."]
            if step_run.result_summary:
                outcome_parts.append(f"Result: {step_run.result_summary[:500]}")
            if step_run.started_at and step_run.finished_at:
                duration = (step_run.finished_at - step_run.started_at).total_seconds()
                outcome_parts.append(f"Duration: {duration:.1f}s")
            outcome_pattern = " ".join(outcome_parts)
        else:
            outcome_parts = ["Task failed."]
            if step_run.error:
                outcome_parts.append(f"Error: {step_run.error[:500]}")
            outcome_pattern = " ".join(outcome_parts)

        # Build key learnings
        if success:
            learnings = []
            if capabilities_used:
                learnings.append(f"Capabilities {', '.join(capabilities_used)} were effective for this task type.")
            if step_def and step_def.on_failure == "continue":
                learnings.append("Continue-on-failure strategy allowed workflow to progress.")
            key_learnings = " ".join(learnings) if learnings else "Approach was successful; repeat for similar tasks."
        else:
            learnings = []
            if step_run.error:
                learnings.append(f"Avoid: {step_run.error[:200]}")
            if capabilities_used:
                learnings.append(f"Capabilities {', '.join(capabilities_used)} may be insufficient alone.")
            key_learnings = " ".join(learnings) if learnings else "This approach failed; consider alternative strategy."

        # Determine confidence based on consistency
        confidence = 0.7 if success else 0.6
        if step_run.attempt and step_run.attempt > 1:
            confidence *= 0.8  # Lower confidence for retried steps

        experience = cls.create(
            agent_id=agent_id,
            experience_type=experience_type,
            domain=domain,
            task_type=step_def.name if step_def else None,
            capabilities_used=capabilities_used,
            strategy=strategy,
            outcome_pattern=outcome_pattern,
            key_learnings=key_learnings,
            confidence=round(confidence, 2),
            applicability_score=0.5,
            source_task_id=task.id if task else None,
            source_step_key=step_run.step_key,
            source_workflow_run_id=step_run.run_id,
            is_shared=False,
        )
        db.session.flush()
        return experience

    @classmethod
    def find_relevant_experiences(cls, agent_id, domain=None, task_type=None,
                                  capabilities=None, experience_type=None,
                                  include_shared=True, limit=10):
        """Find experiences relevant to a given task context.

        Searches the agent's own experiences and optionally shared experiences
        from other agents in the same domain/capability space.
        """
        query = cls.query.filter(cls.is_valid == True)

        if include_shared:
            query = query.filter(
                (cls.agent_id == agent_id) | (cls.is_shared == True)
            )
        else:
            query = query.filter(cls.agent_id == agent_id)

        if domain:
            query = query.filter(cls.domain == domain)
        if task_type:
            query = query.filter(cls.task_type == task_type)
        if experience_type:
            query = query.filter(cls.experience_type == experience_type)

        # Filter by capabilities overlap (JSON contains)
        if capabilities:
            # Use a simple approach: filter experiences where any capability matches
            for cap in capabilities[:3]:  # Limit to avoid overly complex queries
                query = query.filter(cls.capabilities_used.contains([cap]))

        # Order by confidence and reuse count
        query = query.order_by(cls.confidence.desc(), cls.times_reused.desc())
        return query.limit(limit).all()

    @classmethod
    def apply_decay(cls, agent_id=None, days_threshold=30, decay_rate=0.02):
        """Apply time-based confidence decay to experiences.

        Experiences that haven't been reused recently lose confidence over time,
        simulating the natural obsolescence of knowledge. Experiences that are
        frequently reused maintain or increase their confidence.

        Args:
            agent_id: Optional agent to scope the decay to (None = all agents)
            days_threshold: Only decay experiences older than this many days
            decay_rate: Confidence reduction per decay cycle (0.0-1.0)

        Returns:
            Number of experiences that were decayed.
        """
        cutoff = datetime.utcnow() - timedelta(days=days_threshold)
        query = cls.query.filter(
            cls.is_valid == True,
            cls.confidence > 0.1,  # Don't decay below 0.1
            cls.updated_at < cutoff,
        )
        if agent_id:
            query = query.filter_by(agent_id=agent_id)

        experiences = query.all()
        decayed_count = 0
        for exp in experiences:
            # Calculate days since last use or update
            reference_time = exp.last_reused_at or exp.updated_at or exp.created_at
            if not reference_time:
                continue
            days_idle = (datetime.utcnow() - reference_time).days

            if days_idle > days_threshold:
                # Apply decay: more idle = more decay
                decay_factor = 1 - (decay_rate * (days_idle / days_threshold))
                new_confidence = max(0.1, exp.confidence * decay_factor)

                # Reuse boosts: if reused frequently, decay less
                if exp.times_reused and exp.times_reused > 3:
                    reuse_boost = min(0.2, exp.times_reused * 0.02)
                    new_confidence = min(1.0, new_confidence + reuse_boost)

                if new_confidence != exp.confidence:
                    exp.confidence = round(new_confidence, 3)
                    decayed_count += 1

                    # Mark as invalid if confidence drops too low
                    if exp.confidence <= 0.1:
                        exp.is_valid = False

        if decayed_count > 0:
            db.session.flush()
        return decayed_count

    @classmethod
    def cross_validate(cls, experience_id, validator_agent_id, is_accurate: bool):
        """Cross-validate an experience by another agent.

        When an agent validates or refutes a shared experience, it affects
        the experience's confidence. Multiple validations converge the
        confidence toward the community consensus.
        """
        exp = cls.query.filter_by(id=experience_id, is_valid=True).first()
        if not exp:
            return None

        # Don't validate own experiences (already accounted for)
        if exp.agent_id == validator_agent_id:
            return exp

        if is_accurate:
            # Validation: increase confidence, cap at 1.0
            boost = 0.05
            if exp.is_shared:
                boost = 0.08  # Shared experiences get more boost from validation
            exp.confidence = min(1.0, round(exp.confidence + boost, 3))
        else:
            # Refutation: decrease confidence significantly
            penalty = 0.15
            exp.confidence = max(0.0, round(exp.confidence - penalty, 3))
            if exp.confidence <= 0.2:
                exp.is_valid = False  # Low confidence after refutation -> mark invalid

        db.session.flush()
        return exp

    @classmethod
    def get_validation_stats(cls, agent_id):
        """Get validation statistics for an agent's experiences."""
        total = cls.query.filter_by(agent_id=agent_id, is_valid=True).count()
        shared = cls.query.filter_by(agent_id=agent_id, is_shared=True, is_valid=True).count()
        high_conf = cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_valid == True,
            cls.confidence >= 0.8,
        ).count()
        low_conf = cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_valid == True,
            cls.confidence < 0.5,
        ).count()
        avg_conf = db.session.query(
            db.func.avg(cls.confidence)
        ).filter(
            cls.agent_id == agent_id,
            cls.is_valid == True,
        ).scalar() or 0

        return {
            "total_experiences": total,
            "shared_experiences": shared,
            "high_confidence": high_conf,
            "low_confidence": low_conf,
            "average_confidence": round(float(avg_conf), 3),
        }

    @classmethod
    def share_experience(cls, experience_id, agent_id):
        """Share an experience with other agents (mark as shared)."""
        exp = cls.query.filter_by(id=experience_id, agent_id=agent_id).first()
        if not exp:
            return None
        exp.is_shared = True
        db.session.flush()
        return exp

    @classmethod
    def learn_from_shared(cls, target_agent_id, experience_id):
        """An agent internalizes a shared experience from another agent.

        Creates a copy of the shared experience adapted for the learning agent,
        with reduced confidence (since it's second-hand knowledge).
        """
        source = cls.query.filter_by(id=experience_id, is_shared=True, is_valid=True).first()
        if not source:
            return None
        # Don't learn from own experiences
        if source.agent_id == target_agent_id:
            return source

        # Check if already learned
        existing = cls.query.filter_by(
            agent_id=target_agent_id,
            source_task_id=source.source_task_id,
            experience_type=source.experience_type,
            domain=source.domain,
        ).first()
        if existing:
            return existing

        # Create adapted copy with reduced confidence
        learned = cls.create(
            agent_id=target_agent_id,
            experience_type=source.experience_type,
            domain=source.domain,
            task_type=source.task_type,
            capabilities_used=source.capabilities_used or [],
            strategy=source.strategy,
            outcome_pattern=f"[Learned from agent #{source.agent_id}] {source.outcome_pattern}",
            key_learnings=source.key_learnings,
            confidence=round(source.confidence * 0.8, 2),  # Reduced confidence for learned experiences
            applicability_score=source.applicability_score,
            source_task_id=source.source_task_id,
            source_step_key=source.source_step_key,
            is_shared=False,
        )
        db.session.flush()
        return learned


class AgentReputation(BaseModel):
    """Tracks an Agent's reputation score based on task performance.

    Reputation is updated after each task completion/failure and affects
    task assignment priority. Higher reputation = higher priority.
    """

    __tablename__ = "agent_reputations"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, unique=True, index=True, comment="Agent ID")
    score = Column(Float, nullable=False, default=50.0, comment="Reputation score (0-100, starts at 50)")
    total_tasks = Column(Integer, nullable=False, default=0, comment="Total tasks assigned")
    completed_tasks = Column(Integer, nullable=False, default=0, comment="Tasks completed successfully")
    failed_tasks = Column(Integer, nullable=False, default=0, comment="Tasks failed")
    avg_completion_time = Column(Float, comment="Average completion time in seconds")
    on_time_rate = Column(Float, default=1.0, comment="Ratio of tasks completed before deadline")
    quality_score = Column(Float, default=50.0, comment="Quality score based on review feedback (0-100)")
    last_updated_at = Column(DateTime, comment="Last time reputation was recalculated")

    agent = relationship("Agent", backref="reputation")

    def to_dict(self):
        result = super().to_dict()
        result["success_rate"] = (self.completed_tasks / self.total_tasks * 100) if self.total_tasks > 0 else 0
        return result

    @classmethod
    def get_or_create(cls, agent_id):
        """Get existing reputation record or create one with defaults."""
        rep = cls.query.filter_by(agent_id=agent_id).first()
        if not rep:
            rep = cls.create(
                agent_id=agent_id,
                score=50.0,
                total_tasks=0,
                completed_tasks=0,
                failed_tasks=0,
            )
            db.session.flush()
        return rep

    @classmethod
    def record_outcome(cls, agent_id, success: bool, completion_time=None, on_time=True, quality_delta=0):
        """Record a task outcome and update the reputation score.

        Scoring:
        - Success: +2 base, +bonus for on_time, +bonus for fast completion
        - Failure: -5 base
        - Quality feedback: +/- quality_delta
        """
        rep = cls.get_or_create(agent_id)
        rep.total_tasks += 1
        rep.last_updated_at = datetime.utcnow()

        if success:
            rep.completed_tasks += 1
            score_delta = 2.0
            if on_time:
                score_delta += 1.0
                rep.on_time_rate = (rep.on_time_rate * (rep.completed_tasks - 1) + 1.0) / rep.completed_tasks
            else:
                rep.on_time_rate = (rep.on_time_rate * (rep.completed_tasks - 1) + 0.0) / rep.completed_tasks
            if completion_time and rep.avg_completion_time:
                if completion_time < rep.avg_completion_time * 0.8:
                    score_delta += 1.0  # Fast completion bonus
                rep.avg_completion_time = (rep.avg_completion_time * (rep.completed_tasks - 1) + completion_time) / rep.completed_tasks
            elif completion_time:
                rep.avg_completion_time = completion_time
        else:
            rep.failed_tasks += 1
            score_delta = -5.0

        rep.quality_score = max(0, min(100, rep.quality_score + quality_delta))
        rep.score = max(0, min(100, rep.score + score_delta))
        db.session.flush()
        # Audit reputation-impacting outcomes (failures and quality deltas) so the
        # unified security event feed can surface them. Success-only updates are
        # too frequent and low-signal to audit individually.
        if not success or quality_delta != 0:
            try:
                AuditLog.record(
                    action="reputation.update", resource_type="agent", resource_id=agent_id,
                    actor_type="system", actor_agent_id=agent_id,
                    detail={"success": success, "score_delta": score_delta,
                            "quality_delta": quality_delta, "new_score": rep.score,
                            "total_tasks": rep.total_tasks},
                )
            except Exception:
                pass  # Never fail the reputation update on an audit error
        return rep


class CrossProjectAgent(BaseModel):
    """Authorizes an Agent to participate in a project it was not originally created in.

    This enables cross-project collaboration: an Agent owned by one user can be
    invited to work on tasks in another project, subject to the project owner's
    approval. The Agent retains its original owner but gains access to the
    target project's tasks and workflows.
    """

    __tablename__ = "cross_project_agents"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent being granted cross-project access")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True, comment="Target project the agent can access")
    authorized_by = Column(Integer, ForeignKey("users.id"), nullable=True, comment="User who authorized this cross-project access")
    role_in_project = Column(String(50), default="contributor", comment="Role in target project: contributor, reviewer, observer")
    capabilities_override = Column(JSON, comment="Override capabilities for this project context (null = use agent's own)")
    max_concurrent_tasks = Column(Integer, default=3, comment="Max concurrent tasks this agent can handle in this project")
    is_active = Column(Boolean, default=True, comment="Whether this cross-project authorization is currently active")
    expires_at = Column(DateTime, comment="Optional expiry time for this authorization")

    agent = relationship("Agent", backref="cross_project_access")
    project = relationship("Project", backref="external_agents")
    authorizer = relationship("User", foreign_keys=[authorized_by])

    __table_args__ = (
        # One authorization per agent per project
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        result["capabilities_override"] = self.capabilities_override or []
        if self.agent:
            result["agent_name"] = self.agent.name
            result["agent_kind"] = self.agent.kind.value if self.agent.kind else None
            result["agent_capabilities"] = self.agent.capabilities or []
        if self.project:
            result["project_name"] = self.project.name
        return result

    @classmethod
    def get_active_for_agent(cls, agent_id):
        """Get all active cross-project authorizations for an agent."""
        now = datetime.utcnow()
        return cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_active == True,
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        ).all()

    @classmethod
    def get_active_for_project(cls, project_id):
        """Get all active cross-project agents for a project."""
        now = datetime.utcnow()
        return cls.query.filter(
            cls.project_id == project_id,
            cls.is_active == True,
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        ).all()

    @classmethod
    def is_authorized(cls, agent_id, project_id):
        """Check if an agent is authorized for a project."""
        now = datetime.utcnow()
        return cls.query.filter(
            cls.agent_id == agent_id,
            cls.project_id == project_id,
            cls.is_active == True,
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        ).first() is not None

    @classmethod
    def get_effective_capabilities(cls, agent_id, project_id):
        """Get the effective capabilities for an agent in a project context.

        If the agent has a capabilities_override for this project, use that;
        otherwise fall back to the agent's own capabilities.
        """
        auth = cls.query.filter(
            cls.agent_id == agent_id,
            cls.project_id == project_id,
            cls.is_active == True,
        ).first()
        if auth and auth.capabilities_override:
            return auth.capabilities_override
        agent = Agent.query.get(agent_id)
        return agent.capabilities if agent else []


class SandboxLevel(enum.Enum):
    """Sandbox security level."""

    STRICT = "strict"  # No network, no filesystem write, tool whitelist only
    MODERATE = "moderate"  # Limited network (allowlist), scoped filesystem, tool allowlist
    PERMISSIVE = "permissive"  # Full network, broad filesystem, tool blocklist


class SandboxViolationType(enum.Enum):
    """Type of sandbox policy violation."""

    DISALLOWED_TOOL = "disallowed_tool"
    NETWORK_BLOCKED = "network_blocked"
    FS_WRITE_BLOCKED = "fs_write_blocked"
    FS_READ_BLOCKED = "fs_read_blocked"
    RESOURCE_LIMIT = "resource_limit"
    TIMEOUT = "timeout"
    CAPABILITY_EXCEED = "capability_exceed"


class SandboxExecutionStatus(enum.Enum):
    """Status of a sandboxed execution."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    VIOLATED = "violated"  # Terminated due to policy violation
    TIMEOUT = "timeout"
    REVOKED = "revoked"  # Manually terminated by owner


class AgentSandbox(BaseModel):
    """A security sandbox policy governing how an Agent may execute.

    A sandbox defines:
    - allowed_tools / blocked_tools: tool-level access control
    - allowed_network_hosts: egress network allowlist (empty = no network)
    - fs_write_paths / fs_read_paths: filesystem scope (scoped roots)
    - resource limits: max_memory_mb, max_cpu_seconds, max_output_tokens
    - timeout_seconds: hard wall-clock limit
    - security_level: a coarse preset that governs defaults

    Sandboxes are attached to an Agent (default policy) and can be referenced
    by WorkflowStep runs to enforce per-execution isolation.
    """

    __tablename__ = "agent_sandboxes"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Owner user ID")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Agent this sandbox is bound to (null = reusable template)")
    name = Column(String(255), nullable=False, comment="Sandbox name")
    description = Column(Text, comment="Sandbox description")
    security_level = Column(Enum(SandboxLevel), default=SandboxLevel.MODERATE, nullable=False, comment="Coarse security preset")
    # Tool access control
    allowed_tools = Column(JSON, comment="Whitelist of tool names (empty + strict = none allowed)")
    blocked_tools = Column(JSON, comment="Blocklist of tool names")
    # Network
    allowed_network_hosts = Column(JSON, comment="Allowed egress hosts (empty = no network in strict)")
    # Filesystem scope
    fs_write_paths = Column(JSON, comment="Allowed write path roots")
    fs_read_paths = Column(JSON, comment="Allowed read path roots")
    # Resource limits
    max_memory_mb = Column(Integer, comment="Max memory in MB (0 = unlimited)")
    max_cpu_seconds = Column(Integer, comment="Max CPU seconds (0 = unlimited)")
    max_output_tokens = Column(Integer, comment="Max output tokens per execution (0 = unlimited)")
    timeout_seconds = Column(Integer, comment="Hard wall-clock timeout (0 = no timeout)")
    # Lifecycle
    is_active = Column(Boolean, default=True, nullable=False, comment="Whether this sandbox policy is active")

    owner = relationship("User")
    agent = relationship("Agent")

    def to_dict(self, include_stats=False):
        result = super().to_dict()
        result["security_level"] = self.security_level.value if self.security_level else None
        result["allowed_tools"] = self.allowed_tools or []
        result["blocked_tools"] = self.blocked_tools or []
        result["allowed_network_hosts"] = self.allowed_network_hosts or []
        result["fs_write_paths"] = self.fs_write_paths or []
        result["fs_read_paths"] = self.fs_read_paths or []
        if include_stats:
            result["stats"] = {
                "total_executions": SandboxExecution.query.filter_by(sandbox_id=self.id).count(),
                "violations": SandboxViolation.query.filter_by(sandbox_id=self.id).count(),
            }
        return result

    @classmethod
    def get_for_agent(cls, agent_id):
        """Get the active sandbox bound to an agent, if any."""
        return cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_active == True,
        ).first()

    def check_tool(self, tool_name):
        """Check if a tool is permitted under this sandbox.

        Returns (allowed: bool, reason: str|None).
        """
        blocked = self.blocked_tools or []
        if tool_name in blocked:
            return False, f"Tool '{tool_name}' is blocked"
        allowed = self.allowed_tools or []
        # In STRICT level, an empty allowlist means no tools permitted
        if self.security_level == SandboxLevel.STRICT:
            if not allowed:
                return False, "Strict sandbox permits no tools (empty allowlist)"
            if tool_name not in allowed:
                return False, f"Tool '{tool_name}' not in strict allowlist"
        elif allowed:
            # Non-strict with a non-empty allowlist still enforces it
            if tool_name not in allowed:
                return False, f"Tool '{tool_name}' not in allowlist"
        return True, None

    def check_network(self, host):
        """Check if network egress to a host is permitted."""
        if self.security_level == SandboxLevel.STRICT:
            return False, "Strict sandbox blocks all network egress"
        allowlist = self.allowed_network_hosts or []
        if not allowlist:
            # MODERATE/PERMISSIVE with empty allowlist: PERMISSIVE allows all, MODERATE blocks
            if self.security_level == SandboxLevel.PERMISSIVE:
                return True, None
            return False, "Moderate sandbox blocks network without explicit allowlist"
        # Match host suffix against allowlist entries
        for allowed_host in allowlist:
            if host == allowed_host or host.endswith("." + allowed_host):
                return True, None
        return False, f"Host '{host}' not in network allowlist"

    def check_fs_write(self, path):
        """Check if a filesystem write path is permitted."""
        roots = self.fs_write_paths or []
        if self.security_level == SandboxLevel.STRICT and not roots:
            return False, "Strict sandbox blocks all filesystem writes"
        if self.security_level == SandboxLevel.PERMISSIVE and not roots:
            return True, None
        for root in roots:
            if path == root or path.startswith(root.rstrip("/") + "/"):
                return True, None
        return False, f"Path '{path}' not in write scope"

    def to_policy_dict(self):
        """Serialize the sandbox as a policy envelope for executors."""
        return {
            "security_level": self.security_level.value if self.security_level else None,
            "allowed_tools": self.allowed_tools or [],
            "blocked_tools": self.blocked_tools or [],
            "allowed_network_hosts": self.allowed_network_hosts or [],
            "fs_write_paths": self.fs_write_paths or [],
            "fs_read_paths": self.fs_read_paths or [],
            "max_memory_mb": self.max_memory_mb or 0,
            "max_cpu_seconds": self.max_cpu_seconds or 0,
            "max_output_tokens": self.max_output_tokens or 0,
            "timeout_seconds": self.timeout_seconds or 0,
        }


class SandboxExecution(BaseModel):
    """A concrete sandboxed execution record — one per Agent run under a sandbox.

    Tracks the policy snapshot at execution time (so audits remain valid even
    if the sandbox policy changes later), the execution status, and aggregated
    resource usage.
    """

    __tablename__ = "sandbox_executions"

    sandbox_id = Column(Integer, ForeignKey("agent_sandboxes.id"), nullable=False, index=True, comment="Sandbox policy used")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Executing Agent")
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=True, index=True, comment="Associated AgentRun (if any)")
    step_run_id = Column(Integer, ForeignKey("workflow_step_runs.id"), nullable=True, index=True, comment="Associated WorkflowStepRun (if any)")
    status = Column(Enum(SandboxExecutionStatus), default=SandboxExecutionStatus.RUNNING, nullable=False, index=True, comment="Execution status")
    policy_snapshot = Column(JSON, comment="Frozen sandbox policy at execution start")
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment="Execution start")
    ended_at = Column(DateTime, comment="Execution end")
    # Aggregated usage
    peak_memory_mb = Column(Integer, comment="Peak memory usage in MB")
    cpu_seconds = Column(Integer, comment="CPU seconds consumed")
    output_tokens = Column(Integer, comment="Output tokens produced")
    tool_calls = Column(Integer, default=0, comment="Number of tool calls made")
    network_calls = Column(Integer, default=0, comment="Number of network egress attempts")
    # Outcome
    output_summary = Column(Text, comment="Execution output summary")
    error = Column(Text, comment="Error message")
    termination_reason = Column(Text, comment="Why the execution ended (violation/timeout/etc.)")

    sandbox = relationship("AgentSandbox")
    agent = relationship("Agent")
    run = relationship("AgentRun")
    step_run = relationship("WorkflowStepRun")
    violations = relationship("SandboxViolation", back_populates="execution", cascade="all, delete-orphan")

    def to_dict(self, include_violations=False):
        result = super().to_dict()
        result["status"] = self.status.value if self.status else None
        result["policy_snapshot"] = self.policy_snapshot or {}
        if include_violations:
            result["violations"] = [v.to_dict() for v in (self.violations or [])]
        return result

    def record_violation(self, violation_type, detail, attempted_action=None):
        """Record a policy violation and append a SandboxViolation."""
        v = SandboxViolation(
            execution_id=self.id,
            sandbox_id=self.sandbox_id,
            agent_id=self.agent_id,
            violation_type=violation_type,
            attempted_action=attempted_action or detail,
            detail=detail,
        )
        db.session.add(v)
        db.session.flush()
        return v

    def finish(self, status, summary=None, error=None, reason=None):
        """Mark the execution as finished with the given status."""
        self.status = status
        self.ended_at = datetime.utcnow()
        if summary is not None:
            self.output_summary = summary
        if error is not None:
            self.error = error
        if reason is not None:
            self.termination_reason = reason


class SandboxViolation(BaseModel):
    """A recorded sandbox policy violation during an execution."""

    __tablename__ = "sandbox_violations"

    execution_id = Column(Integer, ForeignKey("sandbox_executions.id"), nullable=False, index=True, comment="SandboxExecution ID")
    sandbox_id = Column(Integer, ForeignKey("agent_sandboxes.id"), nullable=False, index=True, comment="Sandbox policy ID")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent that attempted the violation")
    violation_type = Column(Enum(SandboxViolationType), nullable=False, index=True, comment="Violation type")
    attempted_action = Column(Text, comment="What the agent tried to do")
    detail = Column(Text, comment="Why it was blocked / policy detail")
    blocked_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment="When the violation was detected")

    execution = relationship("SandboxExecution", back_populates="violations")

    def to_dict(self):
        result = super().to_dict()
        result["violation_type"] = self.violation_type.value if self.violation_type else None
        return result


class ConflictType(enum.Enum):
    """Type of Agent collaboration conflict."""

    DUPLICATE_CLAIM = "duplicate_claim"  # Multiple agents claimed/are assigned the same task
    RESOURCE_CONTENTION = "resource_contention"  # Concurrent writes to the same shared resource
    PROTOCOL_DEADLOCK = "protocol_deadlock"  # A protocol cannot reach resolution (no quorum / tie)
    CIRCULAR_DEPENDENCY = "circular_dependency"  # Workflow step dependency cycle
    CAPABILITY_OVERLAP = "capability_overlap"  # Multiple agents with identical capabilities idle
    PRIORITY_INVERSION = "priority_inversion"  # Low-priority task blocking high-priority one
    ASSIGNMENT_STALE = "assignment_stale"  # Assignment lease expired but still appears active


class ConflictSeverity(enum.Enum):
    """Severity of a detected conflict."""

    INFO = "info"  # Noted but no action needed
    WARNING = "warning"  # Likely to cause issues; recommend resolution
    CRITICAL = "critical"  # Active problem; workflow/agent may be blocked


class ConflictStatus(enum.Enum):
    """Lifecycle status of a conflict record."""

    DETECTED = "detected"  # Newly detected, awaiting resolution
    ACKNOWLEDGED = "acknowledged"  # Owner has seen it
    RESOLVING = "resolving"  # Auto/manual resolution in progress
    RESOLVED = "resolved"  # Successfully resolved
    IGNORED = "ignored"  # Dismissed without action


class ConflictResolutionStrategy(enum.Enum):
    """How a conflict was (or should be) resolved."""

    FIRST_WINS = "first_wins"  # Earliest claim/assignment kept, others revoked
    HIGHEST_REPUTATION = "highest_reputation"  # Agent with best reputation wins
    LEAST_LOADED = "least_loaded"  # Agent with fewest active tasks wins
    MANUAL = "manual"  # Resolved by a human
    AUTO_RETRY = "auto_retry"  # Re-queue the contested work
    SPLIT = "split"  # Divide the work among contenders
    ESCALATE = "escalate"  # Escalate to a leader/coordinator agent


class AgentConflict(BaseModel):
    """A detected collaboration conflict between Agents.

    Records the conflict type, the parties involved, contextual evidence, and
    the resolution trajectory. Conflicts are detected by maintenance scans and
    may be resolved automatically or manually.
    """

    __tablename__ = "agent_conflicts"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Owner user ID")
    conflict_type = Column(Enum(ConflictType), nullable=False, index=True, comment="Conflict type")
    severity = Column(Enum(ConflictSeverity), default=ConflictSeverity.WARNING, nullable=False, comment="Severity")
    status = Column(Enum(ConflictStatus), default=ConflictStatus.DETECTED, nullable=False, index=True, comment="Resolution status")
    # Resource context — what is being contested
    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Task involved (if any)")
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=True, index=True, comment="Workflow run involved (if any)")
    protocol_id = Column(Integer, ForeignKey("collaboration_protocols.id"), nullable=True, index=True, comment="Protocol involved (if any)")
    # Parties — list of agent IDs involved
    agent_ids = Column(JSON, comment="List of Agent IDs involved in the conflict")
    # Evidence & resolution
    title = Column(String(255), nullable=False, comment="Short conflict title")
    description = Column(Text, comment="Detailed conflict description")
    evidence = Column(JSON, comment="Structured evidence (snapshots, counts, etc.)")
    suggested_strategy = Column(Enum(ConflictResolutionStrategy), nullable=True, comment="Suggested resolution strategy")
    resolution = Column(Text, comment="Resolution description (filled on resolve)")
    resolution_strategy = Column(Enum(ConflictResolutionStrategy), nullable=True, comment="Strategy actually used")
    resolved_at = Column(DateTime, comment="When the conflict was resolved")
    resolved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, comment="User who resolved (if manual)")

    owner = relationship("User", foreign_keys=[owner_id])
    task = relationship("Task")
    workflow_run = relationship("WorkflowRun")
    protocol = relationship("CollaborationProtocol")

    def to_dict(self):
        result = super().to_dict()
        result["conflict_type"] = self.conflict_type.value if self.conflict_type else None
        result["severity"] = self.severity.value if self.severity else None
        result["status"] = self.status.value if self.status else None
        result["suggested_strategy"] = self.suggested_strategy.value if self.suggested_strategy else None
        result["resolution_strategy"] = self.resolution_strategy.value if self.resolution_strategy else None
        result["agent_ids"] = self.agent_ids or []
        result["evidence"] = self.evidence or {}
        return result

    @classmethod
    def get_active_for_owner(cls, owner_id):
        """Get all unresolved conflicts for an owner."""
        return cls.query.filter(
            cls.owner_id == owner_id,
            cls.status.in_([ConflictStatus.DETECTED, ConflictStatus.ACKNOWLEDGED, ConflictStatus.RESOLVING]),
        ).order_by(cls.created_at.desc()).all()

    def resolve(self, strategy, description, resolved_by_user_id=None):
        """Mark this conflict as resolved."""
        self.status = ConflictStatus.RESOLVED
        self.resolution_strategy = strategy
        self.resolution = description
        self.resolved_at = datetime.utcnow()
        self.resolved_by_user_id = resolved_by_user_id


class OrchestrationRun(BaseModel):
    """Historical record of a single global orchestration cycle.

    Written by the orchestrator (both the HTTP endpoint and the built-in
    scheduler) so operators can review trends over time — stale agent counts,
    timed-out steps, triggered runs, auto-resolved conflicts, duration, errors.
    """
    __tablename__ = "agent_orchestration_runs"

    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    triggered_by = db.Column(db.String(32), nullable=False)  # "manual" | "scheduler"
    stale_agents = db.Column(db.Integer, default=0)
    expired_leases = db.Column(db.Integer, default=0)
    escalated_tasks = db.Column(db.Integer, default=0)
    timed_out_steps = db.Column(db.Integer, default=0)
    triggers_fired = db.Column(db.Integer, default=0)
    trigger_run_ids = db.Column(db.JSON, default=list)
    conflicts_detected = db.Column(db.Integer, default=0)
    conflicts_auto_resolved = db.Column(db.Integer, default=0)
    conflicts_skipped = db.Column(db.Integer, default=0)
    error_count = db.Column(db.Integer, default=0)
    error_details = db.Column(db.JSON, default=list)
    duration_seconds = db.Column(db.Float, default=0.0)
    summary = db.Column(db.Text)

    owner = db.relationship("User", backref="orchestration_runs")

    def to_dict(self):
        result = super().to_dict()
        result["trigger_run_ids"] = self.trigger_run_ids or []
        result["error_details"] = self.error_details or []
        return result

    @classmethod
    def record(cls, owner_id, triggered_by, report, duration, summary):
        """Persist one orchestration cycle. Returns the created row."""
        entry = cls(
            owner_id=owner_id,
            triggered_by=triggered_by,
            stale_agents=report.get("stale_agents", 0),
            expired_leases=report.get("expired_leases", 0),
            escalated_tasks=report.get("escalated_tasks", 0),
            timed_out_steps=report.get("timed_out_steps", 0),
            triggers_fired=report.get("triggers_fired", 0),
            trigger_run_ids=report.get("trigger_run_ids", []),
            conflicts_detected=report.get("conflicts_detected", 0),
            conflicts_auto_resolved=report.get("conflicts_auto_resolved", 0),
            conflicts_skipped=report.get("conflicts_skipped", 0),
            error_count=len(report.get("errors", [])),
            error_details=report.get("errors", []),
            duration_seconds=round(duration, 3),
            summary=summary,
        )
        db.session.add(entry)
        db.session.flush()
        return entry
