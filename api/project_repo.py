"""
项目仓库绑定与 Pull Request 端点（P1.1 代码平面）

任务 → 分支 → PR → 合并回写的平台侧入口：
- 项目绑定 GitHub 仓库（可带绑定级 token，加密存储）
- 为任务准备工作分支 / 创建 PR（PR 记录以 TaskEvidenceRecord(type='pr') 存证）
- 查询 PR 状态并同步回写任务（merged → 任务自动 DONE）
"""

from flask import Blueprint

from models import db, Project, Task, TaskEvidenceRecord, ProjectRepoBinding
from core.auth import unified_auth_required, get_current_user
from core.secret_encryption import get_secret_encryption
from .base import ApiResponse, validate_json_request
from services.github_client import (
    GitHubClient,
    GitHubClientError,
    resolve_token,
)

project_repo_bp = Blueprint('project_repo', __name__)


def _get_binding(project_id: int):
    return ProjectRepoBinding.query.filter_by(project_id=project_id).first()


def _ensure_task_access(task_id: int, manage: bool):
    """返回 (task, binding, error_response)。access 校验通过时 error 为 None。"""
    current_user = get_current_user()
    task = Task.query.get(task_id)
    if not task:
        return None, None, ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()
    if not current_user.can_access_project(task.project):
        return None, None, ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
    if manage and not current_user.can_manage_project(task.project):
        return None, None, ApiResponse.error("Permission denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
    binding = _get_binding(task.project_id)
    return task, binding, None


def _upsert_pr_evidence(task: Task, binding: ProjectRepoBinding, pr_data: dict, created_by: str = 'system'):
    """以 TaskEvidenceRecord(type='pr') 记录/刷新任务关联的 PR。"""
    number = pr_data.get('number')
    existing = None
    detail = {}
    for ev in TaskEvidenceRecord.query.filter_by(task_id=task.id, evidence_type='pr').all():
        detail = ev.detail or {}
        if detail.get('pr_number') == number:
            existing = ev
            break

    pr_state = pr_data.get('state') or 'open'
    merged = bool(pr_data.get('merged'))
    status = 'passed' if merged else ('failed' if pr_state == 'closed' else 'unknown')

    if existing:
        existing.status = status
        existing.detail = {
            **detail,
            'pr_state': pr_state,
            'pr_merged': merged,
            'pr_url': pr_data.get('html_url'),
        }
        return existing

    ev = TaskEvidenceRecord(
        task_id=task.id,
        evidence_type='pr',
        status=status,
        summary=f"PR #{number}: {pr_data.get('title') or ''}"[:500],
        detail={
            'pr_number': number,
            'pr_state': pr_state,
            'pr_merged': merged,
            'pr_url': pr_data.get('html_url'),
            'head_branch': pr_data.get('head', {}).get('ref'),
            'base_branch': pr_data.get('base', {}).get('ref'),
            'repo': binding.repo_full_name if binding else None,
        },
        url=pr_data.get('html_url'),
        created_by=created_by,
    )
    db.session.add(ev)
    return ev


# ── 项目仓库绑定 ──

@project_repo_bp.route('/projects/<int:project_id>/repo', methods=['GET'])
@unified_auth_required
def get_project_repo(project_id: int):
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404).to_response()
        if not current_user.can_access_project(project):
            return ApiResponse.error("Permission denied", 403).to_response()

        binding = _get_binding(project_id)
        if not binding:
            return ApiResponse.error("No repo bound", 404, error_details={"code": "NO_REPO_BOUND"}).to_response()
        return ApiResponse.success(data=binding.to_dict(), message='Repo binding retrieved').to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve repo binding: {e}", 500).to_response()


@project_repo_bp.route('/projects/<int:project_id>/repo', methods=['PUT'])
@unified_auth_required
def bind_project_repo(project_id: int):
    """绑定/更新项目仓库。

    body: {repo_owner, repo_name, default_branch?, token?, provider?}
    """
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404).to_response()
        if not current_user.can_manage_project(project):
            return ApiResponse.error("Permission denied", 403).to_response()

        data = validate_json_request(
            required_fields=['repo_owner', 'repo_name'],
            optional_fields=['default_branch', 'token', 'provider'],
        )
        if isinstance(data, tuple):
            return data

        provider = (data.get('provider') or 'github').strip().lower()
        if provider != 'github':
            return ApiResponse.error("Only 'github' provider is supported", 400).to_response()

        binding = _get_binding(project_id)
        if not binding:
            binding = ProjectRepoBinding(project_id=project_id, created_by=f'user:{current_user.id}')
            db.session.add(binding)

        binding.provider = 'github'
        binding.repo_owner = data['repo_owner'].strip()
        binding.repo_name = data['repo_name'].strip()
        binding.default_branch = (data.get('default_branch') or 'main').strip() or 'main'
        if 'token' in data:
            binding.token_encrypted = (
                get_secret_encryption().encrypt(data['token']) if data['token'] else None
            )
        db.session.commit()
        return ApiResponse.success(data=binding.to_dict(), message='Repo binding saved').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to bind repo: {e}", 500).to_response()


@project_repo_bp.route('/projects/<int:project_id>/repo', methods=['DELETE'])
@unified_auth_required
def unbind_project_repo(project_id: int):
    try:
        current_user = get_current_user()
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404).to_response()
        if not current_user.can_manage_project(project):
            return ApiResponse.error("Permission denied", 403).to_response()

        binding = _get_binding(project_id)
        if binding:
            db.session.delete(binding)
            db.session.commit()
        return ApiResponse.success(data={'unbound': True}, message='Repo binding removed').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to unbind repo: {e}", 500).to_response()


# ── 任务 PR 端点 ──

@project_repo_bp.route('/tasks/<int:task_id>/pull-request', methods=['POST'])
@unified_auth_required
def create_task_pull_request(task_id: int):
    """为任务创建 Pull Request。

    body: {head_branch, base_branch?, title?, body?, create_branch_if_missing?}
    若分支尚不存在（Agent 还没推送），会先从 base 分支建出空分支；
    此时 GitHub 会因无差异拒绝开 PR，返回 pr_created=false + branch_ready=true，
    Agent 推送提交后再次调用即可。
    """
    try:
        task, binding, err = _ensure_task_access(task_id, manage=False)
        if err:
            return err
        if not binding:
            return ApiResponse.error(
                "Project has no repo bound", 404, error_details={"code": "NO_REPO_BOUND"},
            ).to_response()

        data = validate_json_request(
            required_fields=['head_branch'],
            optional_fields=['base_branch', 'title', 'body', 'create_branch_if_missing'],
        )
        if isinstance(data, tuple):
            return data

        head_branch = data['head_branch'].strip()
        base_branch = (data.get('base_branch') or binding.default_branch or 'main').strip()
        if head_branch == base_branch:
            return ApiResponse.error("head_branch must differ from base_branch", 400).to_response()

        client = GitHubClient(resolve_token(binding))
        branch_created = False
        if data.get('create_branch_if_missing', True):
            branch_created = client.ensure_branch(
                binding.repo_owner, binding.repo_name, head_branch, base_branch,
            )

        title = (data.get('title') or f"[Task #{task.id}] {task.title}").strip()
        body = data.get('body') or f"Automated pull request for task #{task.id} ({task.title})."

        try:
            pr_data = client.create_pull_request(
                binding.repo_owner, binding.repo_name, head_branch, base_branch, title, body,
            )
            pr_created, reason = True, None
        except GitHubClientError as e:
            # 分支刚建出来还没有差异时，GitHub 返回 422：不算失败，等 Agent 推送后重开
            if e.status_code == 422:
                pr_created, reason = False, 'no_commits_yet'
                pr_data = {'head': {'ref': head_branch}, 'base': {'ref': base_branch}}
            else:
                raise

        if pr_created:
            _upsert_pr_evidence(task, binding, pr_data, created_by=f'user:{get_current_user().id}')
        db.session.commit()

        return ApiResponse.success(data={
            'pr_created': pr_created,
            'reason': reason,
            'branch_ready': True,
            'branch_created': branch_created,
            'head_branch': head_branch,
            'base_branch': base_branch,
            'pr': {
                'number': pr_data.get('number'),
                'url': pr_data.get('html_url'),
                'state': pr_data.get('state'),
            } if pr_created else None,
        }, message='Pull request flow processed').to_response()
    except GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create pull request: {e}", 500).to_response()


@project_repo_bp.route('/tasks/<int:task_id>/pull-request', methods=['GET'])
@unified_auth_required
def get_task_pull_request(task_id: int):
    """查询任务关联 PR 的最新状态，并把合并结果同步回任务与证据。"""
    try:
        task, binding, err = _ensure_task_access(task_id, manage=False)
        if err:
            return err
        if not binding:
            return ApiResponse.error(
                "Project has no repo bound", 404, error_details={"code": "NO_REPO_BOUND"},
            ).to_response()

        pr_evidence = (
            TaskEvidenceRecord.query
            .filter_by(task_id=task.id, evidence_type='pr')
            .order_by(TaskEvidenceRecord.id.desc())
            .first()
        )
        pr_number = (pr_evidence.detail or {}).get('pr_number') if pr_evidence else None
        if not pr_number:
            return ApiResponse.error(
                "No pull request associated with this task", 404,
                error_details={"code": "NO_PR"},
            ).to_response()

        client = GitHubClient(resolve_token(binding))
        pr_data = client.get_pull_request(binding.repo_owner, binding.repo_name, pr_number)
        _upsert_pr_evidence(task, binding, pr_data)

        merged = bool(pr_data.get('merged'))
        if merged and task.status and task.status.value != 'done':
            from models import TaskStatus
            from datetime import datetime
            task.status = TaskStatus.DONE
            task.completed_at = datetime.utcnow()
            task.completion_rate = 100

        db.session.commit()

        return ApiResponse.success(data={
            'task_status': task.status.value if task.status else None,
            'pr': {
                'number': pr_data.get('number'),
                'url': pr_data.get('html_url'),
                'state': pr_data.get('state'),
                'merged': merged,
                'merge_commit_sha': pr_data.get('merge_commit_sha'),
                'head_branch': pr_data.get('head', {}).get('ref'),
                'base_branch': pr_data.get('base', {}).get('ref'),
            },
        }, message='Pull request status synced').to_response()
    except GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to sync pull request: {e}", 500).to_response()
