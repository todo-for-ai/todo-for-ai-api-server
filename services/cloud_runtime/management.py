"""Agent 云端运行时生命周期管理（业务层）。

从 api/agent_runtime_mgmt.py 抽出的业务流程：运行时 spawn/terminate/status/list
与工作区配额设置。路由层只做"找资源 → 鉴权 → 调这里 → 异常映射 HTTP 语义"。
"""

from models import db
from models.agent_key import AgentKey
from services.agent_runtime_controller import get_agent_controller
from services.workspace_runtime_policy import (
    active_agent_count,
    get_workspace_runtime_setting,
    set_workspace_runtime_setting,
)

# 视为"已在岗"的 Pod 阶段（spawn 前的幂等护栏）
_OCCUPYING_PHASES = ('Running', 'Pending')


class RuntimeManagementError(Exception):
    """运行时管理业务错误（status_code 由路由层映射为 HTTP 响应）。"""

    status_code = 500


class AgentRuntimeExistsError(RuntimeManagementError):
    status_code = 409

    def __init__(self, existing_status):
        super().__init__('Agent runtime already exists')
        self.existing = existing_status


class AgentKeyDecryptError(RuntimeManagementError):
    status_code = 500


class SpawnRuntimeError(RuntimeManagementError):
    status_code = 500


class NoRunningRuntimeError(RuntimeManagementError):
    status_code = 404


class SettingsValidationError(RuntimeManagementError):
    status_code = 400


def _occupying_status(agent_id):
    status = get_agent_controller().get_agent_pod_status(agent_id)
    if status and status.get('phase') in _OCCUPYING_PHASES:
        raise AgentRuntimeExistsError(status)


def _resolve_runtime_key(agent, workspace_id, user_id):
    """取可直接注入 Pod 的 Agent Key 明文：已有则解密，缺失则生成落库。"""
    row = AgentKey.query.filter_by(agent_id=agent.id, is_active=True).first()
    if row:
        revealed = row.reveal()
        if not revealed:
            raise AgentKeyDecryptError('Failed to decrypt existing agent key')
        return revealed
    row, plaintext = AgentKey.generate_key(
        name=f'Runtime Key for agent {agent.id}',
        workspace_id=workspace_id,
        agent_id=agent.id,
        created_by_user_id=user_id,
    )
    db.session.add(row)
    db.session.commit()
    return plaintext


def spawn_runtime(agent, workspace_id, user_id, payload):
    """启动云端运行时：幂等检查 → 凭据 → 创建 Pod → 更新 Agent 执行模式。"""
    _occupying_status(agent.id)
    runtime_key = _resolve_runtime_key(agent, workspace_id, user_id)
    data = payload or {}
    sandbox_profile = data.get('sandbox_profile', agent.sandbox_profile or 'standard')
    try:
        result = get_agent_controller().spawn_agent_pod(
            agent=agent,
            agent_key=runtime_key,
            sandbox_profile=sandbox_profile,
        )
    except Exception as e:
        raise SpawnRuntimeError(f'Failed to spawn agent runtime: {e}')
    agent.runner_enabled = True
    agent.execution_mode = 'managed_runner'
    db.session.commit()
    return {'pod': result, 'agent': agent.to_dict()}


def terminate_runtime(agent):
    """终止云端运行时并回切外部拉取模式；无在岗 Pod 时 404。"""
    if not get_agent_controller().terminate_agent_pod(agent.id):
        raise NoRunningRuntimeError('No running runtime found for this agent')
    agent.runner_enabled = False
    agent.execution_mode = 'external_pull'
    db.session.commit()
    return {'terminated': True}


def runtime_status(agent):
    return {
        'agent_id': agent.id,
        'execution_mode': agent.execution_mode,
        'runner_enabled': agent.runner_enabled,
        'pod': get_agent_controller().get_agent_pod_status(agent.id),
    }


def list_runtime_pods(workspace_id):
    pods = get_agent_controller().list_agent_pods(workspace_id=workspace_id)
    return {'pods': pods, 'total': len(pods)}


def get_runtime_settings(workspace_id):
    settings = get_workspace_runtime_setting(get_agent_controller(), workspace_id)
    # 当前「正在干活」的 distinct Agent 数，供前端显示水位（active/limit）
    return {
        'settings': settings,
        'orchestration': {
            'active_agents': active_agent_count(workspace_id),
        },
    }


def update_runtime_settings(workspace_id, payload):
    """校验并保存工作区配额（max_pods / idle_timeout_minutes /
    max_concurrent_agents，None=不改）。"""
    data = payload or {}
    max_pods = data.get('max_pods')
    idle_timeout = data.get('idle_timeout_minutes')
    max_agents = data.get('max_concurrent_agents')
    try:
        if max_pods is not None and not 0 <= int(max_pods) <= 100:
            raise SettingsValidationError('max_pods must be within 0..100')
        if idle_timeout is not None and not 0 <= int(idle_timeout) <= 10080:
            raise SettingsValidationError(
                'idle_timeout_minutes must be within 0..10080')
        if max_agents is not None and not 0 <= int(max_agents) <= 200:
            raise SettingsValidationError(
                'max_concurrent_agents must be within 0..200')
    except (TypeError, ValueError):
        raise SettingsValidationError(
            'max_pods/idle_timeout_minutes/max_concurrent_agents must be integers')
    set_workspace_runtime_setting(
        workspace_id,
        max_pods=max_pods,
        idle_timeout_minutes=idle_timeout,
        max_concurrent_agents=max_agents,
    )
    return get_runtime_settings(workspace_id)
