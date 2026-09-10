"""Agent Pod 清单构建（纯函数，独立于 K8s 客户端）。

从 AgentRuntimeController 抽出的"声明式部分"：镜像/资源/环境变量/
RuntimeClass 等全部是可纯测的规格构造，便于 100% 覆盖与后续维护。
"""

import os
from typing import Dict, List

from services.runtime_env.engines import (
    ENGINE_SPECS,
    engine_task_env,
    resolve_engine,
)

def _kc():
    """kubernetes.client 惰性导入：非 K8s 部署（docker/compose/baremetal）无需装 kubernetes 包。"""
    import kubernetes.client
    return kubernetes.client



from core.config import Config
from models.agent import Agent

# 运行时镜像映射：由引擎注册表（services/runtime_env/engines.py）派生，
# 引擎轴的单一事实来源在那里；此处仅为向后兼容保留同名导出。
RUNTIME_IMAGES = {spec.key: spec.image for spec in ENGINE_SPECS}

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
    """运行时（引擎）类型：委托引擎注册表解析（策略声明 CLI 引擎优先，否则按 LLM 供应商）。"""
    return resolve_engine(agent).key


def network_mode(agent: Agent) -> str:
    return agent_policy(agent).get('network_mode', 'isolated')


def build_env_vars(agent: Agent) -> list:
    """构建环境变量（AGENT_KEY 走 SecretKeyRef，绝不落明文）。

    引擎/任务相关的明文变量统一来自 engines.engine_task_env；
    K8s 特有的凭据注入（SecretKeyRef）与回连地址在此追加。
    """
    env_vars = [
        _kc().V1EnvVar(
            name='AGENT_KEY',
            value_from=_kc().V1EnvVarSource(
                secret_key_ref=_kc().V1SecretKeySelector(
                    name='todo4ai-runtime-keys',
                    key=runtime_secret_field(agent.id),
                )
            ),
        ),
        _kc().V1EnvVar(
            name='API_BASE_URL',
            value=getattr(Config, 'API_BASE_URL', None)
            or 'https://api.todo-for-ai.com/todo-for-ai/api/v1'
        ),
    ]
    for name, value in engine_task_env(agent).items():
        env_vars.append(_kc().V1EnvVar(name=name, value=value))
    # LLM API Key（从 Secret）
    if agent.llm_provider:
        env_vars.append(
            _kc().V1EnvVar(
                name='LLM_API_KEY',
                value_from=_kc().V1EnvVarSource(
                    secret_key_ref=_kc().V1SecretKeySelector(
                        name='agent-llm-keys',
                        key=agent.llm_provider,
                        optional=True
                    )
                )
            )
        )
    return env_vars


def build_pod(name: str, agent: Agent, secret_name: str,
              sandbox_profile: str):
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

    return _kc().V1Pod(
        api_version='v1',
        kind='Pod',
        metadata=_kc().V1ObjectMeta(
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
        spec=_kc().V1PodSpec(
            runtime_class_name=runtime_class,
            restart_policy='OnFailure',
            termination_grace_period_seconds=30,
            security_context=_kc().V1PodSecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                seccomp_profile=_kc().V1SeccompProfile(
                    type='RuntimeDefault'
                )
            ),
            containers=[
                _kc().V1Container(
                    name='agent-runtime',
                    image=image,
                    image_pull_policy=image_pull_policy,
                    env=env,
                    resources=_kc().V1ResourceRequirements(
                        requests=resources['requests'],
                        limits=resources['limits']
                    ),
                    ports=[
                        _kc().V1ContainerPort(
                            container_port=8080,
                            name='metrics'
                        )
                    ],
                    security_context=_kc().V1SecurityContext(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        capabilities=_kc().V1Capabilities(
                            drop=['ALL']
                        )
                    ),
                    volume_mounts=[
                        _kc().V1VolumeMount(
                            name='tmp',
                            mount_path='/tmp'
                        ),
                        _kc().V1VolumeMount(
                            name='cache',
                            mount_path='/app/.cache'
                        ),
                    ] + ([
                        _kc().V1VolumeMount(
                            name='shared-workspace',
                            mount_path='/workspace/shared'
                        ),
                    ] if shared_workspace else []),
                    liveness_probe=_kc().V1Probe(
                        http_get=_kc().V1HTTPGetAction(
                            path='/health',
                            port=8080
                        ),
                        initial_delay_seconds=10,
                        period_seconds=30
                    ),
                    readiness_probe=_kc().V1Probe(
                        http_get=_kc().V1HTTPGetAction(
                            path='/ready',
                            port=8080
                        ),
                        initial_delay_seconds=5,
                        period_seconds=10
                    )
                )
            ],
            volumes=[
                _kc().V1Volume(
                    name='tmp',
                    empty_dir=_kc().V1EmptyDirVolumeSource(
                        size_limit='1Gi'
                    )
                ),
                _kc().V1Volume(
                    name='cache',
                    empty_dir=_kc().V1EmptyDirVolumeSource(
                        size_limit='500Mi'
                    )
                ),
            ] + ([
                _kc().V1Volume(
                    name='shared-workspace',
                    persistent_volume_claim=_kc().V1PersistentVolumeClaimVolumeSource(
                        claim_name=f'todo4ai-ws-{agent.workspace_id}-shared',
                    ),
                ),
            ] if shared_workspace else [])
        )
    )


def datetime_utcnow_isoformat() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat()
