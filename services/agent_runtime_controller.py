"""
Agent Runtime Controller

K8s Operator 风格的控制器，管理 Agent 容器的生命周期
"""

import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional
from uuid import uuid4

import kubernetes.client
from kubernetes.client import V1Pod, V1PodSpec, V1Container, V1ResourceRequirements
from kubernetes.client.rest import ApiException

from core.config import Config
from models.agent import Agent
from utils.logger import logger


class AgentRuntimeController:
    """Agent 运行时控制器"""

    # 运行时镜像映射
    RUNTIME_IMAGES = {
        'openai': 'todo4ai/agent-openai:latest',
        'anthropic': 'todo4ai/agent-claude:latest',
        'google': 'todo4ai/agent-gemini:latest',
        'ollama': 'todo4ai/agent-ollama:latest',
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

    def __init__(self):
        self.namespace = Config.K8S_AGENT_NAMESPACE or 'todo4ai-agents'
        self.k8s_client = None
        self.core_v1 = None
        self._init_k8s_client()

    def _init_k8s_client(self):
        """初始化 K8s 客户端"""
        try:
            # 尝试加载集群内配置
            kubernetes.config.load_incluster_config()
            logger.info("controller.k8s_loaded", source="incluster")
        except kubernetes.config.config_exception.ConfigException:
            # 回退到本地配置
            kubernetes.config.load_kube_config()
            logger.info("controller.k8s_loaded", source="kubeconfig")

        self.k8s_client = kubernetes.client.ApiClient()
        self.core_v1 = kubernetes.client.CoreV1Api(self.k8s_client)

    def spawn_agent_pod(
        self,
        agent: Agent,
        agent_key: str,
        sandbox_profile: str = 'standard'
    ) -> Dict:
        """
        启动 Agent Pod

        Args:
            agent: Agent 模型实例
            agent_key: Agent 密钥
            sandbox_profile: 沙箱配置档位 (minimal/standard/performance)

        Returns:
            Pod 信息字典
        """
        pod_name = f"agent-{agent.id}-{uuid4().hex[:8]}"

        # 构建 Pod 配置
        pod = self._build_pod(
            name=pod_name,
            agent=agent,
            agent_key=agent_key,
            sandbox_profile=sandbox_profile
        )

        try:
            # 创建 Pod
            created_pod = self.core_v1.create_namespaced_pod(
                namespace=self.namespace,
                body=pod
            )

            logger.info(
                "controller.pod_created",
                pod_name=pod_name,
                agent_id=agent.id,
                namespace=self.namespace,
            )

            return {
                'pod_name': pod_name,
                'pod_uid': created_pod.metadata.uid,
                'status': 'creating',
                'agent_id': agent.id,
                'created_at': datetime.utcnow().isoformat(),
            }

        except ApiException as e:
            logger.error(
                "controller.pod_create_failed",
                pod_name=pod_name,
                error=str(e),
            )
            raise RuntimeError(f"Failed to create pod: {e}")

    def terminate_agent_pod(self, agent_id: int) -> bool:
        """
        终止 Agent Pod

        Args:
            agent_id: Agent ID

        Returns:
            是否成功终止
        """
        # 查找 Pod
        pods = self._find_pods_by_agent(agent_id)

        if not pods:
            logger.warning("controller.no_pods_found", agent_id=agent_id)
            return False

        deleted = False
        for pod in pods:
            try:
                self.core_v1.delete_namespaced_pod(
                    name=pod.metadata.name,
                    namespace=self.namespace,
                    body=kubernetes.client.V1DeleteOptions(
                        grace_period_seconds=30
                    )
                )
                deleted = True
                logger.info(
                    "controller.pod_deleted",
                    pod_name=pod.metadata.name,
                    agent_id=agent_id,
                )
            except ApiException as e:
                logger.error(
                    "controller.pod_delete_failed",
                    pod_name=pod.metadata.name,
                    error=str(e),
                )

        return deleted

    def get_agent_pod_status(self, agent_id: int) -> Optional[Dict]:
        """
        获取 Agent Pod 状态

        Args:
            agent_id: Agent ID

        Returns:
            Pod 状态字典或 None
        """
        pods = self._find_pods_by_agent(agent_id)

        if not pods:
            return None

        pod = pods[0]  # 取第一个
        return self._format_pod_status(pod)

    def list_agent_pods(
        self,
        workspace_id: Optional[int] = None
    ) -> List[Dict]:
        """
        列出 Agent Pods

        Args:
            workspace_id: 可选的工作区过滤

        Returns:
            Pod 状态列表
        """
        label_selector = 'app=todo4ai-agent'
        if workspace_id:
            label_selector += f',workspace-id={workspace_id}'

        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector
            )

            return [self._format_pod_status(pod) for pod in pods.items]

        except ApiException as e:
            logger.error("controller.list_pods_failed", error=str(e))
            return []

    def _build_pod(
        self,
        name: str,
        agent: Agent,
        agent_key: str,
        sandbox_profile: str
    ) -> V1Pod:
        """构建 Pod 配置"""
        # 确定镜像
        runtime_type = self._get_runtime_type(agent)
        image = self.RUNTIME_IMAGES.get(runtime_type, self.RUNTIME_IMAGES['custom'])

        # 确定资源
        resources = self.SANDBOX_RESOURCES.get(
            sandbox_profile,
            self.SANDBOX_RESOURCES['standard']
        )

        # 构建环境变量
        env = self._build_env_vars(agent, agent_key)

        # 构建标签
        labels = {
            'app': 'todo4ai-agent',
            'agent-id': str(agent.id),
            'workspace-id': str(agent.workspace_id),
            'runtime-type': runtime_type,
        }

        # 创建 Pod
        return V1Pod(
            api_version='v1',
            kind='Pod',
            metadata=kubernetes.client.V1ObjectMeta(
                name=name,
                labels=labels,
                annotations={
                    'todo4ai.io/agent-name': agent.name,
                    'todo4ai.io/created-at': datetime.utcnow().isoformat(),
                }
            ),
            spec=V1PodSpec(
                runtime_class_name='gvisor',  # 使用 gVisor 沙箱
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
                        image_pull_policy='Always',
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
                            )
                        ],
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
                    )
                ]
            )
        )

    def _build_env_vars(
        self,
        agent: Agent,
        agent_key: str
    ) -> List[kubernetes.client.V1EnvVar]:
        """构建环境变量"""
        env_vars = [
            kubernetes.client.V1EnvVar(
                name='AGENT_KEY',
                value=agent_key
            ),
            kubernetes.client.V1EnvVar(
                name='API_BASE_URL',
                value=Config.API_BASE_URL or 'https://api.todo-for-ai.com/todo-for-ai/api/v1'
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
                value=self._get_network_mode(agent)
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

        # 添加 LLM API Key（从 Secret）
        if agent.llm_provider:
            env_vars.append(
                kubernetes.client.V1EnvVar(
                    name='LLM_API_KEY',
                    value_from=kubernetes.client.V1EnvVarSource(
                        secret_key_ref=kubernetes.client.V1SecretKeySelector(
                            name=f'agent-llm-keys',
                            key=agent.llm_provider,
                            optional=True
                        )
                    )
                )
            )

        return env_vars

    def _get_runtime_type(self, agent: Agent) -> str:
        """获取运行时类型"""
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

    def _get_network_mode(self, agent: Agent) -> str:
        """获取网络模式"""
        policy = agent.sandbox_policy or {}
        return policy.get('network_mode', 'isolated')

    def _find_pods_by_agent(self, agent_id: int) -> List[V1Pod]:
        """根据 Agent ID 查找 Pods"""
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f'app=todo4ai-agent,agent-id={agent_id}'
            )
            return pods.items
        except ApiException:
            return []

    def _format_pod_status(self, pod: V1Pod) -> Dict:
        """格式化 Pod 状态"""
        return {
            'pod_name': pod.metadata.name,
            'pod_uid': pod.metadata.uid,
            'agent_id': int(pod.metadata.labels.get('agent-id', 0)),
            'workspace_id': int(pod.metadata.labels.get('workspace-id', 0)),
            'runtime_type': pod.metadata.labels.get('runtime-type', 'unknown'),
            'phase': pod.status.phase,
            'pod_ip': pod.status.pod_ip,
            'host_ip': pod.status.host_ip,
            'start_time': pod.status.start_time.isoformat() if pod.status.start_time else None,
            'conditions': [
                {
                    'type': c.type,
                    'status': c.status,
                    'reason': c.reason,
                }
                for c in (pod.status.conditions or [])
            ],
        }


    @staticmethod
    def auto_assign_task(task):
        """Auto-assign AI task to an active agent on creation.

        This creates an AgentTaskAttempt and AgentTaskLease immediately
        so the runtime can pull the task without waiting for WebSocket push.
        """
        from models import Agent, AgentTaskAttempt, AgentTaskLease, TaskStatus, db
        from api.agent_common import generate_id, now_utc
        from datetime import timedelta

        agent = Agent.query.filter_by(
            workspace_id=task.owner_id,
            runner_enabled=True,
            status='ACTIVE'
        ).first()
        if not agent:
            return

        now = now_utc()
        attempt_id = generate_id('att')
        lease_id = generate_id('lea')
        lease_exp = now + timedelta(seconds=60)

        attempt = AgentTaskAttempt(
            attempt_id=attempt_id,
            task_id=task.id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            state='ACTIVE',
            lease_id=lease_id,
            started_at=now,
            created_by='system',
        )
        lease = AgentTaskLease(
            lease_id=lease_id,
            task_id=task.id,
            attempt_id=attempt_id,
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            expires_at=lease_exp,
            active=True,
            created_by='system',
        )
        db.session.add(attempt)
        db.session.add(lease)
        if task.status == TaskStatus.TODO:
            task.status = TaskStatus.IN_PROGRESS
        db.session.commit()


# 单例实例
_controller: Optional[AgentRuntimeController] = None


def get_agent_controller() -> AgentRuntimeController:
    """获取控制器单例"""
    global _controller
    if _controller is None:
        _controller = AgentRuntimeController()
    return _controller
