"""私有化部署自检服务（Phase 4 企业能力：私有化部署增强）

面向离线/私有化环境的部署健康自检，覆盖三类检查：

1. 版本一致性（version）：代码声明的部署 schema 版本（DEPLOY_SCHEMA_VERSION）
   与 migrations/versions 目录的迁移文件数量、数据库实际 schema 能力对齐；
2. 必需配置项（env）：密钥类配置是否就位、.env.private.template 的占位符
   是否被真实值替换（'your_' 前缀检测）、生产模式告警项；
3. 迁移完整性（migration）：按 EXPECTED_SCHEMA 清单逐一核对关键表/列
   是否存在（覆盖 P2~P4 各迁移引入的 schema 特征）。

同一套检查供两端消费：
- 管理端点 GET /system/deploy/check（管理员）
- 离线脚本 scripts/deploy_check.py（退出码非 0 表示存在 error 级问题）
"""

import os
from pathlib import Path
from typing import Any, Dict, List

import structlog

from models import db

logger = structlog.get_logger()

# 代码侧声明的部署 schema 版本（对应 migrations/versions 最新编号）
DEPLOY_SCHEMA_VERSION = 16

# 必需配置项：缺失 → error
REQUIRED_ENV_KEYS = (
    'SECRET_KEY',
    'JWT_SECRET_KEY',
    'SECRET_ENCRYPTION_KEY',
)

# 已知占位符特征（.env.private.template 未替换的真实值）
PLACEHOLDER_MARK = 'your_'

# 告警配置项：缺失/命中 → warning（不阻断部署）
WARNING_ENV_KEYS = (
    'GITHUB_CLIENT_ID',
    'GITHUB_CLIENT_SECRET',
)

# 迁移完整性特征清单：(迁移编号/说明, 表名, [列名])
EXPECTED_SCHEMA: List[Dict[str, Any]] = [
    {'migration': '000001-000007 core+auth', 'table': 'agent_role_templates', 'columns': ['name']},
    {'migration': '000008 skill profile', 'table': 'agents', 'columns': ['skill_profile', 'skill_profile_updated_at']},
    {'migration': '000009 memory governance', 'table': 'agent_soul_versions', 'columns': ['memory_kind', 'snapshot_json']},
    {'migration': '000010 knowledge curation', 'table': 'project_knowledge_proposals', 'columns': ['dedupe_key', 'status']},
    {'migration': '000011 marketplace', 'table': 'agent_role_templates', 'columns': ['published_to_marketplace', 'published_at']},
    {'migration': '000012 sso', 'table': 'workspace_sso_configs', 'columns': ['provider', 'enabled']},
    {'migration': '000013 connectors', 'table': 'external_connector_configs', 'columns': ['provider', 'secret_encrypted']},
    {'migration': '000014 task parent indexes', 'table': 'tasks', 'columns': ['parent_task_id']},
    {'migration': '000015 audit project ids', 'table': 'agent_audit_events', 'columns': ['project_id']},
    {'migration': '000016 user theme', 'table': 'user_settings', 'columns': ['theme']},
]

# 基础 schema 特征（更早功能，缺失即核心能力受损）
CORE_SCHEMA: List[Dict[str, Any]] = [
    {'migration': 'core', 'table': 'tasks', 'columns': ['dod', 'human_intervention_count', 'epic_id']},
    {'migration': 'core', 'table': 'project_repo_bindings', 'columns': ['require_agent_review', 'autonomy_level']},
    {'migration': 'core', 'table': 'github_app_configs', 'columns': ['installation_id']},
    {'migration': 'core', 'table': 'budgets', 'columns': ['scope_type', 'period']},
    {'migration': 'core', 'table': 'goals', 'columns': []},
    {'migration': 'core', 'table': 'task_evidences', 'columns': ['evidence_type', 'status']},
]

PLACEHOLDER_VALUES = ('your_secure_root_password_here', 'your_secret_key_32_chars_or_more_here',
                      'your_jwt_secret_key_32_chars_or_more_here')


def _table_exists(connection, table_name) -> bool:
    dialect = connection.dialect.name
    if dialect == 'mysql':
        return connection.execute(
            db.text('SHOW TABLES LIKE :t'), {'t': table_name}
        ).first() is not None
    return connection.execute(
        db.text("SELECT name FROM sqlite_master WHERE type='table' AND name = :t"),
        {'t': table_name},
    ).first() is not None


def _column_exists(connection, table_name, column_name) -> bool:
    dialect = connection.dialect.name
    if dialect == 'mysql':
        return connection.execute(
            db.text('SHOW COLUMNS FROM `%s` LIKE :c' % table_name), {'c': column_name}
        ).first() is not None
    rows = connection.execute(db.text(f'PRAGMA table_info({table_name})')).fetchall()
    return any(row[1] == column_name for row in rows)


def _check_env(app) -> List[Dict[str, Any]]:
    checks = []

    def add(name, status, detail):
        checks.append({'name': name, 'category': 'env', 'status': status, 'detail': detail})

    for key in REQUIRED_ENV_KEYS:
        value = os.environ.get(key) or app.config.get(key if key != 'JWT_SECRET_KEY' else 'JWT_SECRET_KEY')
        if not value:
            add(f'env.{key}', 'error', 'required but missing')
        elif any(placeholder in str(value) for placeholder in PLACEHOLDER_VALUES) or str(value).startswith(PLACEHOLDER_MARK):
            add(f'env.{key}', 'error', 'still contains .env template placeholder')
        else:
            add(f'env.{key}', 'pass', 'configured')

    for key in WARNING_ENV_KEYS:
        value = os.environ.get(key)
        if not value or value.startswith(PLACEHOLDER_MARK):
            add(f'env.{key}', 'warning', 'not configured (optional integration)')

    if str(os.environ.get('DEBUG', '')).lower() in ('1', 'true', 'yes'):
        add('env.DEBUG', 'warning', 'DEBUG=true in deployment environment')

    return checks


def _check_database() -> List[Dict[str, Any]]:
    checks = []
    try:
        db.session.execute(db.text('SELECT 1'))
        checks.append({'name': 'db.connectivity', 'category': 'database',
                       'status': 'pass', 'detail': 'database reachable'})
    except Exception as e:  # noqa: BLE001
        checks.append({'name': 'db.connectivity', 'category': 'database',
                       'status': 'error', 'detail': f'database unreachable: {e}'})
        return checks

    for spec in CORE_SCHEMA + EXPECTED_SCHEMA:
        with db.engine.connect() as connection:
            if not _table_exists(connection, spec['table']):
                checks.append({'name': f"migration.{spec['table']}", 'category': 'migration',
                               'status': 'error',
                               'detail': f"table missing: {spec['table']} ({spec['migration']})"})
                continue
            missing = [column for column in spec['columns']
                       if not _column_exists(connection, spec['table'], column)]
            if missing:
                checks.append({'name': f"migration.{spec['table']}", 'category': 'migration',
                               'status': 'error',
                               'detail': f"missing columns: {', '.join(missing)} ({spec['migration']})"})
            else:
                checks.append({'name': f"migration.{spec['table']}", 'category': 'migration',
                               'status': 'pass', 'detail': f"({spec['migration']})"})
    return checks


def _check_version_consistency() -> List[Dict[str, Any]]:
    import re

    checks = []
    versions_dir = Path(__file__).resolve().parent.parent / 'migrations' / 'versions'
    migration_files = sorted(versions_dir.glob('*.py')) if versions_dir.exists() else []
    file_version = 0
    for path in migration_files:
        # 新命名：YYYYMMDD_HHMMSS_<序号>_<slug>.py，取第三段序号
        match = re.match(r'\d{8}_\d{6}_(\d+)_', path.stem)
        if match:
            file_version = max(file_version, int(match.group(1)))

    checks.append({
        'name': 'version.declared', 'category': 'version', 'status': 'pass',
        'detail': f'deploy schema version {DEPLOY_SCHEMA_VERSION}',
    })
    status = 'pass' if file_version == DEPLOY_SCHEMA_VERSION else 'warning'
    checks.append({
        'name': 'version.migration_files', 'category': 'version', 'status': status,
        'detail': f'migrations/versions latest={file_version}, declared={DEPLOY_SCHEMA_VERSION}',
    })
    return checks


def _add_runtime_check(checks: List[Dict[str, Any]], name: str, status: str,
                       detail: str, hint: str = '') -> None:
    entry = {'name': name, 'category': 'runtime', 'status': status, 'detail': detail}
    if hint:
        entry['hint'] = hint
    checks.append(entry)


def _check_runtime_provider() -> List[Dict[str, Any]]:
    """运行时环境后端检查：RUNTIME_PROVIDER 合法性 + 选中后端的前置条件。"""
    import subprocess

    from core.config import Config

    checks: List[Dict[str, Any]] = []
    provider = (getattr(Config, 'RUNTIME_PROVIDER', None) or 'k8s').strip().lower()
    valid = ('k8s', 'docker', 'compose', 'baremetal', 'remote')
    if provider in valid:
        _add_runtime_check(checks, 'runtime.provider', 'pass',
                           f'RUNTIME_PROVIDER={provider}')
    else:
        _add_runtime_check(checks, 'runtime.provider', 'error',
                           f'unknown RUNTIME_PROVIDER {provider!r}',
                           hint='可选值：k8s | docker | compose | baremetal | remote')
        return checks

    if provider == 'k8s':
        try:
            import kubernetes  # noqa: F401
            try:
                kubernetes.config.load_incluster_config()
                source = 'in-cluster'
            except Exception:  # noqa: BLE001
                kubernetes.config.load_kube_config()
                source = 'kubeconfig'
            _add_runtime_check(checks, 'runtime.backend.k8s', 'pass',
                               f'cluster credentials loaded ({source})')
        except Exception as e:  # noqa: BLE001
            _add_runtime_check(
                checks, 'runtime.backend.k8s', 'error', f'k8s unavailable: {e}',
                hint='安装 kubernetes 包并配置集群凭据，或改用 RUNTIME_PROVIDER=docker')
    elif provider in ('docker', 'compose'):
        try:
            result = subprocess.run(
                ['docker', 'info'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                _add_runtime_check(checks, f'runtime.backend.{provider}', 'pass',
                                   'docker daemon reachable')
            else:
                _add_runtime_check(
                    checks, f'runtime.backend.{provider}', 'error',
                    f'docker info failed: {(result.stderr or "").strip()[:200]}',
                    hint='确认 Docker 已安装且当前用户有权限访问 docker daemon')
        except FileNotFoundError:
            _add_runtime_check(checks, f'runtime.backend.{provider}', 'error',
                               'docker CLI not found',
                               hint='安装 Docker 或改用其他 RUNTIME_PROVIDER')
        except Exception as e:  # noqa: BLE001
            _add_runtime_check(checks, f'runtime.backend.{provider}', 'error',
                               f'docker unreachable: {e}')
    elif provider == 'baremetal':
        command = getattr(Config, 'BAREMETAL_RUNTIME_COMMAND', None)
        cwd = getattr(Config, 'BAREMETAL_RUNTIME_CWD', None)
        if command and cwd:
            _add_runtime_check(checks, 'runtime.backend.baremetal', 'pass',
                               f'command configured (cwd={cwd})')
        else:
            _add_runtime_check(
                checks, 'runtime.backend.baremetal', 'error',
                'BAREMETAL_RUNTIME_COMMAND / BAREMETAL_RUNTIME_CWD not configured',
                hint='baremetal 后端必须显式配置启动命令与工作目录')
    else:  # remote
        _add_runtime_check(checks, 'runtime.backend.remote', 'pass',
                           'reverse-connect agents self-manage their lifecycle')

    api_base_url = getattr(Config, 'API_BASE_URL', None)
    if api_base_url:
        _add_runtime_check(checks, 'runtime.api_base_url', 'pass',
                           f'API_BASE_URL={api_base_url}')
    else:
        _add_runtime_check(
            checks, 'runtime.api_base_url', 'warning', 'API_BASE_URL not set',
            hint='容器/远程运行时需要回连平台地址（docker 后端可用 DOCKER_API_BASE_URL）')
    return checks


def _check_agents_overview() -> List[Dict[str, Any]]:
    """Agent/连接面概览：有没有可干活的 Agent、反连是否在线。"""
    checks: List[Dict[str, Any]] = []
    try:
        from models.agent import Agent

        total = Agent.query.count()
        active = Agent.query.filter_by(status='ACTIVE').count()
        managed = Agent.query.filter_by(execution_mode='managed_runner').count()
        _add_runtime_check(
            checks, 'runtime.agents',
            'pass' if total else 'warning',
            f'total={total} active={active} managed_runner={managed}',
            hint='' if total else '还没有任何 Agent：在网页创建 Agent 并接入运行时',
        )
    except Exception as e:  # noqa: BLE001
        _add_runtime_check(checks, 'runtime.agents', 'error', f'query failed: {e}')
        return checks

    try:
        from api.agent_runtime_websocket import _CONNECTED_AGENT_IDS
        online = len(_CONNECTED_AGENT_IDS)
        _add_runtime_check(
            checks, 'runtime.ws_connected', 'pass' if online else 'warning',
            f'{online} agent(s) connected via websocket (this process)',
            hint='' if online else '没有在线反连连接：确认 agent-runtime daemon 已启动并配置了 agent_key',
        )
    except Exception:  # noqa: BLE001 — 非 Web 进程（脚本自检）无 WS 注册表，跳过
        pass
    return checks


def run_deploy_checks() -> Dict[str, Any]:
    """执行全部部署自检，返回报告（ok=False 表示存在 error 级问题）。"""
    from flask import current_app

    app = current_app._get_current_object()
    checks: List[Dict[str, Any]] = []
    checks.extend(_check_version_consistency())
    checks.extend(_check_env(app))
    checks.extend(_check_database())
    checks.extend(_check_runtime_provider())
    checks.extend(_check_agents_overview())

    errors = [check for check in checks if check['status'] == 'error']
    warnings = [check for check in checks if check['status'] == 'warning']
    return {
        'ok': not errors,
        'summary': {
            'total': len(checks), 'pass': len(checks) - len(errors) - len(warnings),
            'error': len(errors), 'warning': len(warnings),
        },
        'schema_version': DEPLOY_SCHEMA_VERSION,
        'checks': checks,
        'errors': [check['name'] for check in errors],
        'warnings': [check['name'] for check in warnings],
    }
