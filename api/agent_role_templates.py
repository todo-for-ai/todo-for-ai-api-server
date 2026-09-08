"""
Agent 角色模板管理 API

提供预定义角色模板的 CRUD 和实例化功能
"""

from flask import Blueprint, request, g
from sqlalchemy import func, or_

from models import (
    db, AgentRoleTemplate, AgentRoleTemplateStatus,
    Agent, AgentStatus, Organization
)
from core.auth import unified_auth_required, get_current_user
from api.agent_common import (
    get_workspace_or_404, ensure_workspace_access,
    write_agent_audit
)
from api.base import ApiResponse, validate_json_request, get_request_args


agent_role_templates_bp = Blueprint('agent_role_templates', __name__)


TEMPLATE_EDITABLE_FIELDS = [
    'display_name', 'description', 'avatar_url', 'category',
    'capability_tags', 'system_prompt', 'soul_markdown',
    'response_style', 'tool_policy', 'memory_policy', 'handoff_policy',
    'llm_provider', 'llm_model', 'temperature', 'reasoning_mode'
]


def _filter_editable_fields(data):
    """过滤可编辑字段"""
    return {k: v for k, v in data.items() if k in TEMPLATE_EDITABLE_FIELDS}


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/industries', methods=['GET'])
@unified_auth_required
def list_template_industries(workspace_id):
    """行业清单（含各行业模板数量），供岗位选择器按行业浏览。"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    rows = (
        db.session.query(
            AgentRoleTemplate.industry,
            func.count(AgentRoleTemplate.id),
        )
        .filter(
            AgentRoleTemplate.status == AgentRoleTemplateStatus.ACTIVE,
            AgentRoleTemplate.is_builtin == True,
            AgentRoleTemplate.industry.isnot(None),
        )
        .group_by(AgentRoleTemplate.industry)
        .order_by(func.count(AgentRoleTemplate.id).desc())
        .all()
    )
    return ApiResponse.success(
        data={
            'industries': [
                {'industry': name, 'count': count} for name, count in rows
            ],
            'total': sum(count for _, count in rows),
        },
        message='Template industries retrieved',
    ).to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates', methods=['GET'])
@unified_auth_required
def list_templates(workspace_id):
    """列出角色模板（包含内置和自定义）"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    args = get_request_args()
    category = request.args.get('category')
    include_builtin = request.args.get('include_builtin', 'true').lower() == 'true'

    query = AgentRoleTemplate.query.filter(
        or_(
            AgentRoleTemplate.workspace_id == workspace_id,
            AgentRoleTemplate.is_builtin == True
        ) if include_builtin else
        (AgentRoleTemplate.workspace_id == workspace_id)
    )

    if category:
        query = query.filter(AgentRoleTemplate.category == category)

    industry = request.args.get('industry')
    if industry:
        query = query.filter(AgentRoleTemplate.industry == industry)
    if request.args.get('keyword'):
        kw = request.args.get('keyword').strip()
        like = f"%{kw}%"
        query = query.filter(
            or_(
                AgentRoleTemplate.display_name.like(like),
                AgentRoleTemplate.description.like(like),
            )
        )

    query = query.filter(AgentRoleTemplate.status == AgentRoleTemplateStatus.ACTIVE)
    query = query.order_by(AgentRoleTemplate.is_builtin.desc(), AgentRoleTemplate.updated_at.desc())

    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return ApiResponse.success(
        {
            'items': [item.to_dict() for item in items],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'has_prev': page > 1,
                'has_next': page * per_page < total,
            },
        },
        'Templates retrieved successfully',
    ).to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/<int:template_id>', methods=['GET'])
@unified_auth_required
def get_template(workspace_id, template_id):
    """获取单个模板详情"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    template = AgentRoleTemplate.query.filter(
        db.or_(
            AgentRoleTemplate.workspace_id == workspace_id,
            AgentRoleTemplate.is_builtin == True
        ),
        AgentRoleTemplate.id == template_id
    ).first()

    if not template:
        return ApiResponse.not_found('Template not found').to_response()

    return ApiResponse.success(template.to_dict()).to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates', methods=['POST'])
@unified_auth_required
def create_template(workspace_id):
    """创建自定义模板（通常基于内置模板复制）"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    data = validate_json_request(
        required_fields=['name', 'display_name'],
        optional_fields=['parent_template_id'],
    )
    if isinstance(data, tuple):
        return data

    parent_template_id = data.get('parent_template_id')
    parent_template = None

    if parent_template_id:
        parent_template = AgentRoleTemplate.query.filter(
            db.or_(
                AgentRoleTemplate.workspace_id == workspace_id,
                AgentRoleTemplate.is_builtin == True
            ),
            AgentRoleTemplate.id == parent_template_id
        ).first()

        if not parent_template:
            return ApiResponse.error('Parent template not found', 400).to_response()

    # 检查名称是否已存在
    existing = AgentRoleTemplate.query.filter_by(
        workspace_id=workspace_id,
        name=data['name']
    ).first()
    if existing:
        return ApiResponse.error('Template with this name already exists', 409).to_response()

    template_data = _filter_editable_fields(data)

    if parent_template:
        # 从父模板复制默认值
        for field in TEMPLATE_EDITABLE_FIELDS:
            if field not in template_data:
                template_data[field] = getattr(parent_template, field)

    template = AgentRoleTemplate(
        workspace_id=workspace_id,
        created_by_user_id=user.id,
        name=data['name'],
        is_builtin=False,
        parent_template_id=parent_template_id,
        **template_data
    )

    db.session.add(template)
    db.session.commit()

    write_agent_audit(
        event_type='template.created',
        actor_type='user',
        actor_id=user.id,
        target_type='template',
        target_id=template.id,
        workspace_id=workspace_id,
        payload={
            'template_name': template.name,
            'parent_template_id': parent_template_id,
        }
    )

    return ApiResponse.created(template.to_dict(), 'Template created successfully').to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/<int:template_id>', methods=['PUT'])
@unified_auth_required
def update_template(workspace_id, template_id):
    """更新自定义模板（内置模板不可修改）"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    template = AgentRoleTemplate.query.filter_by(
        id=template_id,
        workspace_id=workspace_id
    ).first()

    if not template:
        return ApiResponse.not_found('Template not found').to_response()

    if template.is_builtin:
        return ApiResponse.error('Builtin templates cannot be modified', 403).to_response()

    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    template_data = _filter_editable_fields(data)
    for key, value in template_data.items():
        setattr(template, key, value)

    db.session.commit()

    write_agent_audit(
        event_type='template.updated',
        actor_type='user',
        actor_id=user.id,
        target_type='template',
        target_id=template.id,
        workspace_id=workspace_id,
        payload={'updated_fields': list(template_data.keys())}
    )

    return ApiResponse.success(template.to_dict(), 'Template updated successfully').to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/<int:template_id>', methods=['DELETE'])
@unified_auth_required
def delete_template(workspace_id, template_id):
    """删除自定义模板（内置模板不可删除）"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    template = AgentRoleTemplate.query.filter_by(
        id=template_id,
        workspace_id=workspace_id
    ).first()

    if not template:
        return ApiResponse.not_found('Template not found').to_response()

    if template.is_builtin:
        return ApiResponse.error('Builtin templates cannot be deleted', 403).to_response()

    template.status = AgentRoleTemplateStatus.DEPRECATED
    db.session.commit()

    write_agent_audit(
        event_type='template.deleted',
        actor_type='user',
        actor_id=user.id,
        target_type='template',
        target_id=template.id,
        workspace_id=workspace_id,
    )

    return ApiResponse.success(None, 'Template deleted successfully').to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/<int:template_id>/instantiate', methods=['POST'])
@unified_auth_required
def instantiate_template(workspace_id, template_id):
    """从模板创建 Agent"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    template = AgentRoleTemplate.query.filter(
        db.or_(
            AgentRoleTemplate.workspace_id == workspace_id,
            AgentRoleTemplate.is_builtin == True
        ),
        AgentRoleTemplate.id == template_id,
        AgentRoleTemplate.status == AgentRoleTemplateStatus.ACTIVE
    ).first()

    if not template:
        return ApiResponse.not_found('Template not found').to_response()

    data = validate_json_request(
        required_fields=['name'],
        optional_fields=[
            'display_name', 'description', 'avatar_url', 'capability_tags',
            'system_prompt', 'soul_markdown', 'response_style', 'tool_policy',
            'memory_policy', 'handoff_policy', 'llm_provider', 'llm_model',
            'temperature', 'reasoning_mode',
        ],
    )
    if isinstance(data, tuple):
        return data

    # 检查 Agent 名称是否已存在
    existing = Agent.query.filter_by(workspace_id=workspace_id, name=data['name']).first()
    if existing:
        return ApiResponse.error('Agent with this name already exists', 409).to_response()

    # 从模板创建 Agent
    agent = Agent(
        workspace_id=workspace_id,
        creator_user_id=user.id,
        name=data['name'],
        display_name=data.get('display_name') or template.display_name,
        description=data.get('description') or template.description,
        avatar_url=data.get('avatar_url') or template.avatar_url,
        capability_tags=data.get('capability_tags') or template.capability_tags,
        system_prompt=data.get('system_prompt') or template.system_prompt,
        soul_markdown=data.get('soul_markdown') or template.soul_markdown,
        response_style=data.get('response_style') or template.response_style,
        tool_policy=data.get('tool_policy') or template.tool_policy,
        memory_policy=data.get('memory_policy') or template.memory_policy,
        handoff_policy=data.get('handoff_policy') or template.handoff_policy,
        llm_provider=data.get('llm_provider') or template.llm_provider,
        llm_model=data.get('llm_model') or template.llm_model,
        temperature=data.get('temperature') or template.temperature,
        reasoning_mode=data.get('reasoning_mode') or template.reasoning_mode,
        status=AgentStatus.ACTIVE,
    )

    db.session.add(agent)

    # 更新模板使用次数
    template.usage_count = (template.usage_count or 0) + 1

    db.session.commit()

    write_agent_audit(
        event_type='agent.instantiated_from_template',
        actor_type='user',
        actor_id=user.id,
        target_type='agent',
        target_id=agent.id,
        workspace_id=workspace_id,
        payload={
            'template_id': template.id,
            'template_name': template.name,
            'is_builtin': template.is_builtin,
        }
    )

    return ApiResponse.created(agent.to_dict(), 'Agent created from template successfully').to_response()


@agent_role_templates_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/categories', methods=['GET'])
@unified_auth_required
def list_categories(workspace_id):
    """列出所有可用的模板分类"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    categories = [
        {'value': 'developer', 'label': 'Developer', 'description': '专注于代码实现和工程化'},
        {'value': 'reviewer', 'label': 'Reviewer', 'description': '代码审查和质量保证'},
        {'value': 'architect', 'label': 'Architect', 'description': '系统架构和技术决策'},
        {'value': 'qa', 'label': 'QA', 'description': '测试和质量保证'},
        {'value': 'pm', 'label': 'Product Manager', 'description': '产品管理和需求分析'},
        {'value': 'designer', 'label': 'Designer', 'description': '用户体验和界面设计'},
        {'value': 'analyst', 'label': 'Analyst', 'description': '数据分析和研究'},
        {'value': 'writer', 'label': 'Writer', 'description': '文档编写和内容创作'},
        {'value': 'general', 'label': 'General', 'description': '通用型角色'},
        {'value': 'custom', 'label': 'Custom', 'description': '自定义角色'},
    ]

    return ApiResponse.success({'items': categories}).to_response()
