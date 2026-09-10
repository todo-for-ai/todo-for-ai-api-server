"""系统监控服务（services/system_monitor.py）单测。

全链路打桩：DB 模型经 sys.modules 注入假模块，runtime provider 经
monkeypatch 替换；psutil 走"缺失/可用"两条路径。不依赖真实集群。
"""

import os
import sys
from types import SimpleNamespace

import pytest

from services import system_monitor as sm


class _ColumnStub:
    """模拟 ORM 列：支持 > / >= 比较（构造 SQLAlchemy 表达式位置）。"""

    def __init__(self, name):
        self.name = name

    def __gt__(self, other):
        return True

    def __ge__(self, other):
        return True

    def desc(self):
        return self

    def asc(self):
        return self


def _install_fake_module(monkeypatch, name, **attrs):
    monkeypatch.setitem(sys.modules, name, SimpleNamespace(**attrs))


class _ChainableQuery:
    """count()/filter_by()/filter()/order_by().limit().all() 可链式打桩。"""

    def __init__(self, counts, items=None):
        self._counts = iter(counts)
        self._items = items or []

    def count(self):
        return next(self._counts)

    def filter(self, *_a, **_kw):
        return self

    def filter_by(self, **_kw):
        return self

    def order_by(self, *_a):
        return self

    def limit(self, _n):
        return self

    def all(self):
        return self._items


class TestSetupState:
    def test_incomplete_without_admin_and_agent(self, monkeypatch):
        user_q = _ChainableQuery([0])
        agent_q = _ChainableQuery([0])
        _install_fake_module(monkeypatch, 'models.user',
                             User=SimpleNamespace(role='role', query=user_q),
                             UserRole=SimpleNamespace(ADMIN='admin'))
        _install_fake_module(monkeypatch, 'models.agent',
                             Agent=SimpleNamespace(query=agent_q))
        monkeypatch.setattr(sm, '_ws_connected_count', lambda: 0)

        state = sm.get_setup_state()
        assert state['has_admin'] is False
        assert state['has_agent'] is False
        assert state['complete'] is False

    def test_complete_with_admin_and_agent(self, monkeypatch):
        user_q = _ChainableQuery([1])
        agent_q = _ChainableQuery([2])
        _install_fake_module(monkeypatch, 'models.user',
                             User=SimpleNamespace(role='role', query=user_q),
                             UserRole=SimpleNamespace(ADMIN='admin'))
        _install_fake_module(monkeypatch, 'models.agent',
                             Agent=SimpleNamespace(query=agent_q))
        monkeypatch.setattr(sm, '_ws_connected_count', lambda: 1)

        state = sm.get_setup_state()
        assert state['has_admin'] is True
        assert state['has_agent'] is True
        assert state['has_connected_agent'] is True
        assert state['complete'] is True


class TestServerMetrics:
    def test_stdlib_fallback_when_psutil_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'psutil', None)  # import 即 ImportError
        monkeypatch.setattr(os, 'getloadavg', lambda: (1.5, 1.0, 0.5), raising=False)

        metrics = sm.collect_server_metrics()
        assert metrics['available_psutil'] is False
        assert metrics['load'] == {'one': 1.5, 'five': 1.0, 'fifteen': 0.5}
        assert metrics['cpu']['count'] == os.cpu_count()
        assert metrics['disk']['total'] is not None
        # 非 Linux 环境下 /proc/meminfo 不存在 → 内存字段为 None
        if not sys.platform.startswith('linux'):
            assert metrics['memory']['total'] is None

    def test_psutil_path(self, monkeypatch):
        fake_psutil = SimpleNamespace(
            cpu_percent=lambda interval=None: 42.0,
            virtual_memory=lambda: SimpleNamespace(
                total=16 * 1024**3, used=8 * 1024**3, percent=50.0),
            Process=lambda: SimpleNamespace(
                cpu_percent=lambda interval=None: 3.3,
                memory_info=lambda: SimpleNamespace(rss=123456789),
                create_time=lambda: 1700000000.0,
            ),
        )
        monkeypatch.setitem(sys.modules, 'psutil', fake_psutil)

        metrics = sm.collect_server_metrics()
        assert metrics['available_psutil'] is True
        assert metrics['cpu']['percent'] == 42.0
        assert metrics['memory']['percent'] == 50.0
        assert metrics['process']['rss_bytes'] == 123456789
        assert metrics['process']['cpu_percent'] == 3.3


class TestAgentMetrics:
    def _install(self, monkeypatch, agents):
        import enum

        class _State(enum.Enum):
            CREATED = 'created'
            ACTIVE = 'active'
            COMMITTED = 'committed'
            ABORTED = 'aborted'

        agent_q = _ChainableQuery(
            counts=[3, 2, 1],  # total / active / managed_runner
            items=agents)
        _install_fake_module(
            monkeypatch, 'models.agent',
            Agent=SimpleNamespace(query=agent_q, last_seen_at=_ColumnStub('last_seen_at')))
        _install_fake_module(
            monkeypatch, 'models.agent_task_attempt',
            AgentTaskAttempt=SimpleNamespace(
                state=_ColumnStub('state'), started_at=_ColumnStub('started_at'),
                query=_ChainableQuery(counts=[1, 2, 3, 0])),
            AgentTaskAttemptState=_State)
        _install_fake_module(
            monkeypatch, 'models.agent_task_lease',
            AgentTaskLease=SimpleNamespace(
                active=_ColumnStub('active'), expires_at=_ColumnStub('expires_at'),
                query=_ChainableQuery(counts=[2])))

        remote_provider = SimpleNamespace(list_runtimes=lambda workspace_id=None: [
            {'phase': 'Running'}, {'phase': 'Pending'}, {'phase': 'Unknown'}])
        global_provider = SimpleNamespace(list_runtimes=lambda workspace_id=None: [
            {'phase': 'Running'}])
        monkeypatch.setattr('services.runtime_env.get_runtime_provider',
                            lambda kind=None: remote_provider if kind == 'remote'
                            else global_provider)
        monkeypatch.setattr(
            'services.runtime_env.get_runtime_provider_for_agent',
            lambda agent: SimpleNamespace(
                get_runtime_status=lambda agent_id: {
                    'runtime_type': 'codex', 'phase': 'Running'}))

    def test_aggregates(self, monkeypatch):
        agents = [
            SimpleNamespace(id=1, name='a', status=SimpleNamespace(value='active'),
                            execution_mode='external_pull',
                            last_seen_at=None),
            SimpleNamespace(id=2, name='b', status=None,
                            execution_mode='managed_runner',
                            last_seen_at=None),
        ]
        self._install(monkeypatch, agents)

        data = sm.collect_agent_metrics()
        assert data['agents'] == {
            'total': 3, 'active': 2, 'managed_runner': 1, 'reverse_connect': 2}
        assert data['runtimes'] == {
            'remote_online': 1, 'remote_pending': 1, 'managed_occupying': 1}
        assert data['tasks']['active_leases'] == 2
        assert data['tasks']['attempts_24h'] == {
            'created': 1, 'active': 2, 'committed': 3, 'aborted': 0}
        assert data['recent_agents'][0]['engine'] == 'codex'
        assert data['recent_agents'][0]['runtime_phase'] == 'Running'

    def test_provider_failure_degrades_not_crashes(self, monkeypatch):
        agents = [SimpleNamespace(id=1, name='a', status=None,
                                  execution_mode='external_pull',
                                  last_seen_at=None)]
        self._install(monkeypatch, agents)

        def boom(kind=None):
            raise RuntimeError('no cluster creds')
        monkeypatch.setattr('services.runtime_env.get_runtime_provider', boom)

        data = sm.collect_agent_metrics()
        assert data['runtimes'] == {
            'remote_online': 0, 'remote_pending': 0, 'managed_occupying': 0}
        assert data['agents']['total'] == 3
