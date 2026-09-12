"""lease_policy 单元测试：租约 TTL 解析优先级与钳制。

背景：派发路径此前散落 60s 硬编码，续约一抖动就 LEASE_EXPIRED 把
长跑任务的工作成果作废；统一从 Agent 激活配置/环境解析并钳制。
"""

import pytest

from services.lease_policy import (
    DEFAULT_LEASE_TTL_SECONDS,
    MAX_LEASE_TTL_SECONDS,
    MIN_LEASE_TTL_SECONDS,
    effective_lease_ttl,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('LEASE_DURATION_SECONDS', raising=False)


@pytest.fixture
def agent(db_session, agent_factory):
    a = agent_factory(runner_enabled=True)
    yield a
    # 先清配置行再让 agent_factory 删 agent，避免 FK 置空冲突
    from models.agent_runtime_monitor import AgentRuntimeConfig
    AgentRuntimeConfig.query.filter_by(agent_id=a.id).delete()
    db_session.commit()


def test_default_ttl_without_config(agent):
    assert effective_lease_ttl(agent_id=agent.id) == DEFAULT_LEASE_TTL_SECONDS == 120


def test_no_args_uses_env_default():
    assert effective_lease_ttl() == DEFAULT_LEASE_TTL_SECONDS


def test_env_override(monkeypatch):
    monkeypatch.setenv('LEASE_DURATION_SECONDS', '300')
    assert effective_lease_ttl() == 300


def test_env_clamped_to_bounds(monkeypatch):
    monkeypatch.setenv('LEASE_DURATION_SECONDS', '5')
    assert effective_lease_ttl() == MIN_LEASE_TTL_SECONDS
    monkeypatch.setenv('LEASE_DURATION_SECONDS', '99999')
    assert effective_lease_ttl() == MAX_LEASE_TTL_SECONDS


def test_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv('LEASE_DURATION_SECONDS', 'abc')
    assert effective_lease_ttl() == DEFAULT_LEASE_TTL_SECONDS


def test_agent_active_config_wins(db_session, agent):
    from models.agent_runtime_monitor import AgentRuntimeConfig

    db_session.add(AgentRuntimeConfig(
        agent_id=agent.id, workspace_id=agent.workspace_id,
        version=1, is_active=True, lease_duration_seconds=900,
    ))
    db_session.commit()
    assert effective_lease_ttl(agent_id=agent.id) == 900


def test_agent_config_clamped(db_session, agent):
    from models.agent_runtime_monitor import AgentRuntimeConfig

    db_session.add(AgentRuntimeConfig(
        agent_id=agent.id, workspace_id=agent.workspace_id,
        version=1, is_active=True, lease_duration_seconds=100000,
    ))
    db_session.commit()
    assert effective_lease_ttl(agent_id=agent.id) == MAX_LEASE_TTL_SECONDS


def test_inactive_config_ignored(db_session, agent):
    from models.agent_runtime_monitor import AgentRuntimeConfig

    db_session.add(AgentRuntimeConfig(
        agent_id=agent.id, workspace_id=agent.workspace_id,
        version=9, is_active=False, lease_duration_seconds=3600,
    ))
    db_session.commit()
    assert effective_lease_ttl(agent_id=agent.id) == DEFAULT_LEASE_TTL_SECONDS


def test_config_lookup_failure_falls_back(monkeypatch, agent):
    """配置读取抛异常时回落环境默认，绝不阻断派发路径。"""
    from models.agent_runtime_monitor import AgentRuntimeConfig

    class _Boom:
        def filter_by(self, *a, **kw):
            raise RuntimeError('db down')

    monkeypatch.setattr(AgentRuntimeConfig, 'query', _Boom())
    monkeypatch.setenv('LEASE_DURATION_SECONDS', '300')
    assert effective_lease_ttl(agent_id=agent.id) == 300
    monkeypatch.undo()  # 先还原类属性，agent fixture teardown 还要用它清配置行
