"""
项目仓库绑定与 Pull Request 端点（P1.1 代码平面）

任务 → 分支 → PR → 合并回写的平台侧入口：
- 项目绑定 GitHub 仓库（可带绑定级 token，加密存储）
- 为任务准备工作分支 / 创建 PR（PR 记录以 TaskEvidenceRecord(type='pr') 存证）
- 查询 PR 状态并同步回写任务（merged → 任务自动 DONE）
"""

from flask import Blueprint, request

import uuid
from typing import Optional

from models import (
    AgentTaskEvent,
    AuditLog,
    db,
    Project,
    ProjectMember,
    ProjectMemberRole,
    Task,
    TaskEvidenceRecord,
    ProjectRepoBinding,
)
from core.auth import unified_auth_required, get_current_user
from core.secret_encryption import get_secret_encryption
from services.github_app import encrypt_str as _encrypt_secret, decrypt_str as _decrypt_secret
from ..base import ApiResponse, get_request_args, validate_json_request
from services.review_gate import check_agent_review_gate, resolve_reviewer_agent_id
from . import _shared
from ._shared import (
    project_repo_bp,
    INTERACTION_REQUEST_EVENT_TYPE,
    INTERACTION_APPROVAL_EVENT_TYPE,
    AUTONOMY_L0_APPROVE_ALL,
    AUTONOMY_L1_AUTO_PR,
    AUTONOMY_L2_AUTO_MERGE,
    EXECUTABLE_EVIDENCE_TYPES,
    _new_interaction_id,
    _emit_pr_interaction_event,
    _executable_evidence_status,
    _get_binding,
    _ensure_task_access,
    _upsert_pr_evidence,
    _agent_review_gate_for,
    _execute_merge,
    _maybe_autonomous_merge,
)


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

        autonomy_level = int(binding.autonomy_level or 0)

        # L0：PR 创建需人工审批 —— 入 interaction 审批队列，不触达 GitHub
        if autonomy_level == AUTONOMY_L0_APPROVE_ALL:
            interaction_id = _new_interaction_id()
            event_row = _emit_pr_interaction_event(
                task,
                interaction_id=interaction_id,
                interaction_type='pr_create',
                status='pending_approval',
                pr_number=None,
                repo_full_name=binding.repo_full_name,
                extra={
                    'head_branch': head_branch,
                    'base_branch': base_branch,
                    'requested_by_user_id': get_current_user().id,
                    'title': (data.get('title') or f"[Task #{task.id}] {task.title}").strip(),
                },
            )
            db.session.commit()
            if event_row is None:
                # 项目不在组织内：无 workspace 审批流，回退权限门（manage 用户手动操作）
                return ApiResponse.success(data={
                    'pr_created': False,
                    'reason': 'approval_required',
                    'interaction_id': interaction_id,
                    'note': 'project has no workspace; approve via manage-permission endpoint',
                }, message='PR creation queued for approval').to_response()
            return ApiResponse.success(data={
                'pr_created': False,
                'reason': 'approval_required',
                'interaction_id': interaction_id,
                'head_branch': head_branch,
                'base_branch': base_branch,
            }, message='PR creation queued for approval').to_response()

        client = _shared.GitHubClient(_shared.resolve_token(binding))
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
        except _shared.GitHubClientError as e:
            # 分支刚建出来还没有差异时，GitHub 返回 422：不算失败，等 Agent 推送后重开
            if e.status_code == 422:
                pr_created, reason = False, 'no_commits_yet'
                pr_data = {'head': {'ref': head_branch}, 'base': {'ref': base_branch}}
            else:
                raise

        if pr_created:
            _upsert_pr_evidence(task, binding, pr_data, created_by=f'user:{get_current_user().id}')
        db.session.commit()

        # L2：验证证据全通过时创建后立即自动合并
        auto_merged = None
        if pr_created and autonomy_level >= AUTONOMY_L2_AUTO_MERGE:
            auto_merged = _maybe_autonomous_merge(task, binding, get_current_user())
            db.session.commit()

        return ApiResponse.success(data={
            'pr_created': pr_created,
            'reason': reason,
            'branch_ready': True,
            'branch_created': branch_created,
            'head_branch': head_branch,
            'base_branch': base_branch,
            'auto_merged': auto_merged,
            'pr': {
                'number': pr_data.get('number'),
                'url': pr_data.get('html_url'),
                'state': pr_data.get('state'),
            } if pr_created else None,
        }, message='Pull request flow processed').to_response()
    except _shared.GitHubClientError as e:
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

        client = _shared.GitHubClient(_shared.resolve_token(binding))
        pr_data = client.get_pull_request(binding.repo_owner, binding.repo_name, pr_number)
        _upsert_pr_evidence(task, binding, pr_data)

        merged = bool(pr_data.get('merged'))
        if merged and task.status and task.status.value != 'done':
            from models import TaskStatus
            from datetime import datetime
            task.status = TaskStatus.DONE
            task.completed_at = datetime.utcnow()
            task.completion_rate = 100

        auto_merged = None
        if not merged and pr_data.get('state') == 'open':
            auto_merged = _maybe_autonomous_merge(task, binding)
            if auto_merged and auto_merged.get('merged'):
                merged = True

        db.session.commit()

        return ApiResponse.success(data={
            'task_status': task.status.value if task.status else None,
            'auto_merged': auto_merged,
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
    except _shared.GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to sync pull request: {e}", 500).to_response()
