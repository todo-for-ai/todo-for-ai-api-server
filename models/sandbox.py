"""
Agent sandbox models: AgentSandbox, SandboxExecution, SandboxViolation and enums.
"""

import enum
from datetime import datetime

from sqlalchemy import (
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

