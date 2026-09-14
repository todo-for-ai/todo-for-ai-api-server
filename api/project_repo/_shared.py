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
from services.github_client import (
    GitHubClient,
    GitHubClientError,
    resolve_token,
)
from services.review_gate import check_agent_review_gate, resolve_reviewer_agent_id

project_repo_bp = Blueprint('project_repo', __name__)

INTERACTION_REQUEST_EVENT_TYPE = 'interaction_request'
INTERACTION_APPROVAL_EVENT_TYPE = 'interaction_approval'

# 自主等级
AUTONOMY_L0_APPROVE_ALL = 0
AUTONOMY_L1_AUTO_PR = 1
AUTONOMY_L2_AUTO_MERGE = 2

# 需要证据通过的 DoD 类型（pr/manual 由人工/平台核验）
EXECUTABLE_EVIDENCE_TYPES = ('test', 'build', 'lint', 'command')


def _new_interaction_id():
    return f"prx-{uuid.uuid4().hex[:12]}"


def _emit_pr_interaction_event(
    task: Task,
    *,
    interaction_id: str,
    interaction_type: str,
    status: str,
    decision: str = None,
    reviewer=None,
    pr_number: int = None,
    repo_full_name: str = None,
    extra: dict = None,
):
    """把 PR 创建/合并审批写入 AgentTaskEvent 交互流（与 agent_approval_queue 兼容）。

    请求事件 event_type=interaction_request；决策事件 event_type=interaction_approval。
    项目不属于任何组织（workspace 缺失）时返回 None，调用方回退权限门行为。
    """
    from datetime import datetime

    project = task.project
    workspace_id = project.organization_id if project else None
    if not workspace_id:
        return None

    agent_id = (
        db.session.query(TaskEvidenceRecord.agent_id)
        .filter_by(task_id=task.id)
        .order_by(TaskEvidenceRecord.id.desc())
        .first()
    )
    agent_id = agent_id[0] if agent_id else None

    event_time = datetime.utcnow()
    payload = {
        'interaction_id': interaction_id,
        'interaction_type': interaction_type,
        'task_id': int(task.id),
        'status': status,
        'pr_number': pr_number,
        'repo_full_name': repo_full_name,
        'governance': {'requires_approval': True, 'risk_tier': 'high'},
        'metadata': extra or {},
        'requested_at': event_time.isoformat(),
    }
    if decision is not None:
        payload.update({
            'decision': decision,
            'reviewer_user_id': int(reviewer.id) if reviewer else None,
            'reviewer_email': reviewer.email if reviewer else None,
            'decided_at': event_time.isoformat(),
        })

    row = AgentTaskEvent(
        task_id=int(task.id),
        attempt_id='',
        agent_id=agent_id,
        workspace_id=int(workspace_id),
        event_type=(
            INTERACTION_REQUEST_EVENT_TYPE
            if decision is None and status in ('pending_approval', 'requested')
            else INTERACTION_APPROVAL_EVENT_TYPE
        ),
        seq=1,
        event_timestamp=event_time,
        payload=payload,
        message=f"{interaction_type} {interaction_id} status={status}" + (f" decision={decision}" if decision else ""),
        created_by=f'user:{reviewer.id}' if reviewer else 'system:autonomy',
    )
    db.session.add(row)
    return row


def _executable_evidence_status(task: Task):
    """返回可执行证据的覆盖与通过情况：({type: status}, all_passed)。

    仅统计任务 DoD 声明的可执行类型；未声明时按已提交证据判断。
    """
    from models import TaskEvidenceRecord

    rows = (
        TaskEvidenceRecord.query
        .filter_by(task_id=task.id)
        .order_by(TaskEvidenceRecord.id.desc())
        .limit(100)
        .all()
    )
    latest = {}
    for row in rows:
        if row.evidence_type in EXECUTABLE_EVIDENCE_TYPES and row.evidence_type not in latest:
            latest[row.evidence_type] = row.status

    required = {
        str(c.get('type') or '').strip().lower()
        for c in (task.dod or [])
        if isinstance(c, dict) and str(c.get('type') or '').strip().lower() in EXECUTABLE_EVIDENCE_TYPES
    }
    if required:
        relevant = {t: latest.get(t, 'unknown') for t in required}
    else:
        relevant = latest

    all_passed = bool(relevant) and all(s == 'passed' for s in relevant.values())
    return relevant, all_passed


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


def _agent_review_gate_for(task: Task, binding: ProjectRepoBinding, pr_number: int,
                           author_agent_id: Optional[int] = None):
    """合并前的 Agent 评审者关卡。通过返回 None；未通过返回哨兵 dict。"""
    gate = check_agent_review_gate(task, binding, pr_number, author_agent_id=author_agent_id)
    if not gate["passed"]:
        AuditLog.record(
            action='task.pr_merge_blocked',
            resource_type='task',
            resource_id=task.id,
            actor_type='system',
            project_id=task.project_id,
            detail={'gate': gate, 'pr_number': pr_number},
        )
        db.session.commit()
        return {
            "_gate_blocked": True,
            "response": ApiResponse.error(
                f"Merge blocked by reviewer gate: {gate['reason']}",
                409,
                error_details={"code": gate["reason"].upper(), "gate": gate},
            ).to_response(),
        }
    return None


def _execute_merge(task: Task, binding: ProjectRepoBinding, pr_number: int,
                   merge_method: str, actor=None) -> dict:
    """执行 GitHub 合并 + 证据刷新 + 任务完成 + 审计。actor=None 表示 L2 系统自主执行。"""
    # P2.4 评审者关卡：binding 启用 require_agent_review 时，合并必须有通过评审
    author_agent_id = None
    author_evidence = (
        TaskEvidenceRecord.query
        .filter(TaskEvidenceRecord.task_id == task.id)
        .order_by(TaskEvidenceRecord.id.desc())
        .first()
    )
    if author_evidence is not None:
        author_agent_id = author_evidence.agent_id

    gate = _agent_review_gate_for(task, binding, pr_number, author_agent_id=author_agent_id)
    if gate is not None:
        return gate

    client = GitHubClient(resolve_token(binding))
    merge_result = client.merge_pull_request(binding.repo_owner, binding.repo_name, pr_number, merge_method)
    pr_data = client.get_pull_request(binding.repo_owner, binding.repo_name, pr_number)
    _upsert_pr_evidence(
        task, binding, pr_data,
        created_by=f'user:{actor.id}' if actor is not None else 'system:autonomy',
    )

    from datetime import datetime
    from models import TaskStatus
    if task.status and task.status.value != 'done':
        task.status = TaskStatus.DONE
        task.completed_at = datetime.utcnow()
        task.completion_rate = 100

    AuditLog.record(
        action='task.pr_merged',
        resource_type='task',
        resource_id=task.id,
        actor_type='human' if actor is not None else 'system:autonomy',
        actor_user_id=actor.id if actor is not None else None,
        project_id=task.project_id,
        detail={
            'repo': binding.repo_full_name,
            'pr_number': pr_number,
            'merge_method': merge_method,
            'merge_commit_sha': merge_result.get('sha'),
            'autonomy_level': int(binding.autonomy_level or 0),
        },
    )
    return merge_result


def _maybe_autonomous_merge(task: Task, binding: ProjectRepoBinding, actor=None):
    """L2 自主等级：验证证据全通过时自动合并任务关联的 open PR。

    返回 {merged, reason, pr_number?, evidence_summary?} 或 None（不适用）。
    """
    if int(binding.autonomy_level or 0) < AUTONOMY_L2_AUTO_MERGE:
        return None

    pr_evidence = (
        TaskEvidenceRecord.query
        .filter_by(task_id=task.id, evidence_type='pr')
        .order_by(TaskEvidenceRecord.id.desc())
        .first()
    )
    pr_number = (pr_evidence.detail or {}).get('pr_number') if pr_evidence else None
    if not pr_number:
        return None

    relevant, all_passed = _executable_evidence_status(task)
    if not all_passed:
        return {'merged': False, 'reason': 'evidence_not_all_passed', 'evidence': relevant}

    interaction_id = _new_interaction_id()
    _emit_pr_interaction_event(
        task,
        interaction_id=interaction_id,
        interaction_type='pr_merge',
        status='auto_approved',
        decision='approved',
        reviewer=None,
        pr_number=int(pr_number),
        repo_full_name=binding.repo_full_name,
        extra={'autonomy_level': int(binding.autonomy_level or 0), 'evidence': relevant},
    )

    try:
        merge_result = _execute_merge(task, binding, int(pr_number), 'merge', actor=None)
    except GitHubClientError as e:
        db.session.rollback()
        return {'merged': False, 'reason': 'merge_failed', 'error': str(e)}

    return {'merged': True, 'interaction_id': interaction_id, 'pr_number': int(pr_number), 'sha': merge_result.get('sha')}
