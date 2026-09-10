"""引擎注册表（services/runtime_env/engines.py）回归测试。

覆盖：引擎→镜像映射与历史字典逐键一致（向后兼容护栏）、
解析矩阵（策略声明 CLI 引擎优先 / llm_provider 别名 / 兜底 custom）、
引擎任务环境变量注入规则、manifests 与 docker 后端对注册表的消费。
"""

from types import SimpleNamespace

from services.cloud_runtime import manifests
from services.runtime_env.engines import (
    DEFAULT_ENGINE_KEY,
    ENGINE_SPECS,
    CLI_ENGINE_FLAGS,
    engine_image,
    engine_task_env,
    get_engine,
    resolve_engine,
)


def _agent(**kwargs):
    defaults = dict(
        id=7,
        llm_provider='openai',
        llm_model='gpt-4',
        sandbox_profile='standard',
        sandbox_policy=None,
        max_concurrency=1,
        timeout_seconds=1800,
        heartbeat_interval_seconds=20,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestRegistry:
    def test_images_match_legacy_mapping(self):
        # 向后兼容护栏：注册表派生的镜像映射必须与历史硬编码逐键一致
        assert manifests.RUNTIME_IMAGES == {
            'openai': 'todo4ai/agent-openai:latest',
            'anthropic': 'todo4ai/agent-claude:latest',
            'google': 'todo4ai/agent-gemini:latest',
            'ollama': 'todo4ai/agent-ollama:latest',
            'claude': 'todo4ai/agent-cli-agents:latest',
            'codex': 'todo4ai/agent-cli-agents:latest',
            'opencode': 'todo4ai/agent-cli-agents:latest',
            'custom': 'todo4ai/agent-runtime:latest',
        }

    def test_cli_flags_cover_expected_engines(self):
        assert CLI_ENGINE_FLAGS == {
            'claude': 'claude', 'codex': 'codex',
            'opencode': 'opencode', 'custom': 'custom',
        }

    def test_get_engine_case_insensitive_and_unknown_none(self):
        assert get_engine('CODEX').key == 'codex'
        assert get_engine('nomad') is None
        assert get_engine(None) is None

    def test_specs_have_unique_keys_and_flags(self):
        keys = [s.key for s in ENGINE_SPECS]
        assert len(keys) == len(set(keys))
        flags = [s.cli_flag for s in ENGINE_SPECS if s.cli_flag]
        assert len(flags) == len(set(flags))


class TestResolveEngine:
    def test_cli_policy_wins_over_provider(self):
        agent = _agent(llm_provider='openai', sandbox_policy={'cli_engine': 'codex'})
        spec = resolve_engine(agent)
        assert spec.key == 'codex'
        assert spec.kind == 'cli'

    def test_provider_aliases(self):
        cases = {
            'openai': 'openai',
            'anthropic': 'anthropic',
            'claude': 'anthropic',
            'google': 'google',
            'gemini': 'google',
            'ollama': 'ollama',
            'local': 'ollama',
        }
        for provider, expected in cases.items():
            assert resolve_engine(_agent(llm_provider=provider)).key == expected

    def test_unknown_provider_falls_back_to_custom(self):
        assert resolve_engine(_agent(llm_provider='mistral')).key == DEFAULT_ENGINE_KEY
        assert resolve_engine(_agent(llm_provider=None)).key == 'openai'

    def test_invalid_cli_flag_ignored(self):
        agent = _agent(llm_provider='anthropic', sandbox_policy={'cli_engine': 'windsurf'})
        assert resolve_engine(agent).key == 'anthropic'


class TestEngineImage:
    def test_known_and_fallback(self):
        assert engine_image('claude') == 'todo4ai/agent-cli-agents:latest'
        assert engine_image('nope', fallback='todo4ai/agent-runtime:latest') \
            == 'todo4ai/agent-runtime:latest'


class TestEngineTaskEnv:
    def test_cli_engine_declared_injected(self):
        env = engine_task_env(_agent(
            llm_provider='anthropic', llm_model='claude-x',
            sandbox_policy={'cli_engine': 'claude', 'network_mode': 'bridged'},
            max_concurrency=3, timeout_seconds=600, heartbeat_interval_seconds=10,
        ))
        assert env['CLI_AGENT_ENGINE'] == 'claude'
        assert env['LLM_PROVIDER'] == 'anthropic'
        assert env['SANDBOX_NETWORK_MODE'] == 'bridged'
        assert env['MAX_CONCURRENT_TASKS'] == '3'
        assert env['TASK_TIMEOUT_SECONDS'] == '600'
        assert env['HEARTBEAT_INTERVAL_SECONDS'] == '10'

    def test_no_cli_engine_by_default(self):
        env = engine_task_env(_agent())
        assert 'CLI_AGENT_ENGINE' not in env
        assert env['SANDBOX_NETWORK_MODE'] == 'isolated'

    def test_invalid_cli_flag_not_injected(self):
        env = engine_task_env(_agent(sandbox_policy={'cli_engine': 'windsurf'}))
        assert 'CLI_AGENT_ENGINE' not in env


class TestBackendConsumption:
    def test_manifests_runtime_type_delegates(self):
        cases = [
            (_agent(sandbox_policy={'cli_engine': 'opencode'}), 'opencode'),
            (_agent(llm_provider='google'), 'google'),
            (_agent(llm_provider='bogus'), 'custom'),
        ]
        for agent, expected in cases:
            assert manifests.runtime_type(agent) == expected

    def test_manifests_env_vars_use_registry(self):
        try:
            env_vars = manifests.build_env_vars(_agent(
                llm_provider='anthropic',
                sandbox_policy={'cli_engine': 'claude'},
            ))
        except ImportError:
            import pytest
            pytest.skip('kubernetes package not installed')
        names = {var.name for var in env_vars}
        assert {'AGENT_KEY', 'API_BASE_URL', 'LLM_PROVIDER', 'LLM_MODEL',
                'SANDBOX_MODE', 'SANDBOX_NETWORK_MODE', 'MAX_CONCURRENT_TASKS',
                'TASK_TIMEOUT_SECONDS', 'HEARTBEAT_INTERVAL_SECONDS', 'LOG_LEVEL',
                'CLI_AGENT_ENGINE', 'LLM_API_KEY'} <= names
        agent_key_var = next(v for v in env_vars if v.name == 'AGENT_KEY')
        assert agent_key_var.value is None          # 明文绝不落 Pod spec
        assert agent_key_var.value_from is not None  # 走 SecretKeyRef

    def test_docker_build_runtime_env_shared_subset(self):
        from services.runtime_env.docker_provider import build_runtime_env

        env = build_runtime_env(_agent(sandbox_policy={'cli_engine': 'codex'}), 'ak')
        assert env['AGENT_KEY'] == 'ak'
        assert 'API_BASE_URL' in env
        assert env['CLI_AGENT_ENGINE'] == 'codex'
        assert env['LLM_PROVIDER'] == 'openai'

    def test_docker_image_for_fallback(self):
        from services.runtime_env.docker_provider import DockerRuntimeProvider

        provider = DockerRuntimeProvider(image='todo4ai/agent-runtime:latest')
        assert provider.image_for('codex') == 'todo4ai/agent-cli-agents:latest'
        assert provider.image_for('nope') == 'todo4ai/agent-runtime:latest'
