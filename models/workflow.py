"""
Workflow models: Workflow, WorkflowStep, WorkflowRun, WorkflowStepRun,
WorkflowTrigger, WorkflowVersion and their status enums.
"""

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


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

