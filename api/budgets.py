"""预算/配额管理端点（P2.2）

工作区维度的 Budget CRUD 与用量查询：
- GET    /workspaces/<id>/budgets              列表（含每条预算的当前用量）
- POST   /workspaces/<id>/budgets              新建（scope/resource/period 校验 + 唯一约束）
- PUT    /workspaces/<id>/budgets/<bid>        更新上限 / 启停
- DELETE /workspaces/<id>/budgets/<bid>        删除
- GET    /workspaces/<id>/budgets/<bid>/usage  单条用量

查看需工作区成员，写操作需 owner/admin（ensure_workspace_manage_access）。
"""

from flask import Blueprint, request
from sqlalchemy.exc import IntegrityError

from models import Agent, Budget, Project, db
from core.auth import get_current_user, unified_auth_required
from .agent_common import (
    ensure_workspace_access,
    ensure_workspace_manage_access,
    get_workspace_or_404,
    write_agent_audit,
)
from .base import ApiResponse, validate_json_request

budgets_bp = Blueprint('budgets', __name__)


def _serialize_budget(budget, include_usage=True):
    data = budget.to_dict()
    if include_usage:
        from services.budget_service import get_usage

        usage = get_usage(budget)
        data['usage'] = usage
        data['usage_ratio'] = (
            round(usage['used'] / budget.limit_value, 4)
            if budget.limit_value and not usage.get('not_tracked') else None
        )
    return data


def _validate_scope_refs(data, workspace_id):
    """校验 scope 与 agent_id/project_id 的配套关系及归属。"""
    scope_type = data.get('scope_type')
    agent_id = data.get('agent_id')
    project_id = data.get('project_id')

    if scope_type == 'agent':
        if not agent_id:
            return None, 'agent_id is required for scope_type=agent'
        agent = db.session.get(Agent, int(agent_id))
        if not agent or agent.workspace_id != workspace_id:
            return None, 'agent not found in this workspace'
    elif scope_type == 'project':
        if not project_id:
            return None, 'project_id is required for scope_type=project'
        project = db.session.get(Project, int(project_id))
        if not project or project.organization_id != workspace_id:
            return None, 'project not found in this workspace'
    elif scope_type == 'workspace':
        if agent_id or project_id:
            return None, 'workspace scope must not carry agent_id/project_id'
    return {'agent_id': agent_id, 'project_id': project_id}, None


def _get_budget_or_404(workspace_id, budget_id):
    budget = Budget.query.filter_by(id=budget_id, workspace_id=workspace_id).first()
    if not budget:
        return None, ApiResponse.not_found('Budget not found').to_response()
    return budget, None


@budgets_bp.route('/workspaces/<int:workspace_id>/budgets', methods=['GET'])
@unified_auth_required
def list_budgets(workspace_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    scope = request.args.get('scope_type')
    query = Budget.query.filter_by(workspace_id=workspace_id)
    if scope:
        query = query.filter(Budget.scope_type == scope)
    budgets = query.order_by(Budget.id.desc()).all()
    return ApiResponse.success(data={
        'budgets': [_serialize_budget(b) for b in budgets],
    }).to_response()


@budgets_bp.route('/workspaces/<int:workspace_id>/budgets', methods=['POST'])
@unified_auth_required
def create_budget(workspace_id: int):
    user = get_current_user()
    data = validate_json_request(
        required_fields=['scope_type', 'resource', 'limit_value'],
        optional_fields=['period', 'agent_id', 'project_id', 'is_active'],
    )
    if isinstance(data, tuple):
        return data
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = request.get_json()
    scope_type = data.get('scope_type')
    resource = data.get('resource')
    period = data.get('period', 'total')

    if scope_type not in Budget.SCOPE_TYPES:
        return ApiResponse.error(f'invalid scope_type, one of {Budget.SCOPE_TYPES}', 400).to_response()
    if resource not in Budget.RESOURCES:
        return ApiResponse.error(f'invalid resource, one of {Budget.RESOURCES}', 400).to_response()
    if period not in Budget.PERIODS:
        return ApiResponse.error(f'invalid period, one of {Budget.PERIODS}', 400).to_response()

    try:
        limit_value = int(data.get('limit_value'))
    except (TypeError, ValueError):
        return ApiResponse.error('limit_value must be a positive integer', 400).to_response()
    if limit_value <= 0:
        return ApiResponse.error('limit_value must be a positive integer', 400).to_response()

    refs, scope_err = _validate_scope_refs(data, workspace_id)
    if scope_err:
        return ApiResponse.error(scope_err, 400).to_response()

    # 含 NULL 列的组合唯一约束在 SQLite/MySQL 下不生效（NULL != NULL），应用层查重
    existing = Budget.query.filter_by(
        scope_type=scope_type,
        workspace_id=workspace_id,
        resource=resource,
        period=period,
    ).filter(
        Budget.agent_id.is_(refs['agent_id']) if refs['agent_id'] is None else Budget.agent_id == refs['agent_id'],
        Budget.project_id.is_(refs['project_id']) if refs['project_id'] is None else Budget.project_id == refs['project_id'],
    ).first()
    if existing:
        return ApiResponse.error(
            'a budget for this scope/resource/period already exists', 409
        ).to_response()

    budget = Budget(
        scope_type=scope_type,
        agent_id=refs['agent_id'],
        project_id=refs['project_id'],
        workspace_id=workspace_id,
        resource=resource,
        limit_value=limit_value,
        period=period,
        is_active=bool(data.get('is_active', True)),
    )
    db.session.add(budget)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return ApiResponse.error(
            'a budget for this scope/resource/period already exists', 409
        ).to_response()

    write_agent_audit(
        event_type='budget.created',
        actor_type='user',
        actor_id=user.id,
        target_type='budget',
        target_id=budget.id,
        workspace_id=workspace_id,
        payload=_serialize_budget(budget, include_usage=False),
        risk_score=10,
    )
    return ApiResponse.success(data=_serialize_budget(budget), message='Budget created').to_response()


@budgets_bp.route('/workspaces/<int:workspace_id>/budgets/<int:budget_id>', methods=['PUT'])
@unified_auth_required
def update_budget(workspace_id: int, budget_id: int):
    user = get_current_user()
    data = validate_json_request(optional_fields=['limit_value', 'is_active'])
    if isinstance(data, tuple):
        return data
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    budget, not_found = _get_budget_or_404(workspace_id, budget_id)
    if not_found:
        return not_found

    data = request.get_json()
    if 'limit_value' in data:
        try:
            limit_value = int(data['limit_value'])
        except (TypeError, ValueError):
            return ApiResponse.error('limit_value must be a positive integer', 400).to_response()
        if limit_value <= 0:
            return ApiResponse.error('limit_value must be a positive integer', 400).to_response()
        budget.limit_value = limit_value
    if 'is_active' in data:
        budget.is_active = bool(data['is_active'])

    db.session.commit()
    write_agent_audit(
        event_type='budget.updated',
        actor_type='user',
        actor_id=user.id,
        target_type='budget',
        target_id=budget.id,
        workspace_id=workspace_id,
        payload={k: data[k] for k in ('limit_value', 'is_active') if k in data},
        risk_score=10,
    )
    return ApiResponse.success(data=_serialize_budget(budget), message='Budget updated').to_response()


@budgets_bp.route('/workspaces/<int:workspace_id>/budgets/<int:budget_id>', methods=['DELETE'])
@unified_auth_required
def delete_budget(workspace_id: int, budget_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    budget, not_found = _get_budget_or_404(workspace_id, budget_id)
    if not_found:
        return not_found

    db.session.delete(budget)
    db.session.commit()
    write_agent_audit(
        event_type='budget.deleted',
        actor_type='user',
        actor_id=user.id,
        target_type='budget',
        target_id=budget_id,
        workspace_id=workspace_id,
        payload={'scope_type': budget.scope_type, 'resource': budget.resource, 'period': budget.period},
        risk_score=15,
    )
    return ApiResponse.success(message='Budget deleted').to_response()


@budgets_bp.route('/workspaces/<int:workspace_id>/budgets/<int:budget_id>/usage', methods=['GET'])
@unified_auth_required
def budget_usage(workspace_id: int, budget_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err
    budget, not_found = _get_budget_or_404(workspace_id, budget_id)
    if not_found:
        return not_found

    from services.budget_service import check_budgets

    data = _serialize_budget(budget)
    violations = check_budgets(workspace_id, agent_id=budget.agent_id, project_id=budget.project_id)
    data['violating'] = any(v.get('budget_id') == budget.id for v in violations)
    return ApiResponse.success(data=data).to_response()
