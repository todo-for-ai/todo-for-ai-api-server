"""
Agent 角色模板模型

预定义的角色模板，用户可以直接使用或基于其创建自定义 Agent
"""

import enum
from datetime import datetime

from sqlalchemy import Column, String, Text, Enum, Integer, ForeignKey, JSON, Boolean, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel


class AgentRoleTemplateStatus(enum.Enum):
    """角色模板状态"""

    ACTIVE = 'active'
    INACTIVE = 'inactive'
    DEPRECATED = 'deprecated'


class AgentRoleTemplate(BaseModel):
    """
    Agent 角色模板

    内置模板：workspace_id = NULL, is_builtin = True
    自定义模板：workspace_id = 组织ID, is_builtin = False
    """

    __tablename__ = 'agent_role_templates'

    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True,
                          comment='所属工作区ID，NULL表示全局内置模板')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True,
                                comment='创建者用户ID')

    # 基本信息
    name = Column(String(64), nullable=False, comment='模板标识名（英文，用于代码引用）')
    display_name = Column(String(128), nullable=False, comment='展示名称')
    description = Column(Text, comment='模板描述')
    avatar_url = Column(String(512), comment='默认头像URL')
    category = Column(String(32), default='general', comment='分类：developer, qa, pm, designer, analyst, writer, architect, custom')

    # 核心配置
    capability_tags = Column(JSON, comment='能力标签列表')
    system_prompt = Column(Text, comment='系统提示词模板')
    soul_markdown = Column(Text, comment='SOUL.md 内容模板')

    # 策略配置
    response_style = Column(JSON, comment='响应风格配置')
    tool_policy = Column(JSON, comment='工具策略配置')
    memory_policy = Column(JSON, comment='记忆策略配置')
    handoff_policy = Column(JSON, comment='移交策略配置')

    # LLM 默认配置
    llm_provider = Column(String(64), comment='默认LLM供应商')
    llm_model = Column(String(128), comment='默认LLM模型')
    temperature = Column(String(10), comment='默认温度')
    reasoning_mode = Column(String(32), default='balanced', comment='推理模式')

    # 元数据
    is_builtin = Column(Boolean, default=False, nullable=False, comment='是否为内置模板')
    published_to_marketplace = Column(Boolean, default=False, nullable=False,
                                      comment='是否发布到数字员工市场（跨工作区可安装）')
    published_at = Column(DateTime, comment='发布时间')
    status = Column(Enum(AgentRoleTemplateStatus), default=AgentRoleTemplateStatus.ACTIVE,
                    nullable=False, comment='状态')
    usage_count = Column(Integer, default=0, comment='使用次数')
    parent_template_id = Column(Integer, ForeignKey('agent_role_templates.id'), nullable=True,
                                comment='父模板ID（用于自定义模板继承）')

    # 关系
    workspace = relationship('Organization', foreign_keys=[workspace_id], backref='agent_role_templates')
    creator = relationship('User', foreign_keys=[created_by_user_id])
    parent_template = relationship('AgentRoleTemplate', remote_side='AgentRoleTemplate.id', backref='child_templates')

    def to_dict(self):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        data['capability_tags'] = self.capability_tags or []
        data['response_style'] = self.response_style or {}
        data['tool_policy'] = self.tool_policy or {}
        data['memory_policy'] = self.memory_policy or {}
        data['handoff_policy'] = self.handoff_policy or {}
        data['is_builtin'] = bool(self.is_builtin)
        return data
