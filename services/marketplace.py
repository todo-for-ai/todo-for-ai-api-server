"""数字员工市场服务（Phase 4：Agent 市场与角色模板市场）

把 AgentRoleTemplate 升级为可发布的「数字员工」：
- 发布：工作区把自有的角色模板发布到市场（published_to_marketplace），
  内置模板天然在架；
- 浏览：市场列表 = 内置模板 + 全部已发布模板（ACTIVE）；
- 安装：把市场模板复制为目标工作区的自有模板（parent_template_id 指向
  市场源，幂等：重复安装返回既有副本），源模板 usage_count 累计，
  写安装审计；可选同时按模板实例化 Agent（复用 instantiate 字段映射）。
"""

from datetime import datetime

import structlog

from models import (
    Agent,
    AgentRoleTemplate,
    AgentRoleTemplateStatus,
    AgentStatus,
    db,
)

logger = structlog.get_logger()

# 市场安装时复制到工作区副本的模板字段
_COPY_FIELDS = (
    'display_name', 'description', 'avatar_url', 'category',
    'capability_tags', 'system_prompt', 'soul_markdown',
    'response_style', 'tool_policy', 'memory_policy', 'handoff_policy',
    'llm_provider', 'llm_model', 'temperature', 'reasoning_mode',
)


def list_market_templates(category=None, search=None, page=1, per_page=20):
    """市场列表：内置 + 已发布的 ACTIVE 模板。返回 (items, pagination)。"""
    query = AgentRoleTemplate.query.filter(
        db.or_(
            AgentRoleTemplate.is_builtin.is_(True),
            AgentRoleTemplate.published_to_marketplace.is_(True),
        ),
        AgentRoleTemplate.status == AgentRoleTemplateStatus.ACTIVE,
    )
    if category:
        query = query.filter(AgentRoleTemplate.category == category)
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(
            AgentRoleTemplate.name.ilike(like),
            AgentRoleTemplate.display_name.ilike(like),
            AgentRoleTemplate.description.ilike(like),
        ))
    query = query.order_by(AgentRoleTemplate.usage_count.desc(), AgentRoleTemplate.id.desc())

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    pagination = {
        'page': page, 'per_page': per_page, 'total': total,
        'has_prev': page > 1, 'has_next': page * per_page < total,
    }
    return items, pagination


def get_market_template(template_id: int):
    """市场详情：可安装 = 内置或已发布且 ACTIVE。"""
    return AgentRoleTemplate.query.filter(
        db.or_(
            AgentRoleTemplate.is_builtin.is_(True),
            AgentRoleTemplate.published_to_marketplace.is_(True),
        ),
        AgentRoleTemplate.id == template_id,
        AgentRoleTemplate.status == AgentRoleTemplateStatus.ACTIVE,
    ).first()


def publish_template(template) -> None:
    """把工作区自有模板发布到市场。"""
    template.published_to_marketplace = True
    template.published_at = datetime.utcnow()
    db.session.commit()
    logger.info("marketplace.published", template_id=template.id)


def unpublish_template(template) -> None:
    """下架（不影响已安装到其他工作区的副本）。"""
    template.published_to_marketplace = False
    template.published_at = None
    db.session.commit()
    logger.info("marketplace.unpublished", template_id=template.id)


def install_to_workspace(template, workspace_id: int, user,
                         create_agent: bool = False, agent_name: str = None) -> dict:
    """安装市场「数字员工」到工作区。

    幂等：该工作区已有指向同一源模板的安装副本时，直接返回既有副本
    （created=False）。create_agent=True 时按副本字段实例化 Agent。
    """
    existing = AgentRoleTemplate.query.filter_by(
        workspace_id=workspace_id,
        parent_template_id=template.id,
    ).first()
    if existing:
        result = {'template': existing, 'created': False}

    else:
        copy = AgentRoleTemplate(
            workspace_id=workspace_id,
            created_by_user_id=user.id,
            name=template.name,
            parent_template_id=template.id,
            is_builtin=False,
            status=AgentRoleTemplateStatus.ACTIVE,
            **{field: getattr(template, field) for field in _COPY_FIELDS},
        )
        db.session.add(copy)
        template.usage_count = (template.usage_count or 0) + 1
        db.session.flush()
        result = {'template': copy, 'created': True}

    agent = None
    if create_agent:
        agent_name = (agent_name or '').strip()
        if not agent_name:
            raise ValueError('agent_name is required when create_agent=true')
        if Agent.query.filter_by(workspace_id=workspace_id, name=agent_name).first():
            raise ValueError('Agent with this name already exists')
        src = result['template']
        agent = Agent(
            workspace_id=workspace_id,
            creator_user_id=user.id,
            name=agent_name,
            display_name=src.display_name,
            description=src.description,
            avatar_url=src.avatar_url,
            capability_tags=src.capability_tags,
            system_prompt=src.system_prompt,
            soul_markdown=src.soul_markdown,
            response_style=src.response_style,
            tool_policy=src.tool_policy,
            memory_policy=src.memory_policy,
            handoff_policy=src.handoff_policy,
            llm_provider=src.llm_provider,
            llm_model=src.llm_model,
            temperature=src.temperature,
            reasoning_mode=src.reasoning_mode,
            status=AgentStatus.ACTIVE,
        )
        db.session.add(agent)
        src.usage_count = (src.usage_count or 0) + 1

    db.session.commit()
    result['agent'] = agent
    return result
