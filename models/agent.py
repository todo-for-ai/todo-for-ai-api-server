"""
Agent 模型（统一版）

`agents` 表的唯一 ORM 映射，同时服务两个子系统：
- 工作区运行时（agent_runtime_* / agent-runtime 容器）：workspace_id/runner/sandbox/SOUL 等字段
- 协作编排（api/agents 包）：owner_id/kind/capabilities/last_seen_at 等字段

历史上有两套 Agent 模型分别映射本表（models/agent.py 与 models/agent_core.py），
2026-08-31 合并冲突后统一为此文件；协作侧字段以可空列并入，见
migrations/unify_agent_collaboration_schema.py。
"""

import enum
from datetime import datetime

from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, JSON, DECIMAL, Boolean, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel, db


class AgentStatus(enum.Enum):
    """Agent 状态（含协作侧扩展值 paused/offline/disabled）"""

    ACTIVE = 'active'
    INACTIVE = 'inactive'
    REVOKED = 'revoked'
    PAUSED = 'paused'
    OFFLINE = 'offline'
    DISABLED = 'disabled'


class AgentKind(enum.Enum):
    """Agent 在协作平台中的角色/类型"""

    ASSISTANT = 'assistant'
    AUTONOMOUS = 'autonomous'
    COORDINATOR = 'coordinator'
    EXTERNAL = 'external'


class Agent(BaseModel):
    """Agent（工作区 + 协作统一模型）"""

    __tablename__ = 'agents'

    # ── 工作区运行时字段 ──
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True, comment='所属工作区(组织)ID（协作侧创建的 Agent 可为空）')
    creator_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True, comment='创建者用户ID（协作侧创建的 Agent 可为空）')

    name = Column(String(128), nullable=False, comment='Agent 名称')
    display_name = Column(String(128), comment='展示名称')
    avatar_url = Column(String(512), comment='头像URL')
    homepage_url = Column(String(512), comment='主页URL')
    contact_email = Column(String(255), comment='联系邮箱')
    description = Column(Text, comment='Agent 描述')
    status = Column(Enum(AgentStatus), default=AgentStatus.ACTIVE, nullable=False, comment='Agent 状态')
    capability_tags = Column(JSON, comment='能力标签')
    allowed_project_ids = Column(JSON, comment='允许访问的项目ID列表')
    llm_provider = Column(String(64), comment='LLM供应商')
    llm_model = Column(String(128), comment='LLM模型')
    temperature = Column(DECIMAL(4, 3), default=0.7, comment='采样温度')
    top_p = Column(DECIMAL(4, 3), default=1.0, comment='Top-p')
    max_output_tokens = Column(Integer, comment='最大输出token')
    context_window_tokens = Column(Integer, comment='上下文窗口大小')
    reasoning_mode = Column(String(32), default='balanced', comment='推理模式')
    system_prompt = Column(Text, comment='系统提示词')
    soul_markdown = Column(Text, comment='SOUL.md内容')
    response_style = Column(JSON, comment='响应风格')
    tool_policy = Column(JSON, comment='工具策略')
    memory_policy = Column(JSON, comment='记忆策略')
    handoff_policy = Column(JSON, comment='移交策略')
    execution_mode = Column(String(32), default='external_pull', nullable=False, comment='执行模式')
    runner_enabled = Column(Boolean, default=False, nullable=False, comment='是否启用平台托管Runner')
    sandbox_profile = Column(String(64), default='standard', nullable=False, comment='沙箱配置档位')
    sandbox_policy = Column(JSON, comment='沙箱策略')
    max_concurrency = Column(Integer, default=1, comment='最大并发')
    max_retry = Column(Integer, default=2, comment='最大重试次数')
    timeout_seconds = Column(Integer, default=1800, comment='超时时间(秒)')
    heartbeat_interval_seconds = Column(Integer, default=20, comment='心跳间隔(秒)')
    soul_version = Column(Integer, default=1, nullable=False, comment='SOUL版本号')
    config_version = Column(Integer, default=1, nullable=False, comment='配置版本号')
    runner_config_version = Column(Integer, default=1, nullable=False, comment='Runner配置版本号')

    # Notification channels configuration (Feishu, WeCom, DingTalk, etc.)
    notification_channels = Column(JSON, comment='通知渠道配置')

    # ── 协作编排字段（原 agent_core.Agent）──
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True, comment='Agent 所有者用户ID（协作侧）')
    kind = Column(Enum(AgentKind), default=AgentKind.ASSISTANT, comment='Agent 角色/类型')
    provider = Column(String(100), comment='Provider, e.g. openai/anthropic/local')
    model = Column(String(255), comment='默认模型或运行时名称')
    capabilities = Column(JSON, comment='能力描述列表')
    skill_profile = Column(JSON, comment='P3.1 技能画像（运行历史聚合: skills 列表 + assignments 统计）')
    skill_profile_updated_at = Column(DateTime, comment='画像最近重建时间')
    config = Column(JSON, comment='非敏感运行时配置（协作侧）')
    collaboration_role = Column(String(50), nullable=True, comment='协作角色: leader, follower, standalone')
    last_seen_at = Column(DateTime, comment='最近心跳时间')
    is_system = Column(Boolean, default=False, nullable=False, comment='是否系统管理的 Agent')

    # ── 关系 ──
    workspace = relationship('Organization', foreign_keys=[workspace_id])
    creator = relationship('User', foreign_keys=[creator_user_id])
    keys = relationship('AgentKey', back_populates='agent', cascade='all, delete-orphan', lazy='dynamic')
    soul_versions = relationship('AgentSoulVersion', back_populates='agent', cascade='all, delete-orphan', lazy='dynamic')
    secrets = relationship('AgentSecret', back_populates='agent', cascade='all, delete-orphan', lazy='dynamic')
    assignments = relationship('TaskAssignment', back_populates='agent', lazy='dynamic')
    runs = relationship('AgentRun', back_populates='agent', lazy='dynamic')

    def __repr__(self):
        return f'<Agent {self.id}: {self.name}>'

    def to_dict(self, include_stats=False):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        data['kind'] = self.kind.value if self.kind else None
        data['capability_tags'] = self.capability_tags or []
        data['capabilities'] = self.capabilities or []
        data['config'] = self.config or {}
        data['collaboration_role'] = self.collaboration_role or 'standalone'
        data['allowed_project_ids'] = self.allowed_project_ids or []
        data['response_style'] = self.response_style or {}
        data['tool_policy'] = self.tool_policy or {}
        data['memory_policy'] = self.memory_policy or {}
        data['handoff_policy'] = self.handoff_policy or {}
        data['execution_mode'] = self.execution_mode or 'external_pull'
        data['runner_enabled'] = bool(self.runner_enabled)
        data['sandbox_profile'] = self.sandbox_profile or 'standard'
        data['sandbox_policy'] = self.sandbox_policy or {'network_mode': 'whitelist', 'allowed_domains': []}
        data['temperature'] = float(self.temperature) if self.temperature is not None else None
        data['top_p'] = float(self.top_p) if self.top_p is not None else None
        data['notification_channels'] = self.notification_channels or {}

        if include_stats:
            now = datetime.utcnow()
            from .agent_core import HUMAN_BLOCKING_ASSIGNMENT_STATES, LEASED_EXECUTION_STATES, TaskAssignment
            data['stats'] = {
                'active_assignments': self.assignments.filter(
                    db.or_(
                        db.and_(
                            TaskAssignment.state.in_(LEASED_EXECUTION_STATES),
                            db.or_(
                                TaskAssignment.lease_expires_at.is_(None),
                                TaskAssignment.lease_expires_at >= now,
                            ),
                        ),
                        TaskAssignment.state.in_(HUMAN_BLOCKING_ASSIGNMENT_STATES),
                    ),
                ).count(),
                'total_runs': self.runs.count(),
            }

        return data

    def heartbeat(self):
        """记录心跳；离线状态的 Agent 恢复为活跃。"""
        self.last_seen_at = datetime.utcnow()
        if self.status == AgentStatus.OFFLINE:
            self.status = AgentStatus.ACTIVE
        db.session.add(self)

    def adapt_capabilities_from_experiences(self):
        """根据经验记录自动给出能力增删建议。

        - 未列入能力域的成功模式 → 建议新增
        - 持续失败的能力 → 建议移除
        返回待审核的变更建议 dict。
        """
        from .experience_reputation import AgentExperience

        current_caps = set(self.capabilities or [])

        success_experiences = AgentExperience.query.filter_by(
            agent_id=self.id,
            experience_type="success_pattern",
            is_valid=True,
        ).filter(
            AgentExperience.confidence >= 0.7,
        ).all()

        failure_experiences = AgentExperience.query.filter_by(
            agent_id=self.id,
            experience_type="failure_pattern",
            is_valid=True,
        ).filter(
            AgentExperience.confidence >= 0.5,
        ).all()

        suggested_additions = {}
        suggested_removals = {}

        for exp in success_experiences:
            domain = exp.domain
            caps_used = set(exp.capabilities_used or [])
            if domain and domain not in current_caps:
                domain_success_count = AgentExperience.query.filter_by(
                    agent_id=self.id,
                    experience_type="success_pattern",
                    domain=domain,
                    is_valid=True,
                ).filter(
                    AgentExperience.confidence >= 0.6,
                ).count()
                if domain_success_count >= 2:  # 至少 2 次成功才建议
                    suggested_additions[domain] = {
                        "reason": f"{domain_success_count} successful experiences in '{domain}' domain",
                        "confidence": min(0.9, domain_success_count * 0.15 + 0.5),
                        "source_experience_ids": [exp.id],
                    }

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
        """应用能力变更建议。

        Args:
            additions: 要新增的能力名列表
            removals: 要移除的能力名列表
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


# ── 兼容再导出（历史兼容层，勿在此添加新模型）──────────────────────────
# models/agent.py 原为 47 个类的单文件，2026-07 拆分为多个子模块；
# 大量调用点仍使用 `from models.agent import X` 的旧路径，此处统一转发。
from .agent_core import (  # noqa: E402
    TaskAssignment,
    TaskAssignmentState,
    RunLog,
    AgentRunStatus,
    LEASED_EXECUTION_STATES,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
    ACTIVE_ASSIGNMENT_STATES,
    AGENT_OFFLINE_AFTER_SECONDS,
    has_live_agent_assignment,
    mark_stale_agents_offline,
)
from .agent_run import AgentRun, AgentRunState  # noqa: E402,F401
from .task_collab import TaskTemplate, TaskEvent, Notification, SharedContext  # noqa: E402
from .workflow import (  # noqa: E402
    Workflow,
    WorkflowStatus,
    StepStatus,
    WorkflowStep,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTrigger,
    WorkflowVersion,
)
from .audit_project import AuditLog, ProjectRole  # noqa: E402
from .project_member import ProjectMember  # noqa: E402
from .channels import (  # noqa: E402
    AgentChannel,
    AgentChannelMember,
    AgentChannelMessage,
    CollaborationTemplate,
    KnowledgeEntry,
)
from .protocols import (  # noqa: E402
    CollaborationProtocol,
    ProtocolMessage,
    ProtocolType,
    ProtocolStatus,
)
from .experience_reputation import AgentExperience, AgentReputation, CrossProjectAgent  # noqa: E402
from .sandbox import (  # noqa: E402
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxLevel,
    SandboxViolationType,
    SandboxExecutionStatus,
)
from .conflicts import (  # noqa: E402
    AgentConflict,
    OrchestrationRun,
    ConflictType,
    ConflictSeverity,
    ConflictStatus,
    ConflictResolutionStrategy,
)
