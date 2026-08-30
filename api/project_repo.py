"""
项目仓库绑定与 Pull Request 端点（P1.1 代码平面）

任务 → 分支 → PR → 合并回写的平台侧入口：
- 项目绑定 GitHub 仓库（可带绑定级 token，加密存储）
- 为任务准备工作分支 / 创建 PR（PR 记录以 TaskEvidenceRecord(type='pr') 存证）
- 查询 PR 状态并同步回写任务（merged → 任务自动 DONE）
"""

from flask import Blueprint

import uuid

from models import AgentTaskEvent, AuditLog, db, Project, Task, TaskEvidenceRecord, ProjectRepoBinding
from core.auth import unified_auth_required, get_current_user
from core.secret_encryption import get_secret_encryption
from services.github_app import encrypt_str as _encrypt_secret, decrypt_str as _decrypt_secret
from .base import ApiResponse, validate_json_request
from services.github_client import (
    GitHubClient,
    GitHubClientError,
    resolve_token,
)

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
            optional_fields=['default_branch', 'token', 'provider', 'autonomy_level'],
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
    except GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to sync pull request: {e}", 500).to_response()


def _execute_merge(task: Task, binding: ProjectRepoBinding, pr_number: int,
                   merge_method: str, actor=None) -> dict:
    """执行 GitHub 合并 + 证据刷新 + 任务完成 + 审计。actor=None 表示 L2 系统自主执行。"""
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
            client = GitHubClient(resolve_token(binding))
            try:
                pr_data = client.create_pull_request(
                    binding.repo_owner, binding.repo_name, head_branch, base_branch,
                    spec.get('title') or f"[Task #{task.id}] {task.title}",
                    f"Approved via interaction {interaction_id}.",
                )
                pr_number = pr_data.get('number')
                _upsert_pr_evidence(task, binding, pr_data, created_by=f'user:{current_user.id}')
                action_result = {'pr_created': True, 'pr_number': pr_number, 'url': pr_data.get('html_url')}
            except GitHubClientError as e:
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
    except GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to approve interaction: {e}", 500).to_response()


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

        db.session.commit()

        return ApiResponse.success(data={
            'merged': True,
            'task_status': task.status.value if task.status else None,
            'pr': {
                'number': pr_number,
                'merge_commit_sha': merge_result.get('sha'),
            },
        }, message='Pull request merged').to_response()
    except GitHubClientError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), e.status_code if e.status_code < 500 else 502).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to merge pull request: {e}", 500).to_response()
