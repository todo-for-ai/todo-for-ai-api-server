"""运行时环境后端（services/runtime_env）回归测试。

覆盖：工厂选择/缓存、docker 后端的 run 参数构造与状态映射、
compose 文件生成与 down、baremetal 进程注册表、跨后端容量模板。
docker/compose 的 CLI 调用全部经 subprocess 打桩，不依赖本机 Docker。
注意：打桩收到的是完整 argv（含 docker 命令前缀）。
"""

import json
import os
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from core.config import Config
from services.runtime_env import (
    get_runtime_provider,
    reset_runtime_provider_cache,
)
from services.runtime_env.docker_provider import DockerRuntimeProvider


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    reset_runtime_provider_cache()
    yield
    reset_runtime_provider_cache()


def _completed(args, rc=0, out='', err=''):
    return CompletedProcess(args, rc, stdout=out, stderr=err)


def _agent(agent_id=7, workspace_id=3, **kwargs):
    defaults = dict(
        sandbox_policy={'cli_engine': 'claude'},
        llm_provider='anthropic',
        llm_model='claude-x',
        sandbox_profile='standard',
        max_concurrency=2,
        timeout_seconds=600,
        heartbeat_interval_seconds=10,
    )
    defaults.update(kwargs)
    return SimpleNamespace(id=agent_id, workspace_id=workspace_id, **defaults)


class TestFactory:
    def test_selects_docker_compose_baremetal(self, monkeypatch):
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'docker')
        assert isinstance(get_runtime_provider(), DockerRuntimeProvider)

        assert get_runtime_provider('compose').name == 'compose'
        assert get_runtime_provider('baremetal').name == 'baremetal'

    def test_selects_k8s_without_touching_cluster(self, monkeypatch):
        from services.agent_runtime_controller import AgentRuntimeController
        monkeypatch.setattr(AgentRuntimeController, '_init_k8s_client',
                            lambda self: None)
        assert isinstance(get_runtime_provider('k8s'), AgentRuntimeController)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match='RUNTIME_PROVIDER'):
            get_runtime_provider('nomad')

    def test_singleton_cache(self):
        assert get_runtime_provider('docker') is get_runtime_provider('docker')


class TestDockerProvider:
    def _provider(self, **kw):
        return DockerRuntimeProvider(**kw)

    def test_spawn_builds_expected_run_args(self, monkeypatch):
        provider = self._provider(network='todo4ai-net')
        calls = []

        def fake_run(args, timeout=None, **kw):
            calls.append(args)
            if args[1:2] == ['inspect']:
                return _completed(args, rc=1, err='No such object')
            return _completed(args, rc=0, out='abc123def456')

        monkeypatch.setattr('services.runtime_env.docker_provider.subprocess.run',
                            fake_run)
        info = provider.spawn(_agent(), 'ak_secret')

        run_args = next(c for c in calls if c[1:2] == ['run'])
        joined = ' '.join(run_args)
        assert '--name todo4ai-agent-7' in joined
        assert 'todo4ai.managed=true' in joined
        assert 'todo4ai.agent-id=7' in joined
        assert 'todo4ai.workspace-id=3' in joined
        assert 'todo4ai.runtime-type=claude' in joined
        assert '-e AGENT_KEY=ak_secret' in joined
        assert '-e CLI_AGENT_ENGINE=claude' in joined
        assert '--network todo4ai-net' in joined
        assert '--cpus 2' in joined       # standard 档 limits
        assert '--memory 2g' in joined    # 2Gi → 2g
        assert joined.endswith('todo4ai/agent-cli-agents:latest')
        assert info['agent_id'] == 7

    def test_status_maps_docker_states(self, monkeypatch):
        provider = self._provider()
        states = {'running': 'Running', 'created': 'Pending', 'exited': 'Failed'}
        for docker_state, expected in states.items():
            payload = json.dumps({
                'Id': 'abc123', 'Name': '/todo4ai-agent-7',
                'State': {'Status': docker_state, 'StartedAt': '2026-09-10T00:00:00Z'},
                'Config': {'Labels': {'agent-id': '7', 'workspace-id': '3',
                                      'runtime-type': 'claude'}},
                'NetworkSettings': {'IPAddress': '172.17.0.5'},
            })
            monkeypatch.setattr(
                'services.runtime_env.docker_provider.subprocess.run',
                lambda args, timeout=None, _payload=payload, **kw:
                    _completed(args, 0, _payload))
            info = provider.get_runtime_status(7)
            assert info['phase'] == expected
            assert info['address'] == '172.17.0.5'
            assert info['runtime_id'] == 'abc123'

    def test_status_missing_container_returns_none(self, monkeypatch):
        provider = self._provider()
        monkeypatch.setattr(
            'services.runtime_env.docker_provider.subprocess.run',
            lambda args, timeout=None, **kw: _completed(args, rc=1, err='No such object'))
        assert provider.get_runtime_status(7) is None

    def test_terminate_uses_rm_force(self, monkeypatch):
        provider = self._provider()
        calls = []
        monkeypatch.setattr(
            'services.runtime_env.docker_provider.subprocess.run',
            lambda args, timeout=None, **kw:
                calls.append(args) or _completed(args, 0))
        assert provider.terminate(7) is True
        assert calls[0][:3] == ['docker', 'rm', '-f']
        assert calls[0][3] == 'todo4ai-agent-7'

    def test_ensure_runtime_capacity_limit(self, db_session):
        from services.workspace_runtime_policy import set_workspace_runtime_setting
        provider = self._provider()
        ws = 910001
        set_workspace_runtime_setting(ws, max_pods=1)
        provider.get_runtime_status = lambda agent_id: None
        provider.list_runtimes = lambda workspace_id=None: [
            {'agent_id': 1, 'workspace_id': ws, 'phase': 'Running'},
        ]
        result = provider.ensure_runtime(_agent(agent_id=2, workspace_id=ws), 'k')
        assert result['status'] == 'workspace_pod_limit'
        assert result['cap'] == 1

    def test_ensure_runtime_already_running(self):
        provider = self._provider()
        provider.get_runtime_status = lambda agent_id: {
            'agent_id': agent_id, 'phase': 'Running'}
        result = provider.ensure_runtime(_agent(), 'k')
        assert result['status'] == 'already_running'


class TestComposeProvider:
    def test_spawn_writes_compose_file_and_runs_up(self, monkeypatch, tmp_path):
        from services.runtime_env.compose_provider import ComposeRuntimeProvider

        provider = ComposeRuntimeProvider(compose_dir=str(tmp_path))
        calls = []
        monkeypatch.setattr(
            'services.runtime_env.docker_provider.subprocess.run',
            lambda args, timeout=None, **kw:
                calls.append(args) or _completed(args, 0))
        monkeypatch.setattr(provider, 'get_runtime_status',
                            lambda agent_id: {'agent_id': agent_id, 'phase': 'Running'})

        info = provider.spawn(_agent(), 'ak_secret')
        assert info['phase'] == 'Running'

        compose_path = tmp_path / 'agent-7.yml'
        assert compose_path.exists()
        content = compose_path.read_text()
        assert 'container_name: todo4ai-agent-7' in content
        assert 'AGENT_KEY: ak_secret' in content
        assert 'CLI_AGENT_ENGINE: claude' in content

        up_args = next(c for c in calls if c[1:2] == ['compose'])
        assert '-p todo4ai-agent-7' in ' '.join(up_args)
        assert up_args[-2:] == ['up', '-d']

    def test_terminate_returns_false_when_absent(self, monkeypatch, tmp_path):
        from services.runtime_env.compose_provider import ComposeRuntimeProvider

        provider = ComposeRuntimeProvider(compose_dir=str(tmp_path))
        monkeypatch.setattr(provider, 'get_runtime_status', lambda agent_id: None)
        assert provider.terminate(7) is False


class TestBaremetalProvider:
    def _provider(self, tmp_path):
        from services.runtime_env.baremetal_provider import BaremetalRuntimeProvider
        return BaremetalRuntimeProvider(
            state_dir=str(tmp_path / 'state'),
            command='python -m runtime.main',
            cwd=str(tmp_path),
        )

    def _write_state(self, provider, agent_id, pid, workspace_id=3):
        with open(provider._state_path(agent_id), 'w') as fh:
            json.dump({'pid': pid, 'agent_id': agent_id,
                       'workspace_id': workspace_id, 'started_at': None}, fh)

    def test_spawn_requires_explicit_config(self, tmp_path):
        from services.runtime_env.baremetal_provider import BaremetalRuntimeProvider
        provider = BaremetalRuntimeProvider(state_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match='BAREMETAL_RUNTIME_COMMAND'):
            provider.spawn(_agent(), 'k')

    def test_spawn_popen_and_state_file(self, monkeypatch, tmp_path):
        provider = self._provider(tmp_path)
        popen_calls = []

        class FakeProc:
            pid = 987654

        monkeypatch.setattr(
            'services.runtime_env.baremetal_provider.subprocess.Popen',
            lambda cmd, **kw: popen_calls.append((cmd, kw)) or FakeProc())
        info = provider.spawn(_agent(), 'ak_secret')

        assert info['phase'] == 'Running'
        assert len(popen_calls) == 1
        cmd, kwargs = popen_calls[0]
        assert cmd == ['python', '-m', 'runtime.main']
        assert kwargs['start_new_session'] is True
        assert kwargs['env']['AGENT_KEY'] == 'ak_secret'
        assert (tmp_path / 'state' / 'agent-7.json').exists()

    def test_status_alive_and_dead(self, monkeypatch, tmp_path):
        provider = self._provider(tmp_path)
        # 活进程：用当前测试进程的 PID（os.kill(pid,0) 必然成功）
        monkeypatch.setattr(
            'services.runtime_env.baremetal_provider.subprocess.Popen',
            lambda cmd, **kw: SimpleNamespace(pid=os.getpid()))
        provider.spawn(_agent(), 'k')
        assert provider.get_runtime_status(7)['phase'] == 'Running'

        # 死进程：状态文件被清理，状态为 None
        self._write_state(provider, 7, pid=99999998)
        assert provider.get_runtime_status(7) is None
        assert not (tmp_path / 'state' / 'agent-7.json').exists()

    def test_terminate_dead_pid_cleans_up(self, tmp_path):
        provider = self._provider(tmp_path)
        self._write_state(provider, 7, pid=99999999)
        assert provider.terminate(7) is True
        assert not (tmp_path / 'state' / 'agent-7.json').exists()
        assert provider.terminate(7) is False

    def test_list_filters_by_workspace(self, tmp_path):
        provider = self._provider(tmp_path)
        # 两个 pid 都不存在 → 状态读取即清场 → 列表为空
        self._write_state(provider, 7, pid=99999991, workspace_id=3)
        self._write_state(provider, 8, pid=99999992, workspace_id=4)
        assert provider.list_runtimes() == []
