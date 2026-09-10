"""Remote 反连后端：agent-runtime daemon 跑在用户自己的机器上，主动反连平台领任务。

与 k8s/docker/compose/baremetal 的「平台拉起」方向相反：平台不创建任何进程，
daemon 安装后凭 agent_key 经 WebSocket `/agent/ws` 反连，HTTP pull + 租约领任务。
但对外它同样满足 RuntimeProvider 契约，只是各方法的语义映射为「注册表」：

- spawn          = 注册（无进程可拉；已启用未连 → Pending，等 daemon 反连）；
- terminate      = 向在线 daemon 下发 shutdown 命令（离线无事可做 → False）；
- get_runtime_status = 连接/心跳新鲜度 → 归一化 phase（在线 Running /
  已启用待反连 Pending / 其余 Unknown）；
- list_runtimes  = 全部非 managed_runner Agent（managed_runner 归部署级后端管）。

在线判定：进程内 WS 连接注册表（单进程部署即时生效）或 last_seen_at 心跳
新鲜度（REMOTE_ONLINE_WINDOW_SECONDS，默认 90s；多 worker 部署下的兜底）。
引擎轴与本后端正交：daemon 内部跑什么引擎由 resolve_engine 决定，
元数据（host/engine/version）由 daemon 连接/心跳时上报。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import Config
from services.runtime_env.base import RuntimeProvider, normalize_runtime
from services.runtime_env.engines import resolve_engine


class RemoteRuntimeProvider(RuntimeProvider):
    """远程反连后端（execution_mode != 'managed_runner' 的 Agent 归它管）。"""

    name = 'remote'

    # 心跳新鲜窗（秒）：last_seen_at 距今小于该值视为在线
    ONLINE_WINDOW_SECONDS = 90

    def __init__(self):
        window = getattr(Config, 'REMOTE_ONLINE_WINDOW_SECONDS', None)
        if window:
            self.ONLINE_WINDOW_SECONDS = int(window)

    # ── 在线判定 ──

    @staticmethod
    def _is_connected(agent_id) -> bool:
        try:
            from api.agent_runtime_websocket import is_agent_connected
            return is_agent_connected(agent_id)
        except Exception:  # noqa: BLE001 — 非 Web 进程/模块缺失时退化为心跳判定
            return False

    def _is_fresh(self, agent) -> bool:
        last_seen = getattr(agent, 'last_seen_at', None)
        if not last_seen:
            return False
        delta = (datetime.utcnow() - last_seen).total_seconds()
        return abs(delta) <= self.ONLINE_WINDOW_SECONDS

    def _is_online(self, agent) -> bool:
        return self._is_connected(agent.id) or self._is_fresh(agent)

    # ── RuntimeProvider 接口 ──

    def spawn(self, agent, agent_key, sandbox_profile=None) -> Dict[str, Any]:
        """注册语义：不创建进程；返回注册后的期望状态（在线 Running / 待反连 Pending）。"""
        if self._is_online(agent):
            return self._status_for(agent)
        engine = resolve_engine(agent)
        return normalize_runtime(
            name=f'remote-agent-{agent.id}',
            runtime_id=f'remote-{agent.id}',
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            runtime_type=engine.key,
            phase='Pending',
            address=None,
            started_at=None,
            environment=self.name,
            engine=engine.key,
            online=False,
            last_seen_at=None,
        )

    def terminate(self, agent_id) -> bool:
        """向在线 daemon 下发 shutdown；离线/不存在返回 False。"""
        from models.agent import Agent

        agent = Agent.query.get(agent_id)
        if not agent or not self._is_online(agent):
            return False
        from api.agent_runtime_websocket import send_command_to_agent
        send_command_to_agent(agent_id, 'shutdown', {'reason': 'runtime_terminated'})
        return True

    def get_runtime_status(self, agent_id) -> Optional[Dict[str, Any]]:
        from models.agent import Agent

        agent = Agent.query.get(agent_id)
        if not agent:
            return None
        return self._status_for(agent)

    def list_runtimes(self, workspace_id=None) -> List[Dict[str, Any]]:
        """全部反连型 Agent（managed_runner 归部署级后端，避免双计）。"""
        from models.agent import Agent
        from sqlalchemy import or_

        query = Agent.query.filter(or_(
            Agent.execution_mode.is_(None),
            Agent.execution_mode != 'managed_runner',
        ))
        if workspace_id is not None:
            query = query.filter(Agent.workspace_id == workspace_id)
        return [self._status_for(agent) for agent in query.all()]

    # ── 归一化 ──

    def _status_for(self, agent) -> Dict[str, Any]:
        engine = resolve_engine(agent)
        online = self._is_online(agent)
        if online:
            phase = 'Running'
        elif agent.runner_enabled:
            phase = 'Pending'      # 已启用、等待 daemon 反连
        else:
            phase = 'Unknown'
        meta = self._runtime_meta(agent)
        return normalize_runtime(
            name=f'remote-agent-{agent.id}',
            runtime_id=f'remote-{agent.id}',
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            runtime_type=engine.key,
            phase=phase,
            address=meta.get('host'),
            started_at=None,
            environment=self.name,
            engine=engine.key,
            online=online,
            last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        )

    @staticmethod
    def _runtime_meta(agent) -> Dict[str, Any]:
        """daemon 上报的元数据（WS 连接/心跳时写入 agent.config['runtime_meta']）。"""
        config = getattr(agent, 'config', None) or {}
        meta = config.get('runtime_meta')
        return meta if isinstance(meta, dict) else {}
