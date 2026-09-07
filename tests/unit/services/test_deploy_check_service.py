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
