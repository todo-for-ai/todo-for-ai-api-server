"""裸机（物理机/裸虚机）后端：直接以宿主进程拉起 agent-runtime。

适用场景：没有 Docker/K8s 的plain服务器——平台用 subprocess 把 runtime
以常驻进程方式跑在本机（进程组隔离，PID 与元数据落状态目录）。

⚠️ 安全边界：宿主进程没有容器隔离，仅建议在单租户/可信环境使用；
命令与工作目录必须显式配置（BAREMETAL_RUNTIME_COMMAND / BAREMETAL_RUNTIME_CWD），
否则 spawn 会拒绝执行（防误把平台自身目录当 runtime 跑）。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from core.config import Config
from services.runtime_env.base import RuntimeProvider, normalize_runtime
from services.runtime_env.docker_provider import build_runtime_env


class BaremetalRuntimeProvider(RuntimeProvider):
    """物理机后端：每 Agent 一个宿主进程，状态落 <state_dir>/agent-<id>.json。"""

    name = 'baremetal'

    def __init__(self, state_dir=None, command=None, cwd=None):
        self.state_dir = state_dir \
            or getattr(Config, 'BAREMETAL_STATE_DIR', None) \
            or os.path.join('/tmp', 'todo4ai-baremetal')
        self.command = command \
            or getattr(Config, 'BAREMETAL_RUNTIME_COMMAND', None) \
            or ''
        self.cwd = cwd \
            or getattr(Config, 'BAREMETAL_RUNTIME_CWD', None) \
            or ''
        os.makedirs(self.state_dir, exist_ok=True)

    # ── 状态文件 ──

    def _state_path(self, agent_id) -> str:
        return os.path.join(self.state_dir, f'agent-{agent_id}.json')

    def _read_state(self, agent_id) -> Optional[Dict[str, Any]]:
        path = self._state_path(agent_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_state(self, agent_id, state: Dict[str, Any]) -> None:
        with open(self._state_path(agent_id), 'w', encoding='utf-8') as fh:
            json.dump(state, fh)

    def _remove_state(self, agent_id) -> None:
        try:
            os.remove(self._state_path(agent_id))
        except OSError:
            pass

    @staticmethod
    def _pid_alive(pid) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 属其他用户但存在

    # ── RuntimeProvider 接口 ──

    def spawn(self, agent, agent_key, sandbox_profile=None) -> Dict[str, Any]:
        if not self.command or not self.cwd:
            raise RuntimeError(
                'baremetal runtime requires BAREMETAL_RUNTIME_COMMAND '
                'and BAREMETAL_RUNTIME_CWD to be configured')

        command = self.command.split() if isinstance(self.command, str) else list(self.command)
        env = {**os.environ, **build_runtime_env(agent, agent_key)}
        log_path = os.path.join(self.state_dir, f'agent-{agent.id}.log')
        with open(log_path, 'ab') as log_fh:
            proc = subprocess.Popen(
                command,
                cwd=self.cwd,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 独立进程组，终止时不伤及平台
            )

        state = {
            'pid': proc.pid,
            'agent_id': agent.id,
            'workspace_id': agent.workspace_id,
            'runtime_type': 'baremetal',
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'command': command,
            'log': log_path,
        }
        self._write_state(agent.id, state)
        return normalize_runtime(
            name=f'baremetal-agent-{agent.id}',
            runtime_id=str(proc.pid),
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            runtime_type='baremetal',
            phase='Running',
            address=socket.gethostname(),
            started_at=state['started_at'],
        )

    def terminate(self, agent_id) -> bool:
        state = self._read_state(agent_id)
        if not state:
            return False
        pid = state.get('pid')
        alive = self._pid_alive(pid)
        if alive:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            # 给进程组一个宽限，残留则 SIGKILL
            deadline = time.time() + 5
            while self._pid_alive(pid) and time.time() < deadline:
                time.sleep(0.2)
            if self._pid_alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self._remove_state(agent_id)
        return True

    def get_runtime_status(self, agent_id) -> Optional[Dict[str, Any]]:
        state = self._read_state(agent_id)
        if not state:
            return None
        running = self._pid_alive(state.get('pid'))
        if not running:
            self._remove_state(agent_id)
            return None
        return normalize_runtime(
            name=f'baremetal-agent-{agent_id}',
            runtime_id=str(state.get('pid')),
            agent_id=state.get('agent_id'),
            workspace_id=state.get('workspace_id'),
            runtime_type='baremetal',
            phase='Running',
            address=socket.gethostname(),
            started_at=state.get('started_at'),
        )

    def list_runtimes(self, workspace_id=None) -> List[Dict[str, Any]]:
        runtimes = []
        for entry in os.listdir(self.state_dir):
            if not (entry.startswith('agent-') and entry.endswith('.json')):
                continue
            try:
                agent_id = int(entry[len('agent-'):-len('.json')])
            except ValueError:
                continue
            status = self.get_runtime_status(agent_id)
            if status and (workspace_id is None or status.get('workspace_id') == workspace_id):
                runtimes.append(status)
        return runtimes
