"""Unit tests for AgentRuntimeController pod lifecycle behavior."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException


def _make_agent(**overrides):
    defaults = {
        "id": 11,
        "workspace_id": 22,
        "name": "Test Agent",
        "llm_provider": "openai",
        "llm_model": "gpt-4",
        "sandbox_profile": "standard",
        "sandbox_policy": {"network_mode": "isolated"},
        "max_concurrency": 2,
        "timeout_seconds": 600,
        "heartbeat_interval_seconds": 20,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _env_map(env_vars):
    return {item.name: item for item in env_vars}


@pytest.fixture
def controller():
    """Build a controller instance without touching real kubeconfig."""
    from services.agent_runtime_controller import AgentRuntimeController

    with patch.object(AgentRuntimeController, "_init_k8s_client", return_value=None):
        instance = AgentRuntimeController()

    instance.namespace = "test-namespace"
    instance.core_v1 = MagicMock()
    return instance


def test_build_env_vars_falls_back_to_default_api_base_url(controller):
    """Agent runtime env injection should not fail when Config.API_BASE_URL is missing."""
    agent = _make_agent(llm_provider="openai")

    with patch("services.agent_runtime_controller.Config", new=SimpleNamespace()):
        env_vars = controller._build_env_vars(agent=agent, agent_key="agk_test_123")

    env_vars_by_name = _env_map(env_vars)
    # 凭据 Secret 化：AGENT_KEY 以 secretKeyRef 注入，Pod spec 不落明文
    agent_key_env = env_vars_by_name["AGENT_KEY"]
    assert agent_key_env.value is None
    assert agent_key_env.value_from.secret_key_ref.name == controller.RUNTIME_SECRET_NAME
    assert agent_key_env.value_from.secret_key_ref.key == "agent-11"
    assert env_vars_by_name["API_BASE_URL"].value == "https://api.todo-for-ai.com/todo-for-ai/api/v1"
    assert env_vars_by_name["LLM_PROVIDER"].value == "openai"
    assert env_vars_by_name["MAX_CONCURRENT_TASKS"].value == "2"
    assert env_vars_by_name["TASK_TIMEOUT_SECONDS"].value == "600"
    assert env_vars_by_name["HEARTBEAT_INTERVAL_SECONDS"].value == "20"
    assert env_vars_by_name["LLM_API_KEY"].value_from.secret_key_ref.key == "openai"


def test_env_injects_cli_agent_engine_from_policy(controller):
    """sandbox_policy.cli_engine 应注入 CLI_AGENT_ENGINE（cli-agents 镜像的引擎选择）。"""
    agent = _make_agent(
        llm_provider="openai",
        sandbox_policy={"network_mode": "isolated", "cli_engine": "codex"},
    )
    env_vars_by_name = _env_map(controller._build_env_vars(agent=agent, agent_key="agk_x"))
    assert env_vars_by_name["CLI_AGENT_ENGINE"].value == "codex"


def test_runtime_type_prefers_cli_engine_policy(controller):
    """策略声明的 CLI 引擎优先于 LLM 供应商映射，镜像走 cli-agents。"""
    agent = _make_agent(
        llm_provider="openai",
        sandbox_policy={"network_mode": "isolated", "cli_engine": "opencode"},
    )
    assert controller._get_runtime_type(agent) == "opencode"
    assert controller.RUNTIME_IMAGES["opencode"] == "todo4ai/agent-cli-agents:latest"
    # 无策略时保持供应商映射
    assert controller._get_runtime_type(_make_agent()) == "openai"


def _secret_404(controller):
    controller.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)


def test_spawn_agent_pod_calls_k8s_and_returns_pod_metadata(controller):
    """Controller should create a pod with expected labels and runtime image."""
    agent = _make_agent(llm_provider="claude", llm_model="claude-3-7-sonnet")
    _secret_404(controller)

    created_pod = MagicMock()
    created_pod.metadata.uid = "pod_uid_123"
    controller.core_v1.create_namespaced_pod.return_value = created_pod

    with patch(
        "services.agent_runtime_controller.uuid4",
        return_value=SimpleNamespace(hex="12345678abcdef00"),
    ), patch(
        "services.agent_runtime_controller.Config",
        new=SimpleNamespace(API_BASE_URL="http://api.internal/todo-for-ai/api/v1"),
    ):
        result = controller.spawn_agent_pod(
            agent=agent,
            agent_key="agk_runtime_abc",
            sandbox_profile="minimal",
        )

    assert result["pod_name"] == "agent-11-12345678"
    assert result["pod_uid"] == "pod_uid_123"
    assert result["status"] == "creating"
    assert result["agent_id"] == 11

    kwargs = controller.core_v1.create_namespaced_pod.call_args.kwargs
    assert kwargs["namespace"] == "test-namespace"

    pod_spec = kwargs["body"]
    assert pod_spec.metadata.labels["agent-id"] == "11"
    assert pod_spec.metadata.labels["workspace-id"] == "22"
    assert pod_spec.metadata.labels["runtime-type"] == "anthropic"

    container = pod_spec.spec.containers[0]
    assert container.name == "agent-runtime"
    assert container.image == controller.RUNTIME_IMAGES["anthropic"]

    env_vars_by_name = _env_map(container.env)
    assert env_vars_by_name["AGENT_KEY"].value is None
    assert env_vars_by_name["AGENT_KEY"].value_from.secret_key_ref.key == "agent-11"
    assert env_vars_by_name["API_BASE_URL"].value == "http://api.internal/todo-for-ai/api/v1"

    # 凭据在 Pod 引用前已写入 Secret
    controller.core_v1.create_namespaced_secret.assert_called_once()


def test_build_pod_mounts_shared_workspace_when_policy_enabled(controller):
    """策略开启 shared_workspace 时挂载工作区级 ReadWriteMany PVC。"""
    agent = _make_agent(
        sandbox_policy={"network_mode": "isolated", "shared_workspace": True},
    )
    pod = controller._build_pod(
        name="agent-11-shared", agent=agent, agent_key="agk_x", sandbox_profile="standard"
    )
    mounts = [m.name for m in pod.spec.containers[0].volume_mounts]
    volumes = [v.name for v in pod.spec.volumes]
    assert "shared-workspace" in mounts and "shared-workspace" in volumes
    claim = next(v for v in pod.spec.volumes if v.name == "shared-workspace")
    assert claim.persistent_volume_claim.claim_name == "todo4ai-ws-22-shared"

    # 未开启策略时不挂载
    plain = controller._build_pod(
        name="agent-11-plain", agent=_make_agent(), agent_key="agk_x", sandbox_profile="standard"
    )
    assert "shared-workspace" not in [m.name for m in plain.spec.containers[0].volume_mounts]


def test_ensure_runtime_secret_creates_then_patches(controller):
    """Secret 幂等写入：404 创建、值不一致 patch、一致则零写操作。"""
    import base64

    agent_id, key = 11, "agk_runtime_abc"
    encoded = base64.b64encode(key.encode()).decode()

    # 404 → 创建
    _secret_404(controller)
    controller.ensure_runtime_secret(agent_id, key)
    created = controller.core_v1.create_namespaced_secret.call_args.kwargs
    assert created["namespace"] == "test-namespace"
    assert created["body"].data == {"agent-11": encoded}

    # 值不一致 → patch
    controller.core_v1.reset_mock()
    controller.core_v1.read_namespaced_secret.side_effect = None
    existing = MagicMock()
    existing.data = {"agent-11": base64.b64encode(b"old").decode()}
    controller.core_v1.read_namespaced_secret.return_value = existing
    controller.ensure_runtime_secret(agent_id, key)
    controller.core_v1.patch_namespaced_secret.assert_called_once()

    # 一致 → 零写操作（模拟 patch 已生效）
    existing.data = {"agent-11": encoded}
    controller.core_v1.reset_mock()
    controller.ensure_runtime_secret(agent_id, key)
    controller.core_v1.patch_namespaced_secret.assert_not_called()
    controller.core_v1.create_namespaced_secret.assert_not_called()


def test_ensure_workspace_shared_pvc_creates_once(controller):
    """共享卷幂等创建：已存在零写操作。"""
    controller.core_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
    name = controller.ensure_workspace_shared_pvc(22)
    assert name == "todo4ai-ws-22-shared"
    assert controller.core_v1.create_namespaced_persistent_volume_claim.call_count == 1

    controller.core_v1.read_namespaced_persistent_volume_claim.side_effect = None
    controller.core_v1.read_namespaced_persistent_volume_claim.return_value = MagicMock()
    controller.core_v1.reset_mock()
    controller.ensure_workspace_shared_pvc(22)
    controller.core_v1.create_namespaced_persistent_volume_claim.assert_not_called()


def test_ensure_agent_pod_skips_when_already_running(controller):
    """编排联动幂等：Pod 已在岗时不重复创建。"""
    running = MagicMock()
    running.status.phase = "Running"
    controller.get_agent_pod_status = MagicMock(return_value={"phase": "Running"})

    result = controller.ensure_agent_pod(_make_agent(), "agk_x")
    assert result["status"] == "already_running"
    controller.core_v1.create_namespaced_pod.assert_not_called()
    assert running  # silence unused


def test_ensure_agent_pod_respects_workspace_pod_cap(controller):
    """工作区在岗 Pod 达上限时不创建（成本护栏），返回 workspace_pod_limit。"""
    controller.get_agent_pod_status = MagicMock(return_value=None)
    pods = []
    for _ in range(controller.MAX_PODS_PER_WORKSPACE):
        pod = MagicMock()
        pod.status.phase = "Running"
        pods.append(pod)
    controller.core_v1.list_namespaced_pod.return_value = MagicMock(items=pods)

    result = controller.ensure_agent_pod(_make_agent(), "agk_x")
    assert result["status"] == "workspace_pod_limit"
    controller.core_v1.create_namespaced_pod.assert_not_called()


def test_ensure_agent_pod_creates_when_needed(controller):
    """不在岗且未超限时：确保 Secret 后创建 Pod。"""
    controller.get_agent_pod_status = MagicMock(return_value=None)
    controller.core_v1.list_namespaced_pod.return_value = MagicMock(items=[])
    controller.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)
    created_pod = MagicMock()
    created_pod.metadata.uid = "uid_new"
    controller.core_v1.create_namespaced_pod.return_value = created_pod

    result = controller.ensure_agent_pod(_make_agent(), "agk_new")
    assert result["status"] == "created"
    assert controller.core_v1.create_namespaced_secret.call_count == 1
    assert controller.core_v1.create_namespaced_pod.call_count == 1


def test_spawn_agent_pod_raises_runtime_error_on_k8s_failure(controller):
    """Controller should wrap Kubernetes API failures into RuntimeError."""
    agent = _make_agent()
    controller.core_v1.create_namespaced_pod.side_effect = ApiException(
        status=403, reason="forbidden"
    )

    with patch(
        "services.agent_runtime_controller.Config",
        new=SimpleNamespace(API_BASE_URL="http://api.internal/todo-for-ai/api/v1"),
    ), patch("services.agent_runtime_controller.logger.error"):
        with pytest.raises(RuntimeError, match="Failed to create pod"):
            controller.spawn_agent_pod(
                agent=agent,
                agent_key="agk_runtime_abc",
                sandbox_profile="standard",
            )
