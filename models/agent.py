"""
Agent collaboration models (legacy re-export shim).

Model definitions have been split into functional submodules:
- agent_core              Agent, TaskAssignment, AgentRun, RunLog + enums + helpers
- task_collab             TaskTemplate, TaskEvent, Notification, SharedContext
- workflow                Workflow, WorkflowStep, WorkflowRun, WorkflowStepRun,
                          WorkflowTrigger, WorkflowVersion + enums
- audit_project           AuditLog, ProjectRole, ProjectMember
- channels                AgentChannel(+Member+Message), CollaborationTemplate,
                          KnowledgeEntry
- protocols               CollaborationProtocol, ProtocolMessage + enums
- experience_reputation   AgentExperience, AgentReputation, CrossProjectAgent
- sandbox                 AgentSandbox, SandboxExecution, SandboxViolation + enums
- conflicts               AgentConflict, OrchestrationRun + enums

This module re-exports every name so existing
``from models.agent import X`` statements keep working without modification.
The real definitions live in the submodules above; importing this shim pulls
them all into a single namespace (and ensures SQLAlchemy relationship string
references resolve against a fully-populated metadata registry).
"""

from .agent_core import (  # noqa: F401
    AgentStatus,
    AgentKind,
    TaskAssignmentState,
    AgentRunStatus,
    Agent,
    TaskAssignment,
    AgentRun,
    RunLog,
    has_live_agent_assignment,
    mark_stale_agents_offline,
    LEASED_EXECUTION_STATES,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
    ACTIVE_ASSIGNMENT_STATES,
    AGENT_OFFLINE_AFTER_SECONDS,
)
from .task_collab import (  # noqa: F401
    TaskTemplate,
    TaskEvent,
    Notification,
    SharedContext,
)
from .workflow import (  # noqa: F401
    WorkflowStatus,
    StepStatus,
    Workflow,
    WorkflowStep,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTrigger,
    WorkflowVersion,
)
from .audit_project import (  # noqa: F401
    AuditLog,
    ProjectRole,
    ProjectMember,
)
from .channels import (  # noqa: F401
    AgentChannel,
    AgentChannelMember,
    AgentChannelMessage,
    CollaborationTemplate,
    KnowledgeEntry,
)
from .protocols import (  # noqa: F401
    ProtocolType,
    ProtocolStatus,
    CollaborationProtocol,
    ProtocolMessage,
)
from .experience_reputation import (  # noqa: F401
    AgentExperience,
    AgentReputation,
    CrossProjectAgent,
)
from .sandbox import (  # noqa: F401
    SandboxLevel,
    SandboxViolationType,
    SandboxExecutionStatus,
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
)
from .conflicts import (  # noqa: F401
    ConflictType,
    ConflictSeverity,
    ConflictStatus,
    ConflictResolutionStrategy,
    AgentConflict,
    OrchestrationRun,
)
