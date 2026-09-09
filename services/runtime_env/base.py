"""RuntimeProvider 抽象基类：所有运行时后端的公共契约与共享流程。"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

# 「在岗」判定：占用资源的阶段（容量计数、幂等确保都用它）
OCCUPYING_PHASES = ('Running', 'Pending')


def normalize_runtime(name='', runtime_id='', agent_id=None, workspace_id=None,
                      runtime_type='', phase='Unknown', address=None,
                      started_at=None, **extra) -> Dict[str, Any]:
    """构造归一化的运行时状态 dict（跨后端统一形状）。"""
    info = {
        'runtime_id': runtime_id,
        'agent_id': agent_id,
        'workspace_id': workspace_id,
        'runtime_type': runtime_type,
        'phase': phase,
        'address': address,
        'started_at': started_at,
        'name': name,
    }
    info.update(extra)
    return info


class RuntimeProvider(ABC):
    """运行时后端接口：创建 / 终止 / 状态 / 列表。

    类属性 MAX_PODS_PER_WORKSPACE / POD_IDLE_TIMEOUT_MINUTES 与
    services/workspace_runtime_policy.get_workspace_runtime_setting 的
    默认值读取约定对齐（DB 设置 > Config > 类默认）。
    """

    name = 'base'
    MAX_PODS_PER_WORKSPACE = 10
    POD_IDLE_TIMEOUT_MINUTES = 30

    # ── 子类必须实现 ──────────────────────────────────────────

    @abstractmethod
    def spawn(self, agent, agent_key, sandbox_profile=None) -> Dict[str, Any]:
        """创建一个新运行时实例（不做幂等/容量检查，调用方保证），返回归一化状态。"""

    @abstractmethod
    def terminate(self, agent_id) -> bool:
        """终止运行时；不存在返回 False。"""

    @abstractmethod
    def get_runtime_status(self, agent_id) -> Optional[Dict[str, Any]]:
        """单实例状态（归一化）；不存在返回 None。"""

    @abstractmethod
    def list_runtimes(self, workspace_id=None) -> List[Dict[str, Any]]:
        """列出运行时（可选按工作区过滤），归一化状态列表。"""

    # ── 共享流程（模板方法，子类一般无需覆盖）─────────────────

    def ensure_runtime(self, agent, agent_key, sandbox_profile=None) -> Dict[str, Any]:
        """幂等确保运行时在岗：已在岗复用；工作区实例超限拒绝；否则创建。

        返回 {'status': 'already_running'|'created'|'workspace_pod_limit',
              'runtime': <归一化状态或 None>, ['cap'|'occupying' 当超限]}。
        """
        existing = self.get_runtime_status(agent.id)
        if existing and existing.get('phase') in OCCUPYING_PHASES:
            return {'status': 'already_running', 'runtime': existing}

        cap = self.workspace_instance_cap(agent.workspace_id)
        if cap > 0:
            occupying = [r for r in self.list_runtimes(agent.workspace_id)
                         if r.get('phase') in OCCUPYING_PHASES]
            if len(occupying) >= cap:
                return {'status': 'workspace_pod_limit', 'runtime': None,
                        'cap': cap, 'occupying': len(occupying)}

        runtime = self.spawn(agent, agent_key, sandbox_profile)
        return {'status': 'created', 'runtime': runtime}

    def workspace_instance_cap(self, workspace_id) -> int:
        """工作区同时在岗实例上限（0=用 DB 未设置时的系统默认；负数视为不限由调用方处理）。"""
        from services.workspace_runtime_policy import get_workspace_runtime_setting
        return int(get_workspace_runtime_setting(self, workspace_id)['max_pods'])
