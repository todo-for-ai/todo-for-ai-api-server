"""
LLM API 调用指标查询 API —— 用户级 / 组织级 / 单 Agent 三个视角。

- GET /llm-metrics/mine                        当前用户名下 Agent 的调用聚合
- GET /workspaces/<wid>/llm-metrics            组织内全部 Agent 的调用聚合（成员可见）
- GET /llm-metrics/agents/<agent_id>           单 Agent 的调用聚合（管理权限）
"""

from flask import Blueprint, request

from core.auth import unified_auth_required, get_current_user
from models import Agent, Organization
from .agent_common import (
    ensure_agent_manage_access,
    ensure_workspace_access,
    get_workspace_or_404,
)
from .base import ApiResponse
from services.llm_metrics import (
    agent_scope_query,
    parse_window_hours,
    summarize,
    user_scope_query,
    workspace_scope_query,
)

llm_metrics_bp = Blueprint('llm_metrics', __name__)


@llm_metrics_bp.route('/llm-metrics/mine', methods=['GET'])
@unified_auth_required
def my_llm_metrics():
    """当前用户名下（owner 或 creator）Agent 的 LLM 调用聚合。"""
    user = get_current_user()
    hours = parse_window_hours(request.args)
    return ApiResponse.success(
        summarize(user_scope_query(user.id), hours),
        'My LLM call metrics retrieved',
    ).to_response()


@llm_metrics_bp.route('/workspaces/<int:workspace_id>/llm-metrics', methods=['GET'])
@unified_auth_required
def workspace_llm_metrics(workspace_id):
    """组织（工作区）内全部 Agent 的 LLM 调用聚合，组织成员可见。"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err
    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    hours = parse_window_hours(request.args)
    return ApiResponse.success(
        summarize(workspace_scope_query(workspace_id), hours),
        'Workspace LLM call metrics retrieved',
    ).to_response()


@llm_metrics_bp.route('/llm-metrics/agents/<int:agent_id>', methods=['GET'])
@unified_auth_required
def agent_llm_metrics(agent_id):
    """单 Agent 的 LLM 调用聚合（需对该 Agent 有管理权限）。"""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id).first()
    if not agent:
        return ApiResponse.error('Agent not found', 404).to_response()
    manage_err = ensure_agent_manage_access(user, agent)
    if manage_err:
        return manage_err

    hours = parse_window_hours(request.args)
    return ApiResponse.success(
        summarize(agent_scope_query(agent_id), hours),
        'Agent LLM call metrics retrieved',
    ).to_response()
