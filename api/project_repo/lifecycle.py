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


@project_repo_bp.route('/tasks/<int:task_id>/pull-request/approve', methods=['POST'])
@unified_auth_required
def approve_task_pull_request(task_id: int):
    """审批队列批准回调：处理 pr_create / pr_merge 审批请求。

    body: {interaction_id, decision: approved|rejected, reason?, merge_method?}
    - pr_create + approved：按请求参数执行 PR 创建（复用创建端点逻辑的 GitHub 部分）
    - pr_merge  + approved：执行合并（复用 _execute_merge）
    - rejected：记录拒绝，不执行任何 GitHub 动作
    决策写入 interaction_approval 事件（审批队列可见），同时写 AuditLog。
    """
    try:
        task, binding, err = _ensure_task_access(task_id, manage=True)
        if err:
            return err
        if not binding:
            return ApiResponse.error(
                "Project has no repo bound", 404, error_details={"code": "NO_REPO_BOUND"},
            ).to_response()

        data = validate_json_request(
            required_fields=['interaction_id', 'decision'],
            optional_fields=['reason', 'merge_method'],
        )
        if isinstance(data, tuple):
            return data

        interaction_id = str(data['interaction_id']).strip()
        decision = str(data['decision']).strip().lower()
        if decision not in ('approved', 'rejected'):
            return ApiResponse.error("decision must be approved or rejected", 400).to_response()
        reason = (data.get('reason') or '').strip() or None
        merge_method = (data.get('merge_method') or 'merge').strip().lower()
        if merge_method not in ('merge', 'squash', 'rebase'):
            return ApiResponse.error("merge_method must be merge|squash|rebase", 400).to_response()

        current_user = get_current_user()

        # 定位审批请求事件
        request_row = (
            AgentTaskEvent.query
            .filter(
                AgentTaskEvent.task_id == task.id,
                AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
            )
            .order_by(AgentTaskEvent.id.desc())
            .limit(200)
            .all()
        )
        request_payload = None
        for row in request_row:
            if (row.payload or {}).get('interaction_id') == interaction_id:
                request_payload = row.payload
                break
        if not request_payload:
            return ApiResponse.error(
                "Interaction request not found", 404, error_details={"code": "NO_INTERACTION"},
            ).to_response()
        if (request_payload.get('status') or '').startswith('approved'):
            return ApiResponse.error("Interaction already approved", 409).to_response()

        interaction_type = request_payload.get('interaction_type')
        pr_number = request_payload.get('pr_number')
        extra_meta = {'reason': reason, 'reviewed_by': current_user.id}
        action_result = None

        if decision == 'rejected':
            _emit_pr_interaction_event(
                task,
                interaction_id=interaction_id,
                interaction_type=interaction_type,
                status='rejected',
                decision='rejected',
                reviewer=current_user,
                pr_number=pr_number,
                repo_full_name=binding.repo_full_name,
                extra=extra_meta,
            )
            AuditLog.record(
                action='task.pr_approval_rejected',
                resource_type='task', resource_id=task.id,
                actor_type='human', actor_user_id=current_user.id,
                project_id=task.project_id,
                detail={'interaction_id': interaction_id, 'interaction_type': interaction_type, 'reason': reason},
            )
            db.session.commit()
            return ApiResponse.success(data={
                'interaction_id': interaction_id, 'decision': 'rejected', 'executed': False,
            }, message='Interaction rejected').to_response()

        # approved：按类型执行
        if interaction_type == 'pr_create':
            spec = request_payload.get('metadata') or {}
            head_branch = spec.get('head_branch')
            base_branch = spec.get('base_branch') or binding.default_branch
            if not head_branch:
                return ApiResponse.error("request missing head_branch", 400).to_response()
            client = _shared.GitHubClient(_shared.resolve_token(binding))
            try:
                pr_data = client.create_pull_request(
                    binding.repo_owner, binding.repo_name, head_branch, base_branch,
                    spec.get('title') or f"[Task #{task.id}] {task.title}",
                    f"Approved via interaction {interaction_id}.",
                )
                pr_number = pr_data.get('number')
                _upsert_pr_evidence(task, binding, pr_data, created_by=f'user:{current_user.id}')
                action_result = {'pr_created': True, 'pr_number': pr_number, 'url': pr_data.get('html_url')}
            except _shared.GitHubClientError as e:
                if e.status_code == 422:
                    action_result = {'pr_created': False, 'reason': 'no_commits_yet'}
                else:
                    raise
        elif interaction_type == 'pr_merge':
            if not pr_number:
                return ApiResponse.error("request missing pr_number", 400).to_response()
            merge_result = _execute_merge(task, binding, int(pr_number), merge_method, actor=current_user)
            action_result = {
                'merged': True,
                'pr_number': int(pr_number),
                'merge_commit_sha': merge_result.get('sha'),
            }
        else:
            return ApiResponse.error(f"unsupported interaction_type: {interaction_type}", 400).to_response()

        _emit_pr_interaction_event(
            task,
            interaction_id=interaction_id,
            interaction_type=interaction_type,
            status='approved',
            decision='approved',
            reviewer=current_user,
            pr_number=pr_number,
            repo_full_name=binding.repo_full_name,
            extra=extra_meta,
        )
        AuditLog.record(
            action='task.pr_approval_approved',
            resource_type='task', resource_id=task.id,
            actor_type='human', actor_user_id=current_user.id,
            project_id=task.project_id,
            detail={'interaction_id': interaction_id, 'interaction_type': interaction_type, **action_result},
        )
        db.session.commit()

        return ApiResponse.success(data={
            'interaction_id': interaction_id,
            'decision': 'approved',
            'executed': True,
            'interaction_type': interaction_type,
            **action_result,
        }, message='Interaction approved and executed').to_response()
    except _shared.GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to approve interaction: {e}", 500).to_response()


@project_repo_bp.route('/tasks/pull-request/approvals/pending', methods=['GET'])
@unified_auth_required
def list_pending_pr_approvals():
    """列出当前用户可管理项目中待审批的 PR 交互请求（L0 审批队列前端数据源）。

    返回 interaction_request 事件（interaction_type ∈ pr_create/pr_merge、
    status=pending_approval），附带任务标题/项目，供 Command Center 展示与操作。
    """
    try:
        from sqlalchemy import or_ as sa_or

        current_user = get_current_user()
        page = max(request.args.get('page', 1, type=int) or 1, 1)
        per_page = min(max(request.args.get('per_page', 20, type=int) or 20, 1), 100)

        # 用户可管理的项目（owner 或成员 manage+）
        if current_user.is_admin():
            managed_project_ids = [pid for (pid,) in (
                db.session.query(Project.id).order_by(Project.id.desc()).limit(500).all()
            )]
        else:
            owned = db.session.query(Project.id).filter(Project.owner_id == current_user.id)
            member = db.session.query(ProjectMember.project_id).filter(
                ProjectMember.user_id == current_user.id,
                ProjectMember.role.in_([
                    ProjectMemberRole.OWNER, ProjectMemberRole.ADMIN,
                    ProjectMemberRole.MAINTAINER,
                ]),
            )
            managed_project_ids = [pid for (pid,) in owned.union(member).all()]

        if not managed_project_ids:
            return ApiResponse.success(data={'items': [], 'pagination': {
                'page': page, 'per_page': per_page, 'total': 0, 'has_next': False,
            }}, message='No pending PR approvals').to_response()

        rows = (
            AgentTaskEvent.query
            .filter(
                AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
                AgentTaskEvent.payload['interaction_type'].as_string().in_(['pr_create', 'pr_merge']),
                AgentTaskEvent.payload['status'].as_string() == 'pending_approval',
                Task.project_id.in_(managed_project_ids),
            )
            .join(Task, Task.id == AgentTaskEvent.task_id)
            .order_by(AgentTaskEvent.id.desc())
            .limit(per_page * page)
            .all()
        )

        # 无既有决策记录的才视为 pending
        items = []
        for row in rows:
            payload = row.payload or {}
            interaction_id = payload.get('interaction_id')
            decided = AgentTaskEvent.query.filter(
                AgentTaskEvent.task_id == row.task_id,
                AgentTaskEvent.event_type == INTERACTION_APPROVAL_EVENT_TYPE,
                AgentTaskEvent.payload['interaction_id'].as_string() == interaction_id,
            ).first()
            if decided:
                continue
            task = Task.query.get(row.task_id)
            items.append({
                'interaction_id': interaction_id,
                'interaction_type': payload.get('interaction_type'),
                'task_id': row.task_id,
                'task_title': task.title if task else None,
                'project_id': task.project_id if task else None,
                'pr_number': payload.get('pr_number'),
                'repo_full_name': payload.get('repo_full_name'),
                'head_branch': (payload.get('metadata') or {}).get('head_branch'),
                'requested_at': payload.get('requested_at'),
            })
            if len(items) >= per_page:
                break

        return ApiResponse.success(data={'items': items, 'pagination': {
            'page': page, 'per_page': per_page, 'total': len(items), 'has_next': False,
        }}, message='Pending PR approvals retrieved').to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to list pending PR approvals: {e}", 500).to_response()


@project_repo_bp.route('/tasks/<int:task_id>/pull-request/review', methods=['POST'])
@unified_auth_required
def review_task_pull_request(task_id: int):
    """Agent 评审者关卡证据提交。

    body: {decision: approved|rejected, reviewer_agent_id, summary?, pr_number?}
    - pr_number 缺省取任务关联 PR
    - 写 TaskEvidenceRecord(type='review', status=passed/failed)
    - reviewer_agent_id 必须等于绑定的评审者或编排 role_assignments['reviewer']
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
            required_fields=['decision', 'reviewer_agent_id'],
            optional_fields=['summary', 'pr_number'],
        )
        if isinstance(data, tuple):
            return data

        decision = str(data['decision']).strip().lower()
        if decision not in ('approved', 'rejected'):
            return ApiResponse.error("decision must be approved or rejected", 400).to_response()

        current_user = get_current_user()
        reviewer_agent_id = int(data['reviewer_agent_id'])
        designated = resolve_reviewer_agent_id(task, binding)
        if designated and reviewer_agent_id != designated:
            return ApiResponse.error(
                f"Only designated reviewer agent {designated} may submit review", 403,
                error_details={"code": "NOT_DESIGNATED_REVIEWER"},
            ).to_response()

        # 关联 PR：显式 pr_number 优先，缺省取最新 pr 证据
        if data.get('pr_number'):
            pr_number = int(data['pr_number'])
        else:
            pr_evidence = (
                TaskEvidenceRecord.query
                .filter_by(task_id=task.id, evidence_type='pr')
                .order_by(TaskEvidenceRecord.id.desc())
                .first()
            )
            pr_number = (pr_evidence.detail or {}).get('pr_number') if pr_evidence else None

        from datetime import datetime

        review = TaskEvidenceRecord(
            task_id=task.id,
            evidence_type='review',
            status='passed' if decision == 'approved' else 'failed',
            summary=(str(data.get('summary'))[:500] if data.get('summary') else f"review {decision}"),
            detail={
                'pr_number': pr_number,
                'decision': decision,
                'reviewer_agent_id': reviewer_agent_id,
            },
            verified_at=datetime.utcnow(),
            created_by=f'agent:{reviewer_agent_id}',
        )
        db.session.add(review)
        db.session.commit()

        return ApiResponse.success(data={
            'review_id': review.id,
            'status': review.status,
            'pr_number': pr_number,
        }, message='Review recorded').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to record review: {e}", 500).to_response()


@project_repo_bp.route('/tasks/<int:task_id>/pull-request/merge', methods=['POST'])
@unified_auth_required
def merge_task_pull_request(task_id: int):
    """人工审批动作：合并任务关联的 PR（合并后任务自动完成）。

    仅项目管理权限（can_manage_project）可调用；合并行为写入 AuditLog。
    body: {merge_method?: 'merge'|'squash'|'rebase'}
    """
    try:
        task, binding, err = _ensure_task_access(task_id, manage=True)
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

        data = validate_json_request(optional_fields=['merge_method'])
        if isinstance(data, tuple):
            return data
        merge_method = (data.get('merge_method') or 'merge').strip().lower()
        if merge_method not in ('merge', 'squash', 'rebase'):
            return ApiResponse.error("merge_method must be merge|squash|rebase", 400).to_response()

        current_user = get_current_user()
        merge_result = _execute_merge(task, binding, int(pr_number), merge_method, actor=current_user)
        if merge_result.get("_gate_blocked"):
            return merge_result["response"]

        db.session.commit()

        return ApiResponse.success(data={
            'merged': True,
            'task_status': task.status.value if task.status else None,
            'pr': {
                'number': pr_number,
                'merge_commit_sha': merge_result.get('sha'),
            },
        }, message='Pull request merged').to_response()
    except _shared.GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to merge pull request: {e}", 500).to_response()
