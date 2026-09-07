"""Agent Pod 清单构建（纯函数，独立于 K8s 客户端）。

从 AgentRuntimeController 抽出的"声明式部分"：镜像/资源/环境变量/
RuntimeClass 等全部是可纯测的规格构造，便于 100% 覆盖与后续维护。
"""

import os
from typing import Dict, List

import kubernetes.client
from kubernetes.client import V1Pod, V1PodSpec, V1Container, V1ResourceRequirements

from core.config import Config
from models.agent import Agent

# 运行时镜像映射（claude/codex/opencode 走 cli-agents 镜像）
RUNTIME_IMAGES = {
    'openai': 'todo4ai/agent-openai:latest',
    'anthropic': 'todo4ai/agent-claude:latest',
    'google': 'todo4ai/agent-gemini:latest',
    'ollama': 'todo4ai/agent-ollama:latest',
    'claude': 'todo4ai/agent-cli-agents:latest',
    'codex': 'todo4ai/agent-cli-agents:latest',
    'opencode': 'todo4ai/agent-cli-agents:latest',
    'custom': 'todo4ai/agent-runtime:latest',
}

# 沙箱资源配置
SANDBOX_RESOURCES = {
    'minimal': {
        'requests': {'cpu': '100m', 'memory': '128Mi'},
        'limits': {'cpu': '500m', 'memory': '512Mi'},
    },
    'standard': {
        'requests': {'cpu': '500m', 'memory': '512Mi'},
        'limits': {'cpu': '2', 'memory': '2Gi'},
    },
    'performance': {
        'requests': {'cpu': '2', 'memory': '4Gi'},
        'limits': {'cpu': '4', 'memory': '8Gi'},
    },
}


def agent_policy(agent: Agent) -> dict:
    """Agent 的沙箱策略 JSON（兼容 None）。"""
    return agent.sandbox_policy or {}


def runtime_secret_field(agent_id: int) -> str:
    return f'agent-{agent_id}'


def runtime_type(agent: Agent) -> str:
    """运行时类型：沙箱策略显式声明的 CLI 引擎优先，否则按 LLM 供应商映射。"""
    cli_engine = (agent_policy(agent).get('cli_engine') or '').strip().lower()
    if cli_engine in ('claude', 'codex', 'opencode', 'custom'):
        return cli_engine
    provider = (agent.llm_provider or 'openai').lower()
    if provider in ['openai']:
        return 'openai'
    elif provider in ['anthropic', 'claude']:
        return 'anthropic'
    elif provider in ['google', 'gemini']:
        return 'google'
    elif provider in ['ollama', 'local']:
        return 'ollama'
    return 'custom'


def network_mode(agent: Agent) -> str:
    return agent_policy(agent).get('network_mode', 'isolated')


def build_env_vars(agent: Agent) -> List[kubernetes.client.V1EnvVar]:
    """构建环境变量（AGENT_KEY 走 SecretKeyRef，绝不落明文）。"""
    cli_engine = (agent_policy(agent).get('cli_engine') or '').strip().lower()
    env_vars = [
        kubernetes.client.V1EnvVar(
            name='AGENT_KEY',
            value_from=kubernetes.client.V1EnvVarSource(
                secret_key_ref=kubernetes.client.V1SecretKeySelector(
                    name='todo4ai-runtime-keys',
                    key=runtime_secret_field(agent.id),
                )
            ),
        ),
        kubernetes.client.V1EnvVar(
            name='API_BASE_URL',
            value=getattr(Config, 'API_BASE_URL', None)
            or 'https://api.todo-for-ai.com/todo-for-ai/api/v1'
        ),
        kubernetes.client.V1EnvVar(
            name='LLM_PROVIDER',
            value=agent.llm_provider or 'openai'
        ),
        kubernetes.client.V1EnvVar(
            name='LLM_MODEL',
            value=agent.llm_model or 'gpt-4'
        ),
        kubernetes.client.V1EnvVar(
            name='SANDBOX_MODE',
            value=agent.sandbox_profile or 'standard'
        ),
        kubernetes.client.V1EnvVar(
            name='SANDBOX_NETWORK_MODE',
            value=network_mode(agent)
        ),
        kubernetes.client.V1EnvVar(
            name='MAX_CONCURRENT_TASKS',
            value=str(agent.max_concurrency or 1)
        ),
        kubernetes.client.V1EnvVar(
            name='TASK_TIMEOUT_SECONDS',
            value=str(agent.timeout_seconds or 1800)
        ),
        kubernetes.client.V1EnvVar(
            name='HEARTBEAT_INTERVAL_SECONDS',
            value=str(agent.heartbeat_interval_seconds or 20)
        ),
        kubernetes.client.V1EnvVar(
            name='LOG_LEVEL',
            value='INFO'
        ),
    ]
    if cli_engine:
        env_vars.append(
            kubernetes.client.V1EnvVar(name='CLI_AGENT_ENGINE', value=cli_engine)
        )
    # LLM API Key（从 Secret）
    if agent.llm_provider:
        env_vars.append(
            kubernetes.client.V1EnvVar(
                name='LLM_API_KEY',
                value_from=kubernetes.client.V1EnvVarSource(
                    secret_key_ref=kubernetes.client.V1SecretKeySelector(
                        name='agent-llm-keys',
                        key=agent.llm_provider,
                        optional=True
                    )
                )
            )
        )
    return env_vars


def build_pod(name: str, agent: Agent, secret_name: str,
              sandbox_profile: str) -> V1Pod:
    """构建 Agent Pod 清单。"""
    runtime = runtime_type(agent)
    image = RUNTIME_IMAGES.get(runtime, RUNTIME_IMAGES['custom'])
    resources = SANDBOX_RESOURCES.get(sandbox_profile, SANDBOX_RESOURCES['standard'])
    env = build_env_vars(agent)
    shared_workspace = bool(agent_policy(agent).get('shared_workspace'))

    runtime_class = os.getenv('K8S_AGENT_RUNTIME_CLASS') or getattr(
        Config, 'K8S_AGENT_RUNTIME_CLASS', None)
    image_pull_policy = os.getenv('K8S_IMAGE_PULL_POLICY') or getattr(
        Config, 'K8S_IMAGE_PULL_POLICY', 'IfNotPresent')

    return V1Pod(
        api_version='v1',
        kind='Pod',
        metadata=kubernetes.client.V1ObjectMeta(
            name=name,
            labels={
                'app': 'todo4ai-agent',
                'agent-id': str(agent.id),
                'workspace-id': str(agent.workspace_id),
                'runtime-type': runtime,
            },
            annotations={
                'todo4ai.io/agent-name': agent.name,
                'todo4ai.io/created-at': datetime_utcnow_isoformat(),
            }
        ),
        spec=V1PodSpec(
            runtime_class_name=runtime_class,
            restart_policy='OnFailure',
            termination_grace_period_seconds=30,
            security_context=kubernetes.client.V1PodSecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                seccomp_profile=kubernetes.client.V1SeccompProfile(
                    type='RuntimeDefault'
                )
            ),
            containers=[
                V1Container(
                    name='agent-runtime',
                    image=image,
                    image_pull_policy=image_pull_policy,
                    env=env,
                    resources=V1ResourceRequirements(
                        requests=resources['requests'],
                        limits=resources['limits']
                    ),
                    ports=[
                        kubernetes.client.V1ContainerPort(
                            container_port=8080,
                            name='metrics'
                        )
                    ],
                    security_context=kubernetes.client.V1SecurityContext(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        capabilities=kubernetes.client.V1Capabilities(
                            drop=['ALL']
                        )
                    ),
                    volume_mounts=[
                        kubernetes.client.V1VolumeMount(
                            name='tmp',
                            mount_path='/tmp'
                        ),
                        kubernetes.client.V1VolumeMount(
                            name='cache',
                            mount_path='/app/.cache'
                        ),
                    ] + ([
                        kubernetes.client.V1VolumeMount(
                            name='shared-workspace',
                            mount_path='/workspace/shared'
                        ),
                    ] if shared_workspace else []),
                    liveness_probe=kubernetes.client.V1Probe(
                        http_get=kubernetes.client.V1HTTPGetAction(
                            path='/health',
                            port=8080
                        ),
                        initial_delay_seconds=10,
                        period_seconds=30
                    ),
                    readiness_probe=kubernetes.client.V1Probe(
                        http_get=kubernetes.client.V1HTTPGetAction(
                            path='/ready',
                            port=8080
                        ),
                        initial_delay_seconds=5,
                        period_seconds=10
                    )
                )
            ],
            volumes=[
                kubernetes.client.V1Volume(
                    name='tmp',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(
                        size_limit='1Gi'
                    )
                ),
                kubernetes.client.V1Volume(
                    name='cache',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(
                        size_limit='500Mi'
                    )
                ),
            ] + ([
                kubernetes.client.V1Volume(
                    name='shared-workspace',
                    persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=f'todo4ai-ws-{agent.workspace_id}-shared',
                    ),
                ),
            ] if shared_workspace else [])
        )
    )


def datetime_utcnow_isoformat() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat()
