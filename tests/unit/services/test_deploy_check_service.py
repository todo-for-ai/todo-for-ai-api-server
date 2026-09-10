"""私有化部署自检服务（services/deploy_check.py）缺口补测。

补齐：MySQL 方言分支、告警级配置项、DEBUG 告警、数据库不可达分支、
缺表/缺列分支、版本一致性文件序号解析。与 api 层测试互补。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import deploy_check as dc


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    app = create_app("testing")
    app.config.update({"TESTING": True})
    ctx = app.app_context()
    ctx.push()
    yield app
    ctx.pop()


class TestDialectBranches:
    def test_mysql_table_check(self):
        conn = SimpleNamespace(
            dialect=SimpleNamespace(name='mysql'),
            execute=MagicMock(return_value=SimpleNamespace(
                first=lambda: [('tasks',)])))
        assert dc._table_exists(conn, 'tasks') is True
        assert "SHOW TABLES" in str(conn.execute.call_args.args[0])

        conn_empty = SimpleNamespace(
            dialect=SimpleNamespace(name='mysql'),
            execute=MagicMock(return_value=SimpleNamespace(first=lambda: None)))
        assert dc._table_exists(conn_empty, 'nope') is False

    def test_mysql_column_check(self):
        def fake_execute(_query, params=None):
            found = (params or {}).get('c') == 'dod'
            return SimpleNamespace(first=lambda: ('dod',) if found else None)
        conn = SimpleNamespace(
            dialect=SimpleNamespace(name='mysql'),
            execute=MagicMock(side_effect=fake_execute))
        assert dc._column_exists(conn, 'tasks', 'dod') is True
        assert dc._column_exists(conn, 'tasks', 'ghost') is False
        assert "SHOW COLUMNS" in str(conn.execute.call_args.args[0])


class TestEnvChecks:
    def test_warning_keys_and_debug(self, monkeypatch):
        for key in ('GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET', 'DEBUG',
                    'SECRET_KEY', 'JWT_SECRET_KEY', 'SECRET_ENCRYPTION_KEY'):
            monkeypatch.delenv(key, raising=False)
        checks = dc._check_env(SimpleNamespace(config={}))
        by_name = {c['name']: c for c in checks}
        assert by_name['env.GITHUB_CLIENT_ID']['status'] == 'warning'
        assert by_name['env.GITHUB_CLIENT_SECRET']['status'] == 'warning'
        assert 'env.DEBUG' not in by_name

        monkeypatch.setenv('DEBUG', 'true')
        checks = dc._check_env(SimpleNamespace(config={}))
        debug = [c for c in checks if c['name'] == 'env.DEBUG']
        assert debug and debug[0]['status'] == 'warning'


class TestDatabaseChecks:
    def test_unreachable_database_short_circuits(self, monkeypatch):
        def boom(_q):
            raise RuntimeError("no db")
        monkeypatch.setattr(dc.db.session, "execute", boom)
        checks = dc._check_database()
        assert len(checks) == 1
        assert checks[0]['status'] == 'error'
        assert "no db" in checks[0]['detail']

    def test_missing_table_reported(self, monkeypatch):
        monkeypatch.setattr(dc, "CORE_SCHEMA",
                            [{'migration': 't', 'table': 'no_such_table_x',
                              'columns': []}])
        monkeypatch.setattr(dc, "EXPECTED_SCHEMA", [])
        checks = dc._check_database()
        errors = [c for c in checks if c['status'] == 'error']
        assert any("table missing: no_such_table_x" in c['detail']
                   for c in errors)

    def test_missing_columns_reported(self, monkeypatch):
        # 桩掉表存在性：被测分支是"表在但列缺失"（:memory: 连接池状态不可依赖）
        monkeypatch.setattr(dc, "_table_exists", lambda conn, t: True)
        monkeypatch.setattr(dc, "CORE_SCHEMA",
                            [{'migration': 't', 'table': 'tasks',
                              'columns': ['no_such_col_y']}])
        monkeypatch.setattr(dc, "EXPECTED_SCHEMA", [])
        checks = dc._check_database()
        errors = [c for c in checks if c['status'] == 'error']
        assert any("missing columns: no_such_col_y" in c['detail']
                   for c in errors)


class TestVersionConsistency:
    def test_parses_latest_migration_serial(self, monkeypatch):
        class _FakePath:
            def __init__(self, *_a):
                pass

            def resolve(self):
                return self

            @property
            def parent(self):
                return self

            def __truediv__(self, _name):
                return self

            def exists(self):
                return True

            def glob(self, _pattern):
                class _F:
                    def __init__(self, stem):
                        self.stem = stem

                    def __lt__(self, other):
                        return self.stem < other.stem
                return [_F('20260901_010101_0016_user_theme'),
                        _F('20260902_020202_0022_runtime'),
                        _F('not-a-migration')]

        monkeypatch.setattr(dc, "Path", _FakePath)
        checks = dc._check_version_consistency()
        files_check = checks[-1]
        assert "latest=22" in files_check['detail']
        assert files_check['status'] == 'warning'  # 声明版本仍是 16
        assert checks[0]['status'] == 'pass'


class TestRuntimeProviderChecks:
    def _by_name(self, checks):
        return {c['name']: c for c in checks}

    def test_unknown_provider_short_circuits(self, monkeypatch):
        from core.config import Config
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'nomad', raising=False)
        checks = dc._check_runtime_provider()
        assert len(checks) == 1
        assert checks[0]['name'] == 'runtime.provider'
        assert checks[0]['status'] == 'error'
        assert 'k8s | docker' in checks[0]['hint']

    def test_docker_backend_reachable(self, monkeypatch):
        from core.config import Config
        from subprocess import CompletedProcess
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'docker', raising=False)
        monkeypatch.setattr(Config, 'API_BASE_URL', 'http://x:1/api/v1', raising=False)

        def fake_run(args, **kw):
            assert args[:2] == ['docker', 'info']
            return CompletedProcess(args, 0)
        monkeypatch.setattr('subprocess.run', fake_run)

        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.provider']['status'] == 'pass'
        assert by_name['runtime.backend.docker']['status'] == 'pass'
        assert by_name['runtime.api_base_url']['status'] == 'pass'

    def test_docker_daemon_down_reports_hint(self, monkeypatch):
        from core.config import Config
        from subprocess import CompletedProcess
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'compose', raising=False)
        monkeypatch.setattr(
            'subprocess.run',
            lambda args, **kw: CompletedProcess(args, 1, stderr='Cannot connect to the Docker daemon'))

        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.backend.compose']['status'] == 'error'
        assert 'Docker' in by_name['runtime.backend.compose']['hint']

    def test_docker_cli_missing(self, monkeypatch):
        from core.config import Config
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'docker', raising=False)

        def no_docker(args, **kw):
            raise FileNotFoundError('docker')
        monkeypatch.setattr('subprocess.run', no_docker)

        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.backend.docker']['status'] == 'error'
        assert '安装 Docker' in by_name['runtime.backend.docker']['hint']

    def test_k8s_sdk_missing_reports_error(self, monkeypatch):
        from core.config import Config
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'k8s', raising=False)
        monkeypatch.setitem(__import__('sys').modules, 'kubernetes', None)

        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.backend.k8s']['status'] == 'error'
        assert 'RUNTIME_PROVIDER=docker' in by_name['runtime.backend.k8s']['hint']

    def test_baremetal_requires_explicit_config(self, monkeypatch):
        from core.config import Config
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'baremetal', raising=False)
        monkeypatch.setattr(Config, 'BAREMETAL_RUNTIME_COMMAND', '', raising=False)
        monkeypatch.setattr(Config, 'BAREMETAL_RUNTIME_CWD', '', raising=False)

        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.backend.baremetal']['status'] == 'error'

        monkeypatch.setattr(Config, 'BAREMETAL_RUNTIME_COMMAND',
                            'python -m runtime.main', raising=False)
        monkeypatch.setattr(Config, 'BAREMETAL_RUNTIME_CWD', '/opt/agent', raising=False)
        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.backend.baremetal']['status'] == 'pass'

    def test_remote_backend_always_pass(self, monkeypatch):
        from core.config import Config
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'remote', raising=False)
        monkeypatch.setattr(Config, 'API_BASE_URL', '', raising=False)

        by_name = self._by_name(dc._check_runtime_provider())
        assert by_name['runtime.backend.remote']['status'] == 'pass'
        assert by_name['runtime.api_base_url']['status'] == 'warning'


class TestAgentsOverviewChecks:
    def _fake_agent_module(self, monkeypatch, counts):
        """把 models.agent.Agent 换成带可链式 query 桩的假模块。"""
        import sys
        from types import SimpleNamespace

        class _Q:
            def __init__(self, counter):
                self._counter = counter

            def count(self):
                return next(self._counter)

            def filter_by(self, **kw):
                return self

        class FakeAgent:
            query = _Q(iter(counts))

        monkeypatch.setitem(sys.modules, 'models.agent',
                            SimpleNamespace(Agent=FakeAgent))
        return FakeAgent

    def test_agents_present_passes(self, monkeypatch):
        self._fake_agent_module(monkeypatch, [3, 2, 1])
        from api import agent_runtime_websocket as ws
        monkeypatch.setattr(ws, '_CONNECTED_AGENT_IDS', {1, 2}, raising=False)

        by_name = {c['name']: c for c in dc._check_agents_overview()}
        assert by_name['runtime.agents']['status'] == 'pass'
        assert 'total=3' in by_name['runtime.agents']['detail']
        assert by_name['runtime.ws_connected']['status'] == 'pass'
        assert '2 agent(s)' in by_name['runtime.ws_connected']['detail']

    def test_no_agents_warns_with_hint(self, monkeypatch):
        self._fake_agent_module(monkeypatch, [0, 0, 0])
        from api import agent_runtime_websocket as ws
        monkeypatch.setattr(ws, '_CONNECTED_AGENT_IDS', set(), raising=False)

        by_name = {c['name']: c for c in dc._check_agents_overview()}
        assert by_name['runtime.agents']['status'] == 'warning'
        assert by_name['runtime.agents']['hint']
        assert by_name['runtime.ws_connected']['status'] == 'warning'

    def test_query_failure_is_error_not_crash(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        class _RaisingDescriptor:
            # 类级访问（Agent.query）即抛错；property 在类访问时返回自身、不适用
            def __get__(self, obj, owner=None):
                raise RuntimeError('db down')

        class BrokenAgent:
            query = _RaisingDescriptor()

        monkeypatch.setitem(sys.modules, 'models.agent',
                            SimpleNamespace(Agent=BrokenAgent))
        checks = dc._check_agents_overview()
        assert checks[0]['status'] == 'error'
        assert 'db down' in checks[0]['detail']


class TestRunDeployChecksIntegration:
    def test_report_includes_runtime_category(self, monkeypatch):
        from core.config import Config
        monkeypatch.setattr(Config, 'RUNTIME_PROVIDER', 'remote', raising=False)
        monkeypatch.setattr(dc, '_check_version_consistency', lambda: [])
        monkeypatch.setattr(dc, '_check_env', lambda app: [])
        monkeypatch.setattr(dc, '_check_database', lambda: [])
        monkeypatch.setattr(dc, '_check_agents_overview', lambda: [])

        report = dc.run_deploy_checks()
        names = [c['name'] for c in report['checks']]
        assert 'runtime.provider' in names
        assert 'runtime.backend.remote' in names
        assert report['summary']['total'] == len(report['checks'])
