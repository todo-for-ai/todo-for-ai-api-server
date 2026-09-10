"""运行时环境后端（RuntimeProvider）可插拔抽象。

把「Agent 运行时跑在哪」从 K8s 专用实现抽象成五个可切换后端：
- k8s：现有 AgentRuntimeController（每 Agent 一个 Pod，gVisor/共享 PVC/配额回收）；
- docker：单机 Docker，每 Agent 一个容器（docker CLI 驱动，无新依赖）；
- compose：Docker Compose 项目封装（每 Agent 一份生成的 compose 文件）；
- baremetal：物理机/裸虚机，直接以宿主进程拉起 runtime（仅限可信单机环境）；
- remote：远程反连，agent-runtime daemon 跑在用户自己的机器上主动反连
  （平台不创建进程，spawn=注册、状态=连接/心跳新鲜度）。

选择后端：Config.RUNTIME_PROVIDER（env RUNTIME_PROVIDER，默认 k8s），
工厂见 get_runtime_provider()。调用方（management / goal_loop / 回收）只依赖
RuntimeProvider 接口，不感知后端差异。

引擎（Engine）轴与此正交：daemon 内部跑什么引擎（claude/codex/opencode/SDK）
由 services/runtime_env/engines.py 解析并注入，任何环境后端共用同一引擎注册表。

归一化契约（所有后端必须遵守，见 base.normalize_runtime）：
- 状态 dict：runtime_id / agent_id / workspace_id / runtime_type / phase /
  address / started_at / name；phase ∈ Running|Pending|Succeeded|Failed|Unknown；
- ensure_runtime 返回 {'status': 'already_running'|'created'|'workspace_pod_limit',
  'runtime': <状态 dict 或 None>}；
- 「在岗」= phase ∈ (Running, Pending)。
"""

from .base import OCCUPYING_PHASES, RuntimeProvider, normalize_runtime

_PROVIDERS = {}


def get_runtime_provider(kind=None):
    """按 Config.RUNTIME_PROVIDER（或显式 kind）取后端单例。"""
    from core.config import Config

    selected = (kind or getattr(Config, 'RUNTIME_PROVIDER', None) or 'k8s').strip().lower()
    if selected in _PROVIDERS:
        return _PROVIDERS[selected]

    if selected == 'k8s':
        from services.agent_runtime_controller import AgentRuntimeController
        provider = AgentRuntimeController()
    elif selected == 'docker':
        from .docker_provider import DockerRuntimeProvider
        provider = DockerRuntimeProvider()
    elif selected == 'compose':
        from .compose_provider import ComposeRuntimeProvider
        provider = ComposeRuntimeProvider()
    elif selected == 'baremetal':
        from .baremetal_provider import BaremetalRuntimeProvider
        provider = BaremetalRuntimeProvider()
    elif selected == 'remote':
        from .remote_provider import RemoteRuntimeProvider
        provider = RemoteRuntimeProvider()
    else:
        raise ValueError(
            f'Unknown RUNTIME_PROVIDER {selected!r} '
            '(supported: k8s | docker | compose | baremetal | remote)')

    _PROVIDERS[selected] = provider
    return provider


def get_runtime_provider_for_agent(agent):
    """按 Agent 的执行模式解析其所属执行环境。

    - managed_runner（平台托管）：部署级全局后端（RUNTIME_PROVIDER）；
    - 其余（external_pull 反连）：remote 后端——daemon 自管生命周期，
      平台只维护注册与状态视图。
    """
    mode = (getattr(agent, 'execution_mode', None) or 'external_pull').strip().lower()
    if mode == 'managed_runner':
        return get_runtime_provider()
    return get_runtime_provider('remote')


def reset_runtime_provider_cache():
    """测试辅助：清空后端单例缓存。"""
    _PROVIDERS.clear()
