"""
GitHub App 端点（GitHub App 化代码侧准备）

- POST /github/app/webhook：接收 GitHub webhook（HMAC SHA256 校验），
  处理 pull_request 事件并同步任务证据/状态（merged → 任务 DONE）
- GET  /github/app/manifest：生成 GitHub App Manifest 配置（引导创建 App）
- GET  /github/app/callback：Manifest 流程回调用一次性 code 换取凭据并加密存储
- GET  /github/app/status：安装状态查询
"""

import json

from flask import Blueprint, request

from models import Task, TaskEvidenceRecord
from core.auth import unified_auth_required, get_current_user
from core.secret_encryption import get_secret_encryption
from .base import ApiResponse
from services import github_app as github_app_service
from services.github_app import (
    GitHubAppError,
    exchange_manifest_code,
    get_app_config,
    get_webhook_secret,
    upsert_app_config,
    verify_webhook_signature,
)

github_app_bp = Blueprint('github_app', __name__)

BASE_URL = '/todo-for-ai/api/v1'  # 供 manifest 外部 URL 默认值拼接


def _find_task_by_pr(repo_full_name: str, pr_number):
    """通过 PR 证据定位任务（_upsert_pr_evidence 写入 repo_full_name + pr_number）。"""
    rows = (
        TaskEvidenceRecord.query
        .filter_by(evidence_type='pr')
        .order_by(TaskEvidenceRecord.id.desc())
        .limit(200)
        .all()
    )
    for row in rows:
        detail = row.detail or {}
        if (
            detail.get('pr_number') == pr_number
            and (detail.get('repo') or '').lower() == (repo_full_name or '').lower()
        ):
            task = Task.query.get(row.task_id)
            if task:
                return task
    return None


def _handle_pull_request_event(action: str, pr: dict, repo: dict):
    """同步 PR 事件到任务证据/状态（webhook 驱动，替代轮询）。"""
    repo_full_name = repo.get('full_name') or ''
    pr_number = pr.get('number')
    task = _find_task_by_pr(repo_full_name, pr_number)
    if not task:
        return {'handled': False, 'reason': 'no matching task'}

    merged = bool(pr.get('merged'))
    state = pr.get('state')
    status = 'passed' if merged else ('failed' if action == 'closed' and state == 'closed' else 'unknown')

    # 复用 project_repo 的证据写入（直接构造 pr_data 形状）
    from api.project_repo import _upsert_pr_evidence
    from models import ProjectRepoBinding, db
    from datetime import datetime
    from models import TaskStatus

    binding = ProjectRepoBinding.query.filter_by(project_id=task.project_id).first()
    pr_data = {
        'number': pr_number,
        'state': state,
        'merged': merged,
        'title': pr.get('title'),
        'html_url': pr.get('html_url'),
        'head': {'ref': (pr.get('head') or {}).get('ref')},
        'base': {'ref': (pr.get('base') or {}).get('ref')},
    }
    _upsert_pr_evidence(task, binding, pr_data, created_by='system:github-app')

    if merged and task.status and task.status.value != 'done':
        task.status = TaskStatus.DONE
        task.completed_at = datetime.utcnow()
        task.completion_rate = 100

    db.session.commit()
    return {'handled': True, 'task_id': task.id, 'action': action, 'status': status}


@github_app_bp.route('/github/app/webhook', methods=['POST'])
def github_app_webhook():
    """GitHub webhook 入口（HMAC SHA256 签名校验，事件处理）。

    签名 secret 来源：GitHubAppConfig（加密存储）优先，回退环境变量
    GITHUB_APP_WEBHOOK_SECRET；两者皆缺时拒绝（fail-closed）。
    """
    secret = get_webhook_secret()
    if not secret:
        return ApiResponse.error('webhook secret not configured', 503).to_response()

    signature = request.headers.get('X-Hub-Signature-256')
    body = request.get_data()
    if not verify_webhook_signature(body, signature, secret):
        return ApiResponse.error('invalid signature', 401).to_response()

    event = request.headers.get('X-GitHub-Event', '')
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return ApiResponse.error('invalid JSON payload', 400).to_response()

    if event == 'ping':
        return ApiResponse.success(data={'handled': True, 'event': 'ping'}, message='pong').to_response()

    if event == 'installation':
        action = payload.get('action')
        installation = payload.get('installation') or {}
        account = (installation.get('account') or {}).get('login')
        if action in ('created',):
            upsert_app_config({
                'installation_id': installation.get('id'),
                'account_login': account,
                'installed': True,
            })
        elif action in ('deleted',):
            upsert_app_config({'installed': False})
        return ApiResponse.success(
            data={'handled': True, 'event': event, 'action': action},
            message='installation event processed',
        ).to_response()

    if event == 'pull_request':
        action = payload.get('action')
        result = _handle_pull_request_event(action, payload.get('pull_request') or {}, payload.get('repository') or {})
        return ApiResponse.success(
            data={'handled': True, 'event': event, **result},
            message='pull_request event processed',
        ).to_response()

    return ApiResponse.success(
        data={'handled': False, 'event': event, 'reason': 'event ignored'},
        message='event ignored',
    ).to_response()


@github_app_bp.route('/github/app/manifest', methods=['GET'])
@unified_auth_required
def github_app_manifest():
    """生成 GitHub App Manifest（引导用户在 GitHub 上一键创建 App）。

    env 可覆盖：GITHUB_APP_NAME / GITHUB_APP_PUBLIC_BASE。
    """
    import os

    user = get_current_user()
    base = os.environ.get('GITHUB_APP_PUBLIC_BASE', request.host_url.rstrip('/'))
    manifest = {
        "name": os.environ.get("GITHUB_APP_NAME", "Todo for AI"),
        "url": f"{base}/todo-for-ai/pages/dashboard",
        "hook_attributes": {
            "active": True,
            "url": f"{base}{BASE_URL}/github/app/webhook",
        },
        "redirect_url": f"{base}{BASE_URL}/github/app/callback",
        "callback_urls": [f"{base}{BASE_URL}/github/app/callback"],
        "public": False,
        "request_oauth_on_install": False,
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
            "metadata": "read",
        },
        "default_events": ["pull_request", "installation"],
        # description 需非空且包含创建者信息（GitHub manifest 要求描述 App 用途）
        "description": f"Repo access for Todo for AI collaboration (installed by {user.email}).",
    }
    target = "https://github.com/settings/apps/new?state=todo-for-ai"
    return ApiResponse.success(
        data={'manifest': manifest, 'target_url': target},
        message='GitHub App manifest generated. POST manifest to target_url to create the App.',
    ).to_response()


@github_app_bp.route('/github/app/callback', methods=['GET'])
def github_app_callback():
    """Manifest 流程回调：一次性 code 换凭据（app_id/slug/private_key/...）并加密存储。"""
    code = request.args.get('code')
    if not code:
        return ApiResponse.error('missing code', 400).to_response()
    try:
        credentials = exchange_manifest_code(code)
    except GitHubAppError as e:
        return ApiResponse.error(str(e), 502).to_response()

    upsert_app_config({
        'app_id': credentials.get('id'),
        'slug': credentials.get('slug'),
        'private_key': credentials.get('pem'),
        'webhook_secret': credentials.get('webhook_secret'),
        'installed': False,  # 安装在用户确认 installation 后由 installation 事件置位
    })
    return ApiResponse.success(
        data={'app_id': credentials.get('id'), 'slug': credentials.get('slug'), 'configured': True},
        message='GitHub App credentials stored. Complete installation on GitHub.',
    ).to_response()


@github_app_bp.route('/github/app/status', methods=['GET'])
@unified_auth_required
def github_app_status():
    """App 配置/安装状态（secret 不回显）。"""
    config = get_app_config()
    if not config:
        return ApiResponse.error('GitHub App not configured', 404, error_details={"code": "NO_APP"}).to_response()
    return ApiResponse.success(data=config, message='GitHub App status').to_response()
