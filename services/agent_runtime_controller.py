"""
Agent Runtime Controller

K8s Operator 风格的控制器，管理 Agent 容器的生命周期
"""

import asyncio
import base64
import os
from datetime import datetime
from typing import Dict, List, Optional
from uuid import uuid4

import kubernetes.client
from kubernetes.client import (
    V1Pod,
    V1PodSpec,
    V1Container,
    V1ResourceRequirements,
    V1Secret,
    V1PersistentVolumeClaim,
    V1PersistentVolumeClaimSpec,
)
from kubernetes.client.rest import ApiException

from core.config import Config
from models.agent import Agent
from services.cloud_runtime import manifests
from utils.logger import logger


class AgentRuntimeController:
    """Agent 运行时控制器"""

    # 运行时镜像映射（定义移至 services/cloud_runtime/manifests.py）
    RUNTIME_IMAGES = manifests.RUNTIME_IMAGES

    # 运行时凭据 Secret（per-namespace，key = agent-<id>；AGENT_KEY 以 secretKeyRef 注入，不再落明文）
    RUNTIME_SECRET_NAME = 'todo4ai-runtime-keys'
    # 工作区共享卷（协作 Agent 的文件产物通道；ReadWriteMany）
    SHARED_WORKSPACE_PVC_PREFIX = 'todo4ai-ws'
    # 单工作区同时运行的 Agent Pod 上限（防成本失控；0 = 不限，可被 Config 覆盖）
    MAX_PODS_PER_WORKSPACE = 10
    # Agent Pod 空闲回收阈值（分钟；0 = 不回收，可被 Config/工作区设置覆盖）
    POD_IDLE_TIMEOUT_MINUTES = 30

    # 沙箱资源配置（定义移至 services/cloud_runtime/manifests.py）
    SANDBOX_RESOURCES = manifests.SANDBOX_RESOURCES

    def __init__(self):
        self.namespace = getattr(Config, 'K8S_AGENT_NAMESPACE', None) or 'todo4ai-agents'
        self.k8s_client = None
        self.core_v1 = None
        self._init_k8s_client()

    def _init_k8s_client(self):
        """初始化 K8s 客户端"""
        try:
            # 尝试加载集群内配置
            kubernetes.config.load_incluster_config()
            logger.info("controller.k8s_loaded source=incluster")
        except kubernetes.config.config_exception.ConfigException:
            # 回退到本地配置
            kubernetes.config.load_kube_config()
            logger.info("controller.k8s_loaded source=kubeconfig")

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

        policy = self._agent_policy(agent)
        # 凭据与共享卷在 Pod 引用它们之前必须存在（幂等）
        self.ensure_runtime_secret(agent.id, agent_key)
        if policy.get('shared_workspace'):
            self.ensure_workspace_shared_pvc(agent.workspace_id)

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
                "controller.pod_created pod=%s agent_id=%s namespace=%s",
                pod_name, agent.id, self.namespace,
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
                "controller.pod_create_failed pod=%s error=%s", pod_name, e,
            )
            raise RuntimeError(f"Failed to create pod: {e}")

    # ── Phase 1：多 Agent 云端协作的三个接缝（凭据 Secret 化 / 共享工作区卷 / 幂等确保在岗）──

    def _agent_policy(self, agent: Agent) -> dict:
        return manifests.agent_policy(agent)

    def _runtime_secret_field(self, agent_id: int) -> str:
        return manifests.runtime_secret_field(agent_id)

    def ensure_runtime_secret(self, agent_id: int, agent_key: str) -> str:
        """确保运行时 Secret 存在且包含该 Agent 的密钥；返回 Secret 名。

        Secret 为 per-namespace 聚合式（todo4ai-runtime-keys），key = agent-<id>。
        已存在且值一致时不做写操作（幂等）。
        """
        import base64

        secret_name = self.RUNTIME_SECRET_NAME
        field = self._runtime_secret_field(agent_id)
        encoded = base64.b64encode((agent_key or '').encode('utf-8')).decode('utf-8')
        try:
            secret = self.core_v1.read_namespaced_secret(secret_name, self.namespace)
            data = dict(secret.data or {})
            if data.get(field) == encoded:
                return secret_name
            data[field] = encoded
            self.core_v1.patch_namespaced_secret(
                name=secret_name, namespace=self.namespace, body={'data': data}
            )
            logger.info("controller.runtime_secret_patched agent_id=%s", agent_id)
            return secret_name
        except ApiException as e:
            if e.status != 404:
                raise
        self.core_v1.create_namespaced_secret(
            namespace=self.namespace,
            body=kubernetes.client.V1Secret(
                metadata=kubernetes.client.V1ObjectMeta(name=secret_name),
                type='Opaque',
                data={field: encoded},
            ),
        )
        logger.info("controller.runtime_secret_created agent_id=%s", agent_id)
        return secret_name

    def shared_workspace_pvc_name(self, workspace_id: int) -> str:
        return f'{self.SHARED_WORKSPACE_PVC_PREFIX}-{workspace_id}-shared'

    def ensure_workspace_shared_pvc(self, workspace_id: int) -> str:
        """确保工作区共享卷存在（协作 Agent 的文件产物通道，ReadWriteMany）。"""
        pvc_name = self.shared_workspace_pvc_name(workspace_id)
        try:
            self.core_v1.read_namespaced_persistent_volume_claim(
                name=pvc_name, namespace=self.namespace
            )
            return pvc_name
        except ApiException as e:
            if e.status != 404:
                raise
        storage_class = getattr(Config, 'K8S_SHARED_WORKSPACE_STORAGE_CLASS', None)
        spec_kwargs = {}
        if storage_class:
            spec_kwargs['storage_class_name'] = storage_class
        self.core_v1.create_namespaced_persistent_volume_claim(
            namespace=self.namespace,
            body=kubernetes.client.V1PersistentVolumeClaim(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=pvc_name,
                    labels={'app': 'todo4ai-workspace', 'workspace-id': str(workspace_id)},
                ),
                spec=kubernetes.client.V1PersistentVolumeClaimSpec(
                    access_modes=['ReadWriteMany'],
                    resources=V1ResourceRequirements(requests={'storage': '10Gi'}),
                    **spec_kwargs,
                ),
            ),
        )
        logger.info("controller.shared_pvc_created workspace_id=%s pvc=%s",
                    workspace_id, pvc_name)
        return pvc_name

    def _list_workspace_pods(self, workspace_id: int) -> List[V1Pod]:
        """工作区内全部 Agent Pod（含非 Running，用于配额判断）。"""
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f'app=todo4ai-agent,workspace-id={workspace_id}',
            )
            return pods.items
        except ApiException:
            return []

    def ensure_agent_pod(self, agent: Agent, agent_key: str, sandbox_profile: str = None) -> Dict:
        """幂等确保 Agent 的云端运行时在岗；编排派发前调用。

        - 已有 Running/Pending Pod：不动作（already_running）；
        - 工作区在岗 Pod 数达到上限：不创建（workspace_pod_limit，任务留在队列）；
        - 否则先确保 Secret / 共享卷（按策略），再 spawn。
        返回 {'status': already_running|created|workspace_pod_limit, ...}。
        """
        existing = self.get_agent_pod_status(agent.id)
        if existing and existing.get('phase') in ('Running', 'Pending'):
            return {'status': 'already_running', 'pod': existing}

        # 工作区配额：DB 设置 > Config > 类默认（services/workspace_runtime_policy.py）
        from services.workspace_runtime_policy import get_workspace_runtime_setting
        cap = get_workspace_runtime_setting(self, agent.workspace_id)['max_pods']
        if cap > 0:
            running = [
                p for p in self._list_workspace_pods(agent.workspace_id)
                if p.status and p.status.phase in ('Running', 'Pending')
            ]
            if len(running) >= cap:
                logger.warning(
                    "controller.workspace_pod_limit workspace_id=%s running=%s cap=%s",
                    agent.workspace_id, len(running), cap,
                )
                return {'status': 'workspace_pod_limit', 'running': len(running), 'cap': cap}

        policy = self._agent_policy(agent)
        result = self.spawn_agent_pod(
            agent=agent,
            agent_key=agent_key,
            sandbox_profile=sandbox_profile or agent.sandbox_profile or 'standard',
        )
        return {'status': 'created', 'pod': result}

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
            logger.warning("controller.no_pods_found agent_id=%s", agent_id)
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
                    "controller.pod_deleted pod=%s agent_id=%s",
                    pod.metadata.name, agent_id,
                )
            except ApiException as e:
                logger.error(
                    "controller.pod_delete_failed pod=%s error=%s",
                    pod.metadata.name, e,
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
            logger.error("controller.list_pods_failed error=%s", e)
            return []

    def _build_pod(self, name: str, agent: Agent, agent_key: str,
                   sandbox_profile: str) -> V1Pod:
        """构建 Agent Pod 清单（声明式部分见 services/cloud_runtime/manifests.py）。"""
        return manifests.build_pod(
            name=name, agent=agent, secret_name=self.RUNTIME_SECRET_NAME,
            sandbox_profile=sandbox_profile,
        )

    def _build_env_vars(self, agent: Agent, agent_key: str) -> List:
        """环境变量（AGENT_KEY 走 SecretKeyRef，见 manifests.build_env_vars）。"""
        return manifests.build_env_vars(agent)

    def _get_runtime_type(self, agent: Agent) -> str:
        return manifests.runtime_type(agent)

    def _get_network_mode(self, agent: Agent) -> str:
        return manifests.network_mode(agent)

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

    def _list_all_agent_pods(self) -> List[V1Pod]:
        """命名空间内全部 Agent Pod（空闲回收巡检用）"""
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector='app=todo4ai-agent'
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
        from models.project import Project
        from api.agent_common import generate_id, now_utc
        from datetime import timedelta

        # owner_id 是用户 ID、workspace_id 是组织 ID，二者属不同 ID 空间；
        # 任务的工作区归属以其所属项目的 organization_id 为准。
        org_id = None
        if task.project_id:
            org_id = db.session.get(Project, task.project_id).organization_id
        agent = None
        if org_id is not None:
            agent = Agent.query.filter_by(
                workspace_id=org_id,
                runner_enabled=True,
                status='ACTIVE'
            ).first()
        if agent is None:
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

        # Push task to connected agent via WebSocket
        try:
            from api.agent_runtime_websocket import push_task_to_agent
            task_data = {
                'task_id': task.id,
                'attempt_id': attempt_id,
                'lease_id': lease_id,
                'payload': {
                    'title': task.title,
                    'content': task.content,
                    'prompt': task.title or task.content or '',
                },
                'project_id': task.project_id,
                'priority': str(task.priority) if task.priority else None,
                'created_at': task.created_at.isoformat() if task.created_at else None,
                'workspace_id': agent.workspace_id,
            }
            push_task_to_agent(agent.id, task_data)
        except Exception as e:
            logger.warning("websocket.push_failed error=%s", e)


# 单例实例
_controller: Optional[AgentRuntimeController] = None


def get_agent_controller() -> AgentRuntimeController:
    """获取控制器单例"""
    global _controller
    if _controller is None:
        _controller = AgentRuntimeController()
    return _controller
