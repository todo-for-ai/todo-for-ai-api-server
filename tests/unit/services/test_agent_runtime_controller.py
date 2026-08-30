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
    assert env_vars_by_name["AGENT_KEY"].value == "agk_test_123"
    assert env_vars_by_name["API_BASE_URL"].value == "https://api.todo-for-ai.com/todo-for-ai/api/v1"
    assert env_vars_by_name["LLM_PROVIDER"].value == "openai"
    assert env_vars_by_name["MAX_CONCURRENT_TASKS"].value == "2"
    assert env_vars_by_name["TASK_TIMEOUT_SECONDS"].value == "600"
    assert env_vars_by_name["HEARTBEAT_INTERVAL_SECONDS"].value == "20"
    assert env_vars_by_name["LLM_API_KEY"].value_from.secret_key_ref.key == "openai"


def test_spawn_agent_pod_calls_k8s_and_returns_pod_metadata(controller):
    """Controller should create a pod with expected labels and runtime image."""
    agent = _make_agent(llm_provider="claude", llm_model="claude-3-7-sonnet")

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
    assert env_vars_by_name["AGENT_KEY"].value == "agk_runtime_abc"
    assert env_vars_by_name["API_BASE_URL"].value == "http://api.internal/todo-for-ai/api/v1"


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
