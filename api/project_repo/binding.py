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
            optional_fields=['default_branch', 'token', 'provider', 'autonomy_level',
                             'require_agent_review', 'reviewer_agent_id'],
        )
        if isinstance(data, tuple):
            return data

        provider = (data.get('provider') or 'github').strip().lower()
        if provider != 'github':
            return ApiResponse.error("Only 'github' provider is supported", 400).to_response()

        autonomy_level = data.get('autonomy_level', 0)
        try:
            autonomy_level = int(autonomy_level)
        except (TypeError, ValueError):
            return ApiResponse.error("autonomy_level must be 0, 1 or 2", 400).to_response()
        if autonomy_level not in (0, 1, 2):
            return ApiResponse.error("autonomy_level must be 0, 1 or 2", 400).to_response()

        binding = _get_binding(project_id)
        if not binding:
            binding = ProjectRepoBinding(project_id=project_id, created_by=f'user:{current_user.id}')
            db.session.add(binding)

        binding.provider = 'github'
        binding.repo_owner = data['repo_owner'].strip()
        binding.repo_name = data['repo_name'].strip()
        binding.default_branch = (data.get('default_branch') or 'main').strip() or 'main'
        binding.autonomy_level = autonomy_level
        binding.require_agent_review = bool(data.get('require_agent_review', False))
        binding.reviewer_agent_id = (
            int(data['reviewer_agent_id']) if data.get('reviewer_agent_id') else None
        )
        if 'token' in data:
            binding.token_encrypted = (
                _encrypt_secret(data['token']) if data['token'] else None
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
