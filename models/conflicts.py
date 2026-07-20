"""
Agent conflict and orchestration run models.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


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

