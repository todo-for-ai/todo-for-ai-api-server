"""Docker 单机后端：每 Agent 一个容器，经 docker CLI 驱动（无新增 Python 依赖）。

适用于单机/小规模部署：平台与 Docker 守护进程同机（或 DOCKER_HOST 可达）。
凭据经 `-e AGENT_KEY=...` 注入容器（单机可信场景；K8s 后端走 SecretKeyRef）。
资源上限映射自 manifests.SANDBOX_RESOURCES 档位（--cpus / --memory / --pids-limit）。
"""

import json
import subprocess
from typing import Any, Dict, List, Optional

from core.config import Config
from services.runtime_env.base import OCCUPYING_PHASES, RuntimeProvider, normalize_runtime

# 状态映射：docker container state → 归一化 phase
_DOCKER_PHASE_MAP = {
    'running': 'Running',
    'created': 'Pending',
    'restarting': 'Pending',
    'paused': 'Unknown',
    'exited': 'Failed',
    'dead': 'Failed',
}

MANAGED_LABEL = 'todo4ai.managed=true'
LABEL_AGENT = 'todo4ai.agent-id'
LABEL_WORKSPACE = 'todo4ai.workspace-id'
LABEL_RUNTIME_TYPE = 'todo4ai.runtime-type'


def _memory_to_docker(value: str) -> str:
    """'2Gi' → '2g'，'512Mi' → '512m'（docker --memory 接受 b/k/m/g）。"""
    v = value.strip()
    if v.endswith('Gi'):
        return v[:-2] + 'g'
    if v.endswith('Mi'):
        return v[:-2] + 'm'
    if v.endswith('G'):
        return v[:-1] + 'g'
    if v.endswith('M'):
        return v[:-1] + 'm'
    return v


def build_runtime_env(agent, agent_key: str) -> Dict[str, str]:
    """跨后端共享的运行时环境变量（与 manifests.build_env_vars 语义对齐）。"""
    policy = agent.sandbox_policy or {}
    env = {
        'AGENT_KEY': agent_key,
        'API_BASE_URL': getattr(Config, 'DOCKER_API_BASE_URL', None)
        or getattr(Config, 'API_BASE_URL', None)
        or 'http://host.docker.internal:50110/todo-for-ai/api/v1',
        'LLM_PROVIDER': agent.llm_provider or 'openai',
        'LLM_MODEL': agent.llm_model or 'gpt-4',
        'SANDBOX_MODE': agent.sandbox_profile or 'standard',
        'SANDBOX_NETWORK_MODE': policy.get('network_mode', 'isolated'),
        'MAX_CONCURRENT_TASKS': str(agent.max_concurrency or 1),
        'TASK_TIMEOUT_SECONDS': str(agent.timeout_seconds or 1800),
        'HEARTBEAT_INTERVAL_SECONDS': str(agent.heartbeat_interval_seconds or 20),
        'LOG_LEVEL': 'INFO',
    }
    cli_engine = (policy.get('cli_engine') or '').strip().lower()
    if cli_engine:
        env['CLI_AGENT_ENGINE'] = cli_engine
    return env


class DockerRuntimeProvider(RuntimeProvider):
    """单机 Docker 后端（每 Agent 一个容器，名字固定便于幂等）。"""

    name = 'docker'

    def __init__(self, image=None, network=None, docker_cmd='docker'):
        self.image = image or getattr(Config, 'DOCKER_RUNTIME_IMAGE', None) \
            or 'todo4ai/agent-runtime:latest'
        self.network = network if network is not None \
            else (getattr(Config, 'DOCKER_RUNTIME_NETWORK', None) or '')
        self.docker_cmd = docker_cmd

    # ── docker CLI 基础封装 ──

    def _run(self, args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.docker_cmd] + args, capture_output=True, text=True, timeout=timeout,
        )

    def container_name(self, agent_id) -> str:
        return f'todo4ai-agent-{agent_id}'

    # ── RuntimeProvider 接口 ──

    def spawn(self, agent, agent_key, sandbox_profile=None) -> Dict[str, Any]:
        from services.cloud_runtime import manifests

        runtime = manifests.runtime_type(agent)
        profile = sandbox_profile or agent.sandbox_profile or 'standard'
        resources = manifests.SANDBOX_RESOURCES.get(profile, manifests.SANDBOX_RESOURCES['standard'])
        cpu = str(resources['limits'].get('cpu', '1'))
        memory = _memory_to_docker(str(resources['limits'].get('memory', '1Gi')))

        args = [
            'run', '-d',
            '--name', self.container_name(agent.id),
            '--restart', 'unless-stopped',
            '--label', MANAGED_LABEL,
            '--label', f'{LABEL_AGENT}={agent.id}',
            '--label', f'{LABEL_WORKSPACE}={agent.workspace_id}',
            '--label', f'{LABEL_RUNTIME_TYPE}={runtime}',
            '--cpus', cpu,
            '--memory', memory,
            '--pids-limit', '512',
        ]
        if self.network:
            args += ['--network', self.network]
        for key, value in build_runtime_env(agent, agent_key).items():
            args += ['-e', f'{key}={value}']
        args += [self.image_for(runtime)]

        result = self._run(args, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f'docker run failed: {result.stderr.strip()[:500]}')
        return self.get_runtime_status(agent.id) or normalize_runtime(
            name=self.container_name(agent.id), runtime_id=result.stdout.strip()[:12],
            agent_id=agent.id, workspace_id=agent.workspace_id,
            runtime_type=runtime, phase='Pending',
        )

    def image_for(self, runtime_type: str) -> str:
        from services.cloud_runtime import manifests
        return manifests.RUNTIME_IMAGES.get(runtime_type, self.image)

    def terminate(self, agent_id) -> bool:
        result = self._run(['rm', '-f', self.container_name(agent_id)])
        return result.returncode == 0

    def get_runtime_status(self, agent_id) -> Optional[Dict[str, Any]]:
        result = self._run(['inspect', '--format', '{{json .}}', self.container_name(agent_id)])
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return self._normalize_inspect(data)

    def list_runtimes(self, workspace_id=None) -> List[Dict[str, Any]]:
        args = ['ps', '-a', '--filter', f'label={MANAGED_LABEL}', '--format', '{{json .}}']
        if workspace_id is not None:
            args += ['--filter', f'label={LABEL_WORKSPACE}={workspace_id}']
        result = self._run(args)
        if result.returncode != 0:
            return []
        runtimes = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            runtimes.append(self._normalize_ps_item(item))
        return runtimes

    # ── 归一化 ──

    def _normalize_inspect(self, data: Dict[str, Any]) -> Dict[str, Any]:
        state = (data.get('State') or {})
        labels = data.get('Config', {}).get('Labels') or {}
        phase = _DOCKER_PHASE_MAP.get(state.get('Status'), 'Unknown')
        agent_id = _to_int(labels.get('agent-id'))
        return normalize_runtime(
            name=(data.get('Name') or '').lstrip('/') or self.container_name(agent_id),
            runtime_id=(data.get('Id') or '')[:12],
            agent_id=agent_id,
            workspace_id=_to_int(labels.get('workspace-id')),
            runtime_type=labels.get('runtime-type', 'unknown'),
            phase=phase,
            address=(data.get('NetworkSettings') or {}).get('IPAddress') or None,
            started_at=state.get('StartedAt') or None,
        )

    def _normalize_ps_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        labels = {}
        raw = item.get('Labels') or ''
        for pair in raw.split(','):
            if '=' in pair:
                k, v = pair.split('=', 1)
                labels[k] = v
        return normalize_runtime(
            name=(item.get('Names') or '').strip(),
            runtime_id=(item.get('ID') or '')[:12],
            agent_id=_to_int(labels.get('agent-id')),
            workspace_id=_to_int(labels.get('workspace-id')),
            runtime_type=labels.get('runtime-type', 'unknown'),
            phase=_DOCKER_PHASE_MAP.get(item.get('State'), 'Unknown'),
            started_at=item.get('CreatedAt') or None,
        )


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
