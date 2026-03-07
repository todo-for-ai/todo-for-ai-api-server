"""
Agent 模型
"""

import enum
from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, JSON, DECIMAL, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentStatus(enum.Enum):
    """Agent 状态"""

    ACTIVE = 'active'
    INACTIVE = 'inactive'
    REVOKED = 'revoked'


class Agent(BaseModel):
    """Workspace(organization) 下的 Agent"""

    __tablename__ = 'agents'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='所属工作区(组织)ID')
    creator_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, comment='创建者用户ID')

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

    workspace = relationship('Organization', foreign_keys=[workspace_id])
    creator = relationship('User', foreign_keys=[creator_user_id])
    keys = relationship('AgentKey', back_populates='agent', cascade='all, delete-orphan', lazy='dynamic')
    soul_versions = relationship('AgentSoulVersion', back_populates='agent', cascade='all, delete-orphan', lazy='dynamic')
    secrets = relationship('AgentSecret', back_populates='agent', cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        data['capability_tags'] = self.capability_tags or []
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
        return data
