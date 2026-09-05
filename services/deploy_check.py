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


def run_deploy_checks() -> Dict[str, Any]:
    """执行全部部署自检，返回报告（ok=False 表示存在 error 级问题）。"""
    from flask import current_app

    app = current_app._get_current_object()
    checks: List[Dict[str, Any]] = []
    checks.extend(_check_version_consistency())
    checks.extend(_check_env(app))
    checks.extend(_check_database())

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
