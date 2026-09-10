"""引擎注册表：Engine（跑什么）× ExecutionEnvironment（跑在哪）两条正交轴的「引擎轴」。

执行环境（RuntimeProvider 后端：k8s/docker/compose/baremetal/remote）负责
Agent 运行时的生命周期（创建/终止/状态/配额/回收）；引擎负责在运行时内部
执行任务（agent-runtime 的 cli_engines.py 按注入的 CLI_AGENT_ENGINE 调用
claude/codex/opencode 等 CLI 或 SDK 引擎）。

本模块是引擎的**单一事实来源**：
- 引擎 → 镜像映射（各环境后端共用，k8s Pod 与 docker/compose 容器同图）；
- 引擎解析（Agent 沙箱策略声明的 CLI 引擎优先，否则按 llm_provider 别名映射，
  兜底 custom）；
- 引擎任务环境变量（CLI_AGENT_ENGINE / LLM_* / 超时心跳等，环境无关，
  由任意后端注入到运行时内部）。

新增引擎 = 在 ENGINE_SPECS 加一个 EngineSpec；新增执行环境不感知引擎细节，
只调 resolve_engine / engine_task_env。
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class EngineSpec:
    """一个可执行引擎的声明式描述。"""

    key: str                    # 归一化引擎键（= pod label runtime-type / 状态 dict runtime_type）
    label: str                  # 展示名
    kind: str                   # 'cli'（包装编码 CLI）| 'sdk'（LLM SDK 引擎）
    image: str                  # 运行时镜像（所有环境后端共用）
    cli_flag: Optional[str]     # 沙箱策略 sandbox_policy.cli_engine 的合法取值；None=非 CLI 引擎
    provider_aliases: tuple     # 命中该引擎的 llm_provider 别名（小写）


ENGINE_SPECS = (
    EngineSpec('openai', 'OpenAI SDK 引擎', 'sdk',
               'todo4ai/agent-openai:latest', None, ('openai',)),
    EngineSpec('anthropic', 'Anthropic SDK 引擎', 'sdk',
               'todo4ai/agent-claude:latest', None, ('anthropic', 'claude')),
    EngineSpec('google', 'Gemini SDK 引擎', 'sdk',
               'todo4ai/agent-gemini:latest', None, ('google', 'gemini')),
    EngineSpec('ollama', 'Ollama 本地引擎', 'sdk',
               'todo4ai/agent-ollama:latest', None, ('ollama', 'local')),
    EngineSpec('claude', 'Claude Code CLI', 'cli',
               'todo4ai/agent-cli-agents:latest', 'claude', ()),
    EngineSpec('codex', 'Codex CLI', 'cli',
               'todo4ai/agent-cli-agents:latest', 'codex', ()),
    EngineSpec('opencode', 'OpenCode CLI', 'cli',
               'todo4ai/agent-cli-agents:latest', 'opencode', ()),
    EngineSpec('custom', '自定义引擎', 'sdk',
               'todo4ai/agent-runtime:latest', 'custom', ()),
)

_ENGINES: Dict[str, EngineSpec] = {spec.key: spec for spec in ENGINE_SPECS}

# 沙箱策略 cli_engine 合法取值 → 引擎键
CLI_ENGINE_FLAGS: Dict[str, str] = {
    spec.cli_flag: spec.key for spec in ENGINE_SPECS if spec.cli_flag
}

DEFAULT_ENGINE_KEY = 'custom'


def get_engine(key) -> Optional[EngineSpec]:
    return _ENGINES.get((key or '').strip().lower()) or None


def list_engines():
    """全部引擎（供 API/文档展示）。"""
    return list(ENGINE_SPECS)


def resolve_engine(agent) -> EngineSpec:
    """Agent → 引擎：沙箱策略显式声明的 CLI 引擎优先，否则按 llm_provider 别名，兜底 custom。"""
    policy = getattr(agent, 'sandbox_policy', None) or {}
    declared = (policy.get('cli_engine') or '').strip().lower()
    if declared in CLI_ENGINE_FLAGS:
        return _ENGINES[CLI_ENGINE_FLAGS[declared]]

    provider = (getattr(agent, 'llm_provider', None) or 'openai').strip().lower()
    for spec in ENGINE_SPECS:
        if provider in spec.provider_aliases:
            return spec
    return _ENGINES[DEFAULT_ENGINE_KEY]


def engine_image(engine_key: str, fallback: str = None) -> str:
    """引擎键 → 镜像；未知键回退 fallback（如部署级 DOCKER_RUNTIME_IMAGE）。"""
    spec = get_engine(engine_key)
    if spec:
        return spec.image
    return fallback or _ENGINES[DEFAULT_ENGINE_KEY].image


def engine_task_env(agent) -> Dict[str, str]:
    """引擎/任务相关的环境变量（环境无关；凭据与回连地址由各环境后端自行注入）。

    CLI_AGENT_ENGINE 只在沙箱策略显式声明时注入（与既有行为一致：
    纯 LLM 供应商 Agent 不应被误设 CLI 引擎键）。
    """
    env = {
        'LLM_PROVIDER': getattr(agent, 'llm_provider', None) or 'openai',
        'LLM_MODEL': getattr(agent, 'llm_model', None) or 'gpt-4',
        'SANDBOX_MODE': getattr(agent, 'sandbox_profile', None) or 'standard',
        'SANDBOX_NETWORK_MODE': ((getattr(agent, 'sandbox_policy', None) or {})
                                 .get('network_mode', 'isolated')),
        'MAX_CONCURRENT_TASKS': str(getattr(agent, 'max_concurrency', None) or 1),
        'TASK_TIMEOUT_SECONDS': str(getattr(agent, 'timeout_seconds', None) or 1800),
        'HEARTBEAT_INTERVAL_SECONDS': str(
            getattr(agent, 'heartbeat_interval_seconds', None) or 20),
        'LOG_LEVEL': 'INFO',
    }
    policy = getattr(agent, 'sandbox_policy', None) or {}
    declared = (policy.get('cli_engine') or '').strip().lower()
    if declared in CLI_ENGINE_FLAGS:
        env['CLI_AGENT_ENGINE'] = declared
    return env
