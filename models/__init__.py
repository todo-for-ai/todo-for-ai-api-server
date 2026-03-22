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
from .attachment import Attachment
from .api_token import ApiToken
from .user_project_pin import UserProjectPin
from .user_activity import UserActivity
from .user_settings import UserSettings
from .custom_prompt import CustomPrompt, PromptType
from .agent import Agent, AgentStatus
from .agent_soul_version import AgentSoulVersion
from .agent_secret import AgentSecret
from .agent_secret_share import AgentSecretShare
from .agent_secret_grant import AgentSecretGrant
from .agent_key import AgentKey
from .agent_session import AgentSession
from .agent_task_attempt import AgentTaskAttempt, AgentTaskAttemptState
from .agent_task_lease import AgentTaskLease
from .agent_task_event import AgentTaskEvent
from .agent_result_dedup import AgentResultDedup
from .agent_trigger import AgentTrigger, AgentTriggerType, AgentMisfirePolicy
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
from .agent_team import AgentTeam, AgentTeamStatus, AgentTeamMember, AgentTeamMemberRole
from .agent_team_project import AgentTeamProject
from .team_task_orchestration import (
    TeamTaskOrchestration, OrchestrationStrategy, OrchestrationStatus,
    TeamSubtask, SubtaskStatus
)

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
    'RuleType',
    'TaskHistory',
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
    'AgentKey',
    'AgentSession',
    'AgentTaskAttempt',
    'AgentTaskAttemptState',
    'AgentTaskLease',
    'AgentTaskEvent',
    'AgentResultDedup',
    'AgentTrigger',
    'AgentTriggerType',
    'AgentMisfirePolicy',
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
]
