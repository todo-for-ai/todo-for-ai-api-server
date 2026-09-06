"""
Agent Runtime Management API

管理 Agent 云端运行时的 API 端点
"""

from flask import Blueprint, request, g
from models import db, Agent
from models.agent_key import AgentKey
from core.auth import unified_auth_required, get_current_user
from services.agent_runtime_controller import get_agent_controller
from api.base import ApiResponse
from api.agent_common import ensure_agent_manage_access

agent_runtime_mgmt_bp = Blueprint('agent_runtime_mgmt', __name__)


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/agents/<int:agent_id>/runtime/spawn',
    methods=['POST']
)
@unified_auth_required
def spawn_agent_runtime(workspace_id, agent_id):
    """
    启动 Agent 云端运行时

    在 K8s 中创建隔离的 Agent Pod
    """
    user = get_current_user()

    # 获取 Agent
    agent = Agent.query.filter_by(
        id=agent_id,
        workspace_id=workspace_id
    ).first()

    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    # 检查权限
    err = ensure_agent_manage_access(user, agent)
    if err:
        return err

    # 检查是否已运行
    controller = get_agent_controller()
    existing_status = controller.get_agent_pod_status(agent_id)

    if existing_status and existing_status['phase'] in ['Running', 'Pending']:
        return ApiResponse.error(
            'Agent runtime already exists',
            409,
            error_details={'existing': existing_status}
        ).to_response()

    # 获取或创建 Agent Key
    agent_key_row = AgentKey.query.filter_by(agent_id=agent_id, is_active=True).first()
    runtime_agent_key = None

    if agent_key_row:
        runtime_agent_key = agent_key_row.reveal()
        if not runtime_agent_key:
            return ApiResponse.error(
                "Failed to decrypt existing agent key",
                500
            ).to_response()
    else:
        agent_key_row, runtime_agent_key = AgentKey.generate_key(
            name=f"Runtime Key for agent {agent_id}",
            workspace_id=workspace_id,
            agent_id=agent_id,
            created_by_user_id=user.id,
        )
        db.session.add(agent_key_row)
        db.session.commit()

    # 获取沙箱配置
    data = request.get_json() or {}
    sandbox_profile = data.get('sandbox_profile', agent.sandbox_profile or 'standard')

    try:
        # 启动 Pod
        result = controller.spawn_agent_pod(
            agent=agent,
            agent_key=runtime_agent_key,  # 注意：实际应该使用更安全的方式传递
            sandbox_profile=sandbox_profile
        )

        # 更新 Agent 状态
        agent.runner_enabled = True
        agent.execution_mode = 'managed_runner'
        db.session.commit()

        return ApiResponse.success({
            'pod': result,
            'agent': agent.to_dict(),
        }, 'Agent runtime spawned successfully').to_response()

    except Exception as e:
        return ApiResponse.error(
            f'Failed to spawn agent runtime: {str(e)}',
            500
        ).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/agents/<int:agent_id>/runtime/terminate',
    methods=['POST']
)
@unified_auth_required
def terminate_agent_runtime(workspace_id, agent_id):
    """
    终止 Agent 云端运行时

    删除 K8s 中的 Agent Pod
    """
    user = get_current_user()

    # 获取 Agent
    agent = Agent.query.filter_by(
        id=agent_id,
        workspace_id=workspace_id
    ).first()

    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    # 检查权限
    err = ensure_agent_manage_access(user, agent)
    if err:
        return err

    # 终止 Pod
    controller = get_agent_controller()
    success = controller.terminate_agent_pod(agent_id)

    if success:
        # 更新 Agent 状态
        agent.runner_enabled = False
        agent.execution_mode = 'external_pull'
        db.session.commit()

        return ApiResponse.success(
            {'terminated': True},
            'Agent runtime terminated'
        ).to_response()
    else:
        return ApiResponse.error(
            'No running runtime found for this agent',
            404
        ).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/agents/<int:agent_id>/runtime/status',
    methods=['GET']
)
@unified_auth_required
def get_agent_runtime_status(workspace_id, agent_id):
    """
    获取 Agent 运行时状态
    """
    user = get_current_user()

    # 获取 Agent
    agent = Agent.query.filter_by(
        id=agent_id,
        workspace_id=workspace_id
    ).first()

    if not agent:
        return ApiResponse.not_found('Agent not found').to_response()

    # 检查权限（只读权限即可）
    if not user.has_workspace_access(workspace_id):
        return ApiResponse.forbidden('Access denied').to_response()

    # 获取状态
    controller = get_agent_controller()
    pod_status = controller.get_agent_pod_status(agent_id)

    return ApiResponse.success({
        'agent_id': agent_id,
        'execution_mode': agent.execution_mode,
        'runner_enabled': agent.runner_enabled,
        'pod': pod_status,
    }).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/runtime/pods',
    methods=['GET']
)
@unified_auth_required
def list_runtime_pods(workspace_id):
    """
    列出工作区下的所有 Agent 运行时 Pod
    """
    user = get_current_user()

    if not user.has_workspace_access(workspace_id):
        return ApiResponse.forbidden('Access denied').to_response()

    controller = get_agent_controller()
    pods = controller.list_agent_pods(workspace_id=workspace_id)

    return ApiResponse.success({
        'pods': pods,
        'total': len(pods),
    }).to_response()


# ── 工作区运行时配额与回收策略（Phase 2）──

@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/runtime/settings',
    methods=['GET']
)
@unified_auth_required
def get_workspace_runtime_settings(workspace_id):
    """查看工作区运行时配额（在岗 Pod 上限 / 空闲回收阈值）。"""
    from models import Organization
    from services.workspace_runtime_policy import get_workspace_runtime_setting

    user = get_current_user()
    workspace = db.session.get(Organization, workspace_id)
    if not workspace:
        return ApiResponse.not_found('Workspace not found').to_response()
    from api.agent_common import ensure_workspace_access
    err = ensure_workspace_access(user, workspace)
    if err:
        return err

    controller = get_agent_controller()
    setting = get_workspace_runtime_setting(controller, workspace_id)
    return ApiResponse.success({'settings': setting}).to_response()


@agent_runtime_mgmt_bp.route(
    '/workspaces/<int:workspace_id>/runtime/settings',
    methods=['PUT']
)
@unified_auth_required
def update_workspace_runtime_settings(workspace_id):
    """设置工作区运行时配额：max_pods / idle_timeout_minutes（0=不限/不回收）。"""
    from models import Organization
    from services.workspace_runtime_policy import (
        get_workspace_runtime_setting,
        set_workspace_runtime_setting,
    )

    user = get_current_user()
    workspace = db.session.get(Organization, workspace_id)
    if not workspace:
        return ApiResponse.not_found('Workspace not found').to_response()
    from api.agent_common import ensure_workspace_access
    err = ensure_workspace_access(user, workspace)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    max_pods = data.get('max_pods')
    idle_timeout = data.get('idle_timeout_minutes')
    try:
        if max_pods is not None and not (0 <= int(max_pods) <= 100):
            return ApiResponse.error('max_pods must be within 0..100', 400).to_response()
        if idle_timeout is not None and not (0 <= int(idle_timeout) <= 10080):
            return ApiResponse.error('idle_timeout_minutes must be within 0..10080', 400).to_response()
    except (TypeError, ValueError):
        return ApiResponse.error('max_pods/idle_timeout_minutes must be integers', 400).to_response()

    set_workspace_runtime_setting(
        workspace_id,
        max_pods=max_pods,
        idle_timeout_minutes=idle_timeout,
    )
    setting = get_workspace_runtime_setting(get_agent_controller(), workspace_id)
    return ApiResponse.success({'settings': setting}).to_response()
