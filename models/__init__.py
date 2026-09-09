"""
Todo for AI - 数据模型包

包含所有数据库模型的定义和关系。
"""

from .base import db
from .user import User, UserRole, UserStatus
from .project import Project, ProjectStatus
from .organization import (
    Organization,
    OrganizationStatus,
    OrganizationMember,
    OrganizationRole,
    OrganizationMemberStatus,
    OrganizationRoleDefinition,
    OrganizationMemberRole,
)
from .organization_agent_member import OrganizationAgentMember, OrganizationAgentMemberStatus
from .project_member import ProjectMember, ProjectMemberRole, ProjectMemberStatus
from .task import Task, TaskStatus, TaskPriority
from .task_label import TaskLabel, BUILTIN_TASK_LABELS
from .context_rule import ContextRule
from .task_history import TaskHistory, ActionType
from .task_evidence import TaskEvidenceRecord
from .project_repo import ProjectRepoBinding
from .github_app import GitHubAppConfig
from .budget import Budget
from .goal import Goal, GoalStatus, Epic, EpicStatus
from .goal_loop import GoalLoop, GoalLoopStatus, GOAL_LOOP_TAG_PREFIX
from .knowledge_proposal import ProjectKnowledgeProposal
from .workspace_sso_config import WorkspaceSSOConfig
from .external_connector_config import ExternalConnectorConfig
from .attachment import Attachment
from .api_token import ApiToken
from .user_project_pin import UserProjectPin
from .user_activity import UserActivity
from .user_settings import UserSettings
from .system_settings import SystemSettings
from .custom_prompt import CustomPrompt, PromptType
from .agent import Agent, AgentStatus
from .agent_soul_version import AgentSoulVersion
from .agent_secret import AgentSecret
from .agent_secret_share import AgentSecretShare
from .agent_secret_grant import AgentSecretGrant
from .secret_audit import SecretAuditLog, SecretAuditAction, SecretApprovalRequest
from .agent_key import AgentKey
from .agent_notification_receipt import AgentNotificationReceipt
from .agent_session import AgentSession
from .agent_task_attempt import AgentTaskAttempt, AgentTaskAttemptState
from .agent_task_lease import AgentTaskLease
from .agent_task_event import AgentTaskEvent
from .agent_result_dedup import AgentResultDedup
from .agent_trigger import AgentTrigger, AgentTriggerType, AgentMisfirePolicy, AgentTriggerAction
from .agent_run import AgentRun, AgentRunState
from .agent_connect_link import AgentConnectLink
from .agent_audit_event import AgentAuditEvent
from .agent_activity_event import AgentActivityEvent
from .task_log import TaskLog, TaskLogActorType
from .task_event_outbox import TaskEventOutbox
from .organization_event import OrganizationEvent
from .notification_channel import NotificationChannel, NotificationScopeType, NotificationChannelType
from .notification_delivery import NotificationDelivery, NotificationDeliveryStatus
from .notification_event import NotificationEvent
from .user_notification import UserNotification
from .agent_role_template import AgentRoleTemplate, AgentRoleTemplateStatus
from .workspace_runtime_setting import WorkspaceRuntimeSetting
from .agent_team import AgentTeam, AgentTeamStatus, AgentTeamMember, AgentTeamMemberRole
from .agent_team_project import AgentTeamProject
from .team_task_orchestration import (
    TeamTaskOrchestration, OrchestrationStrategy, OrchestrationStatus,
    TeamSubtask, SubtaskStatus
)
from .ai_request_log import AIRequestLog

from .agent_runtime_monitor import (
    AgentHeartbeat,
    AgentMetrics,
    AgentRuntimeConfig,
)

# ── 协作平台模型（原 models/agent.py 大文件拆分的子模块）──
from .agent_core import (
    AgentKind,
    AgentRunStatus,
    TaskAssignment,
    TaskAssignmentState,
    RunLog,
    LEASED_EXECUTION_STATES,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
    ACTIVE_ASSIGNMENT_STATES,
    AGENT_OFFLINE_AFTER_SECONDS,
    has_live_agent_assignment,
    mark_stale_agents_offline,
)
from .audit_project import AuditLog
from .task_collab import TaskTemplate, TaskEvent, Notification, SharedContext
from .workflow import (
    Workflow,
    WorkflowStatus,
    StepStatus,
    WorkflowStep,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTrigger,
    WorkflowVersion,
)
from .channels import (
    AgentChannel,
    AgentChannelMember,
    AgentChannelMessage,
    CollaborationTemplate,
    KnowledgeEntry,
)
from .protocols import (
    CollaborationProtocol,
    ProtocolMessage,
    ProtocolType,
    ProtocolStatus,
)
from .experience_reputation import AgentExperience, AgentReputation, CrossProjectAgent
from .sandbox import (
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxLevel,
    SandboxViolationType,
    SandboxExecutionStatus,
)
from .conflicts import (
    AgentConflict,
    OrchestrationRun,
    ConflictType,
    ConflictSeverity,
    ConflictStatus,
    ConflictResolutionStrategy,
)
from .project_member import ProjectRole  # noqa: F401  (兼容别名，见 project_member.py)

__all__ = [
    'db',
    'User',
    'UserRole',
    'UserStatus',
    'Project',
    'ProjectStatus',
    'Organization',
    'OrganizationStatus',
    'OrganizationMember',
    'OrganizationRole',
    'OrganizationMemberStatus',
    'OrganizationRoleDefinition',
    'OrganizationMemberRole',
    'OrganizationAgentMember',
    'OrganizationAgentMemberStatus',
    'ProjectMember',
    'ProjectMemberRole',
    'ProjectMemberStatus',
    'Task',
    'TaskStatus',
    'TaskPriority',
    'TaskLabel',
    'BUILTIN_TASK_LABELS',
    'ContextRule',
    'TaskHistory',
    'TaskEvidenceRecord',
    'ProjectRepoBinding',
    'GitHubAppConfig',
    'Budget',
    'Goal',
    'GoalStatus',
    'Epic',
    'EpicStatus',
    'ProjectKnowledgeProposal',
    'WorkspaceSSOConfig',
    'ExternalConnectorConfig',
    'ActionType',
    'Attachment',
    'ApiToken',
    'UserProjectPin',
    'UserActivity',
    'UserSettings',
    'CustomPrompt',
    'PromptType',
    'Agent',
    'AgentStatus',
    'AgentSoulVersion',
    'AgentSecret',
    'AgentSecretShare',
    'AgentSecretGrant',
    'SecretAuditLog',
    'SecretAuditAction',
    'SecretApprovalRequest',
    'AgentKey',
    'AgentNotificationReceipt',
    'AgentSession',
    'AgentTaskAttempt',
    'AgentTaskAttemptState',
    'AgentTaskLease',
    'AgentTaskEvent',
    'AgentResultDedup',
    'AgentTrigger',
    'AgentTriggerType',
    'AgentMisfirePolicy',
    'AgentTriggerAction',
    'AgentRun',
    'AgentRunState',
    'AgentConnectLink',
    'AgentAuditEvent',
    'AgentActivityEvent',
    'TaskLog',
    'TaskLogActorType',
    'TaskEventOutbox',
    'OrganizationEvent',
    'NotificationChannel',
    'NotificationScopeType',
    'NotificationChannelType',
    'NotificationDelivery',
    'NotificationDeliveryStatus',
    'NotificationEvent',
    'UserNotification',
    'AgentRoleTemplate',
    'WorkspaceRuntimeSetting',
    'AgentRoleTemplateStatus',
    'AgentTeam',
    'AgentTeamStatus',
    'AgentTeamMember',
    'AgentTeamMemberRole',
    'AgentTeamProject',
    'TeamTaskOrchestration',
    'OrchestrationStrategy',
    'OrchestrationStatus',
    'TeamSubtask',
    'SubtaskStatus',
    'AIRequestLog',
    # Agent Runtime Monitoring
    'AgentHeartbeat',
    'AgentMetrics',
    'AgentRuntimeConfig',
    # Agent 协作平台
    'AgentKind',
    'AgentRunStatus',
    'TaskAssignment',
    'TaskAssignmentState',
    'RunLog',
    'LEASED_EXECUTION_STATES',
    'HUMAN_BLOCKING_ASSIGNMENT_STATES',
    'ACTIVE_ASSIGNMENT_STATES',
    'AGENT_OFFLINE_AFTER_SECONDS',
    'has_live_agent_assignment',
    'mark_stale_agents_offline',
    'AuditLog',
    'ProjectRole',
    'TaskTemplate',
    'TaskEvent',
    'Notification',
    'SharedContext',
    'Workflow',
    'WorkflowStatus',
    'StepStatus',
    'WorkflowStep',
    'WorkflowRun',
    'WorkflowStepRun',
    'WorkflowTrigger',
    'WorkflowVersion',
    'AgentChannel',
    'AgentChannelMember',
    'AgentChannelMessage',
    'CollaborationTemplate',
    'KnowledgeEntry',
    'CollaborationProtocol',
    'ProtocolMessage',
    'ProtocolType',
    'ProtocolStatus',
    'AgentExperience',
    'AgentReputation',
    'CrossProjectAgent',
    'AgentSandbox',
    'SandboxExecution',
    'SandboxViolation',
    'SandboxLevel',
    'SandboxViolationType',
    'SandboxExecutionStatus',
    'AgentConflict',
    'OrchestrationRun',
    'ConflictType',
    'ConflictSeverity',
    'ConflictStatus',
    'ConflictResolutionStrategy',
]
