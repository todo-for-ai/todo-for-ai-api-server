"""数字员工市场端点（Phase 4：Agent 市场与角色模板市场）

- GET  /marketplace/digital-employees                     市场列表（登录即可浏览）
- GET  /marketplace/digital-employees/<template_id>       市场详情
- POST /marketplace/digital-employees/<template_id>/install  安装到工作区（管理权限 + 审计）
- POST /workspaces/<ws>/agent-role-templates/<id>/publish    发布自有模板到市场
- POST /workspaces/<ws>/agent-role-templates/<id>/unpublish  下架
"""

from flask import Blueprint, request

from models import AgentRoleTemplate, AgentRoleTemplateStatus, db
from core.auth import get_current_user, unified_auth_required
from .agent_common import ensure_workspace_manage_access, get_workspace_or_404, write_agent_audit
from .base import ApiResponse, validate_json_request
from services.marketplace import (
    get_market_template,
    install_to_workspace,
    list_market_templates,
    publish_template,
    unpublish_template,
)

marketplace_bp = Blueprint('marketplace', __name__)


def _get_workspace_template_or_404(workspace_id: int, template_id: int):
    template = AgentRoleTemplate.query.filter_by(
        id=template_id, workspace_id=workspace_id,
    ).first()
    if not template:
        return None, ApiResponse.not_found('Template not found in this workspace').to_response()
    return template, None


@marketplace_bp.route('/marketplace/digital-employees', methods=['GET'])
@unified_auth_required
def list_market():
    """市场列表：内置 + 已发布的 ACTIVE 模板（分类/搜索过滤）。"""
    get_current_user()

    try:
        page = max(int(request.args.get('page', 1)), 1)
        per_page = min(max(int(request.args.get('per_page', 20)), 1), 100)
    except (TypeError, ValueError):
        page, per_page = 1, 20
    category = request.args.get('category') or None
    search = request.args.get('search') or None

    items, pagination = list_market_templates(
        category=category, search=search, page=page, per_page=per_page,
    )
    return ApiResponse.success(data={
        'items': [item.to_dict() for item in items],
        'pagination': pagination,
    }).to_response()


@marketplace_bp.route('/marketplace/digital-employees/<int:template_id>', methods=['GET'])
@unified_auth_required
def market_detail(template_id: int):
    get_current_user()
    template = get_market_template(template_id)
    if not template:
        return ApiResponse.not_found('Marketplace template not found').to_response()
    return ApiResponse.success(data=template.to_dict()).to_response()


@marketplace_bp.route('/marketplace/digital-employees/<int:template_id>/install', methods=['POST'])
@unified_auth_required
def install(template_id: int):
    """安装数字员工到工作区：复制为工作区自有模板（幂等），可选实例化 Agent。"""
    user = get_current_user()
    data = validate_json_request(
        required_fields=['workspace_id'],
        optional_fields=['create_agent', 'agent_name'],
    )
    if isinstance(data, tuple):
        return data
    workspace, not_found = get_workspace_or_404(int(data['workspace_id']))
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    template = get_market_template(template_id)
    if not template:
        return ApiResponse.not_found('Marketplace template not found').to_response()

    create_agent = bool(data.get('create_agent'))
    agent_name = data.get('agent_name')

    try:
        result = install_to_workspace(
            template, workspace.id, user,
            create_agent=create_agent, agent_name=agent_name,
        )
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()

    write_agent_audit(
        event_type='marketplace.employee_installed',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_role_template',
        target_id=result['template'].id,
        workspace_id=workspace.id,
        payload={
            'source_template_id': template.id,
            'source_template_name': template.name,
            'source_workspace_id': template.workspace_id,
            'is_builtin': bool(template.is_builtin),
            'created': result['created'],
            'agent_id': result['agent'].id if result['agent'] else None,
        },
        risk_score=10,
    )
    return ApiResponse.success(
        data={
            'template': result['template'].to_dict(),
            'created': result['created'],
            'agent': result['agent'].to_dict() if result['agent'] else None,
        },
        message='Installed' if result['created'] else 'Already installed',
    ).to_response()


@marketplace_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/<int:template_id>/publish', methods=['POST'])
@unified_auth_required
def publish(workspace_id: int, template_id: int):
    """发布工作区自有模板到数字员工市场。"""
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    template, not_found = _get_workspace_template_or_404(workspace_id, template_id)
    if not_found:
        return not_found
    if template.status != AgentRoleTemplateStatus.ACTIVE:
        return ApiResponse.error('only ACTIVE templates can be published', 400).to_response()

    publish_template(template)
    write_agent_audit(
        event_type='marketplace.employee_published',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_role_template',
        target_id=template.id,
        workspace_id=workspace_id,
        payload={'template_name': template.name},
        risk_score=10,
    )
    return ApiResponse.success(data=template.to_dict(), message='Published to marketplace').to_response()


@marketplace_bp.route('/workspaces/<int:workspace_id>/agent-role-templates/<int:template_id>/unpublish', methods=['POST'])
@unified_auth_required
def unpublish(workspace_id: int, template_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    template, not_found = _get_workspace_template_or_404(workspace_id, template_id)
    if not_found:
        return not_found

    unpublish_template(template)
    write_agent_audit(
        event_type='marketplace.employee_unpublished',
        actor_type='user',
        actor_id=user.id,
        target_type='agent_role_template',
        target_id=template.id,
        workspace_id=workspace_id,
        payload={'template_name': template.name},
        risk_score=5,
    )
    return ApiResponse.success(data=template.to_dict(), message='Unpublished').to_response()
