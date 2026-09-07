"""
Agent Runtime Management API

管理 Agent 云端运行时的 API 端点。
路由层只做参数解析、鉴权与 HTTP 语义映射；业务流程在
services/cloud_runtime/management.py。
"""

from flask import Blueprint, request
from models import db, Agent
from core.auth import unified_auth_required, get_current_user
from api.base import ApiResponse
from api.agent_common import ensure_agent_manage_access, ensure_workspace_access
from services.cloud_runtime import management

agent_runtime_mgmt_bp = Blueprint('agent_runtime_mgmt', __name__)


def _agent_or_error(workspace_id, agent_id):
    """按工作区取 Agent 并校验管理权限；出错时返回 (None, response)。"""
    agent = Agent.query.filter_by(
        id=agent_id,
        workspace_id=workspace_id
    ).first()

    if not agent:
        return None, ApiResponse.not_found('Agent not found').to_response()

    err = ensure_agent_manage_access(get_current_user(), agent)
    if err:
        return None, err
    return agent, None


def _workspace_or_error(workspace_id):
    """取工作区并校验访问权限；出错时返回 (None, response)。"""
    from models import Organization
    workspace = db.session.get(Organization, workspace_id)
    if not workspace:
        return None, ApiResponse.not_found('Workspace not found').to_response()
    err = ensure_workspace_access(get_current_user(), workspace)
    if err:
        return None, err
    return workspace, None


def _management_error_response(err):
    details = getattr(err, 'existing', None)
    kwargs = {'error_details': {'existing': details}} if details is not None else {}
    return ApiResponse.error(str(err), err.status_code, **kwargs).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/agents/<int:agent_id>/runtime/spawn',
    methods=['POST']
)
@unified_auth_required
def spawn_agent_runtime(workspace_id, agent_id):
    """启动 Agent 云端运行时：在 K8s 中创建隔离的 Agent Pod。"""
    agent, err = _agent_or_error(workspace_id, agent_id)
    if err:
        return err

    try:
        data = management.spawn_runtime(
            agent,
            workspace_id,
            get_current_user().id,
            request.get_json(silent=True),
        )
    except management.RuntimeManagementError as e:
        return _management_error_response(e)

    return ApiResponse.success(
        data, 'Agent runtime spawned successfully').to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/agents/<int:agent_id>/runtime/terminate',
    methods=['POST']
)
@unified_auth_required
def terminate_agent_runtime(workspace_id, agent_id):
    """终止 Agent 云端运行时：删除 K8s 中的 Agent Pod。"""
    agent, err = _agent_or_error(workspace_id, agent_id)
    if err:
        return err

    try:
        data = management.terminate_runtime(agent)
    except management.RuntimeManagementError as e:
        return _management_error_response(e)

    return ApiResponse.success(data, 'Agent runtime terminated').to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/agents/<int:agent_id>/runtime/status',
    methods=['GET']
)
@unified_auth_required
def get_agent_runtime_status(workspace_id, agent_id):
    """获取 Agent 运行时状态（只读权限即可）。"""
    agent, err = _agent_or_error(workspace_id, agent_id)
    if err:
        return err

    return ApiResponse.success(management.runtime_status(agent)).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/runtime/pods',
    methods=['GET']
)
@unified_auth_required
def list_runtime_pods(workspace_id):
    """列出工作区下的所有 Agent 运行时 Pod。"""
    _, err = _workspace_or_error(workspace_id)
    if err:
        return err

    return ApiResponse.success(
        management.list_runtime_pods(workspace_id)).to_response()


# ── 工作区运行时配额与回收策略（Phase 2）──

@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/runtime/settings',
    methods=['GET']
)
@unified_auth_required
def get_workspace_runtime_settings(workspace_id):
    """查看工作区运行时配额（在岗 Pod 上限 / 空闲回收阈值）。"""
    _, err = _workspace_or_error(workspace_id)
    if err:
        return err

    return ApiResponse.success(
        management.get_runtime_settings(workspace_id)).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/runtime/settings',
    methods=['PUT']
)
@unified_auth_required
def update_workspace_runtime_settings(workspace_id):
    """设置工作区运行时配额：max_pods / idle_timeout_minutes（0=不限/不回收）。"""
    _, err = _workspace_or_error(workspace_id)
    if err:
        return err

    try:
        data = management.update_runtime_settings(
            workspace_id, request.get_json(silent=True))
    except management.SettingsValidationError as e:
        return ApiResponse.error(str(e), 400).to_response()

    return ApiResponse.success(data).to_response()
