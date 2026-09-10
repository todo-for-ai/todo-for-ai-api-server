"""remote 后端（services/runtime_env/remote_provider.py）与按 Agent 解析回归测试。

覆盖：连接注册表/心跳新鲜度 → 归一化 phase 映射、spawn 注册语义、
terminate 下发 shutdown、列表排除 managed_runner、工厂按执行模式解析、
ensure_runtime 模板在 remote 上的幂等、WS 元数据合并。
DB 交互走真实 Agent 模型（sqlite 会话级隔离）。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.config import Config
from services.runtime_env import (
    get_runtime_provider,
    get_runtime_provider_for_agent,
    reset_runtime_provider_cache,
)
from services.runtime_env.docker_provider import DockerRuntimeProvider
from services.runtime_env.remote_provider import RemoteRuntimeProvider


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    reset_runtime_provider_cache()
    yield
    reset_runtime_provider_cache()


@pytest.fixture()
def _clear_connected():
    from api import agent_runtime_websocket as ws

    ws._CONNECTED_AGENT_IDS.clear()
    yield ws
    ws._CONNECTED_AGENT_IDS.clear()


def _touch(agent, minutes_ago=0):
    agent.last_seen_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
    from models import db
    db.session.commit()
    return agent


class TestFactory:
    def test_remote_kind_selectable(self):
        assert get_runtime_provider('remote').name == 'remote'

    def test_resolver_managed_runner_uses_global_backend(self, monkeypatch):
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'docker')
        agent = SimpleNamespace(execution_mode='managed_runner')
        assert isinstance(get_runtime_provider_for_agent(agent), DockerRuntimeProvider)

    def test_resolver_external_pull_uses_remote(self):
        for mode in ('external_pull', None, ''):
            agent = SimpleNamespace(execution_mode=mode)
            assert isinstance(get_runtime_provider_for_agent(agent),
                              RemoteRuntimeProvider)


class TestRemoteStatus:
    def test_fresh_heartbeat_is_running(self, db_session, agent_factory, _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = _touch(agent_factory(
            llm_provider='anthropic', runner_enabled=False, execution_mode='external_pull'))
        status = provider.get_runtime_status(agent.id)
        assert status['phase'] == 'Running'
        assert status['environment'] == 'remote'
        assert status['online'] is True
        assert status['runtime_type'] == 'anthropic'
        assert status['runtime_id'] == f'remote-{agent.id}'

    def test_connected_registry_overrides_stale_heartbeat(
            self, db_session, agent_factory, _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = _touch(agent_factory(runner_enabled=True), minutes_ago=30)
        _clear_connected._CONNECTED_AGENT_IDS.add(agent.id)
        assert provider.get_runtime_status(agent.id)['phase'] == 'Running'

    def test_stale_enabled_agent_is_pending(self, db_session, agent_factory,
                                            _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = _touch(agent_factory(runner_enabled=True), minutes_ago=30)
        status = provider.get_runtime_status(agent.id)
        assert status['phase'] == 'Pending'
        assert status['online'] is False

    def test_stale_disabled_agent_is_unknown(self, db_session, agent_factory,
                                             _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = agent_factory(runner_enabled=False)
        agent.last_seen_at = None
        from models import db
        db.session.commit()
        assert provider.get_runtime_status(agent.id)['phase'] == 'Unknown'

    def test_address_comes_from_reported_meta(self, db_session, agent_factory,
                                              _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = agent_factory(config={'runtime_meta': {'host': 'macbook.local'}})
        _touch(agent)
        assert provider.get_runtime_status(agent.id)['address'] == 'macbook.local'

    def test_missing_agent_returns_none(self, _clear_connected):
        assert RemoteRuntimeProvider().get_runtime_status(987654321) is None


class TestRemoteSpawnTerminate:
    def test_spawn_offline_registers_pending(self, db_session, agent_factory,
                                             _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = agent_factory(runner_enabled=True, llm_provider='anthropic',
                              sandbox_policy={'cli_engine': 'codex'})
        agent.last_seen_at = None
        from models import db
        db.session.commit()
        result = provider.spawn(agent, 'ak')
        assert result['phase'] == 'Pending'
        assert result['runtime_type'] == 'codex'   # 引擎轴与本后端正交
        assert result['environment'] == 'remote'

    def test_spawn_online_returns_running(self, db_session, agent_factory,
                                          _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = _touch(agent_factory(runner_enabled=False))
        assert provider.spawn(agent, 'ak')['phase'] == 'Running'

    def test_ensure_runtime_idempotent_when_online(self, db_session, agent_factory,
                                                   _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = _touch(agent_factory(runner_enabled=True))
        result = provider.ensure_runtime(agent, 'ak')
        assert result['status'] == 'already_running'

    def test_terminate_online_sends_shutdown(self, db_session, agent_factory,
                                             _clear_connected, monkeypatch):
        provider = RemoteRuntimeProvider()
        agent = agent_factory(runner_enabled=True)
        _touch(agent)
        from api import agent_runtime_websocket as ws
        _clear_connected._CONNECTED_AGENT_IDS.add(agent.id)
        calls = []
        monkeypatch.setattr(ws, 'send_command_to_agent',
                            lambda agent_id, command, args=None: calls.append(command))
        assert provider.terminate(agent.id) is True
        assert calls == ['shutdown']

    def test_terminate_offline_returns_false(self, db_session, agent_factory,
                                             _clear_connected):
        provider = RemoteRuntimeProvider()
        agent = agent_factory(runner_enabled=True)
        agent.last_seen_at = None
        from models import db
        db.session.commit()
        assert provider.terminate(agent.id) is False


class TestRemoteList:
    def test_list_excludes_managed_and_filters_workspace(
            self, db_session, agent_factory, organization_factory, _clear_connected):
        from models import db

        provider = RemoteRuntimeProvider()
        org_a = organization_factory()
        org_b = organization_factory()
        managed = agent_factory(workspace_id=org_a.id,
                                execution_mode='managed_runner')
        remote_a = agent_factory(workspace_id=org_a.id,
                                 execution_mode='external_pull')
        remote_b = agent_factory(workspace_id=org_b.id,
                                 execution_mode='external_pull')
        db.session.commit()

        all_ids = {r['agent_id'] for r in provider.list_runtimes()}
        assert managed.id not in all_ids
        assert {remote_a.id, remote_b.id} <= all_ids

        ws_a_ids = {r['agent_id'] for r in provider.list_runtimes(workspace_id=org_a.id)}
        assert ws_a_ids == {remote_a.id}


class TestWebsocketMeta:
    def test_merge_writes_once_and_filters_fields(self, db_session, agent_factory):
        from api.agent_runtime_websocket import merge_runtime_meta

        agent = agent_factory(config=None)
        assert merge_runtime_meta(agent, {
            'host': 'macbook.local', 'engine': 'codex', 'junk': 'x',
        }) is True
        assert agent.config['runtime_meta'] == {'host': 'macbook.local',
                                                'engine': 'codex'}
        assert merge_runtime_meta(agent, {'host': 'macbook.local'}) is False
        assert merge_runtime_meta(agent, {'host': 'changed'}) is True
        assert agent.config['runtime_meta']['host'] == 'changed'

    def test_connected_set_add_discard(self, _clear_connected):
        from api.agent_runtime_websocket import is_agent_connected

        _clear_connected._CONNECTED_AGENT_IDS.add(42)
        assert is_agent_connected(42) is True
        _clear_connected._CONNECTED_AGENT_IDS.discard(42)
        assert is_agent_connected(42) is False
