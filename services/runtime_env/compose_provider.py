"""Docker Compose 后端：每 Agent 一份生成的 compose 文件 + 独立 compose 项目。

适合"所有编排定义都要可审计/可手工接管"的单机部署：实例的镜像、环境变量、
资源上限落在 compose 文件里（默认 /tmp/todo4ai-compose，COMPOSE_RUNTIME_DIR 可改），
用 `docker compose -p todo4ai-agent-<id>` 管理。容器名与 docker 后端一致
（todo4ai-agent-<id>），状态/列表/容量逻辑直接复用。
"""

import os
from typing import Any, Dict, Optional

from core.config import Config
from services.runtime_env.docker_provider import DockerRuntimeProvider, build_runtime_env
from services.runtime_env.base import normalize_runtime

_COMPOSE_TEMPLATE = """# 由 todo4ai 平台生成（agent={agent_id}），手工修改会被下次 spawn 覆盖
services:
  agent-runtime:
    container_name: {name}
    image: {image}
    restart: unless-stopped
    cpus: "{cpu}"
    mem_limit: {memory}
    pids_limit: 512
    environment:
{env_block}
"""


class ComposeRuntimeProvider(DockerRuntimeProvider):
    """Compose 后端：spawn = 生成 compose 文件并 up -d；terminate = down。"""

    name = 'compose'

    def __init__(self, compose_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.compose_dir = compose_dir \
            or getattr(Config, 'COMPOSE_RUNTIME_DIR', None) \
            or os.path.join('/tmp', 'todo4ai-compose')

    # ── compose 文件 ──

    def _project(self, agent_id) -> str:
        return f'todo4ai-agent-{agent_id}'

    def _compose_path(self, agent_id) -> str:
        return os.path.join(self.compose_dir, f'agent-{agent_id}.yml')

    def _write_compose_file(self, agent, agent_key, sandbox_profile=None) -> str:
        from services.cloud_runtime import manifests

        runtime = manifests.runtime_type(agent)
        profile = sandbox_profile or agent.sandbox_profile or 'standard'
        resources = manifests.SANDBOX_RESOURCES.get(
            profile, manifests.SANDBOX_RESOURCES['standard'])
        cpu = str(resources['limits'].get('cpu', '1'))
        memory = str(resources['limits'].get('memory', '1Gi'))
        env = build_runtime_env(agent, agent_key)
        env_block = '\n'.join(f'      {k}: {v}' for k, v in env.items())
        content = _COMPOSE_TEMPLATE.format(
            agent_id=agent.id,
            name=self.container_name(agent.id),
            image=self.image_for(runtime),
            cpu=cpu,
            memory=memory,
            env_block=env_block,
        )
        os.makedirs(self.compose_dir, exist_ok=True)
        path = self._compose_path(agent.id)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content)
        return path

    def _compose(self, agent_id, args, timeout=120):
        return self._run(
            ['compose', '-p', self._project(agent_id),
             '-f', self._compose_path(agent_id)] + args,
            timeout=timeout,
        )

    # ── RuntimeProvider 覆盖 ──

    def spawn(self, agent, agent_key, sandbox_profile=None) -> Dict[str, Any]:
        self._write_compose_file(agent, agent_key, sandbox_profile)
        result = self._compose(agent.id, ['up', '-d'])
        if result.returncode != 0:
            raise RuntimeError(f'docker compose up failed: {result.stderr.strip()[:500]}')
        return self.get_runtime_status(agent.id) or normalize_runtime(
            name=self.container_name(agent.id),
            agent_id=agent.id, workspace_id=agent.workspace_id, phase='Pending',
        )

    def terminate(self, agent_id) -> bool:
        if self.get_runtime_status(agent_id) is None:
            return False
        result = self._compose(agent_id, ['down', '--remove-orphans'])
        return result.returncode == 0
