"""
产品目标层 API（P2.1）

Goal（目标）→ Epic（特性）→ 带 DoD 的任务图：
- 人类：目标 CRUD、手工建 Epic、单条/批量裁决 Agent 提议、触发展开任务图
- Agent：提议 Epic（proposed 状态，待人类裁决）
"""

from datetime import datetime

from flask import Blueprint, request

from models import (
    Goal,
    GoalStatus,
    Epic,
    EpicStatus,
    db,
)
from core.auth import unified_auth_required, get_current_user
from .base import ApiResponse, validate_json_request

goals_bp = Blueprint('goals', __name__)


def _get_goal_or_error(goal_id: int, user):
    goal = db.session.get(Goal, goal_id)
    if not goal:
        return None, ApiResponse.error("Goal not found", 404, error_details={"code": "GOAL_NOT_FOUND"}).to_response()
    if goal.workspace_id and not user.is_admin:
        # workspace 成员校验（owner/admin/member 任一即可见）
        from models import OrganizationMember
        member = OrganizationMember.query.filter_by(
            organization_id=goal.workspace_id, user_id=user.id
        ).first()
        is_owner = getattr(goal, 'owner_id', None) == user.id
        if not member and not is_owner:
            return None, ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()
    return goal, None


# ── Goal CRUD ──

@goals_bp.route('/goals', methods=['POST'])
@unified_auth_required
def create_goal():
    try:
        user = get_current_user()
        data = validate_json_request(
            required_fields=['workspace_id', 'title'],
            optional_fields=['description', 'metrics', 'due_date', 'status'],
        )
        if isinstance(data, tuple):
            return data

        status = GoalStatus.DRAFT
        if data.get('status'):
            try:
                status = GoalStatus(str(data['status']).strip().lower())
            except ValueError:
                return ApiResponse.error("invalid status", 400).to_response()

        due_date = None
        if data.get('due_date'):
            try:
                due_date = datetime.fromisoformat(str(data['due_date']).replace('Z', '+00:00'))
            except ValueError:
                return ApiResponse.error("invalid due_date", 400).to_response()

        goal = Goal(
            workspace_id=int(data['workspace_id']),
            title=str(data['title']).strip()[:500],
            description=data.get('description'),
            metrics=data.get('metrics') if isinstance(data.get('metrics'), list) else None,
            status=status,
            owner_id=user.id,
            due_date=due_date,
            created_by=f'user:{user.id}',
        )
        db.session.add(goal)
        db.session.commit()
        return ApiResponse.success(data=goal.to_dict(), message='Goal created').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create goal: {e}", 500).to_response()


@goals_bp.route('/goals/<int:goal_id>', methods=['GET'])
@unified_auth_required
def get_goal(goal_id: int):
    user = get_current_user()
    goal, err = _get_goal_or_error(goal_id, user)
    if err:
        return err
    return ApiResponse.success(data=goal.to_dict(include_epics=True), message='Goal retrieved').to_response()


@goals_bp.route('/goals', methods=['GET'])
@unified_auth_required
def list_goals():
    try:
        user = get_current_user()
        page = max(request.args.get('page', 1, type=int) or 1, 1)
        per_page = min(max(request.args.get('per_page', 20, type=int) or 20, 1), 100)
        workspace_id = request.args.get('workspace_id', type=int)

        query = Goal.query
        if workspace_id:
            query = query.filter_by(workspace_id=workspace_id)
        query = query.order_by(Goal.id.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        return ApiResponse.success(data={
            'items': [g.to_dict() for g in pagination.items],
            'pagination': {
                'page': page, 'per_page': per_page,
                'total': pagination.total, 'has_next': pagination.has_next,
            },
        }, message='Goals retrieved').to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to list goals: {e}", 500).to_response()


# ── Epic 创建 / 提议 ──

def _create_epic(goal: Goal, title: str, description: str = None,
                 *, agent_proposed: bool = False, proposed_by_agent_id: int = None) -> Epic:
    order_index = int(goal.epics.count())
    epic = Epic(
        goal_id=goal.id,
        title=title[:500],
        description=description,
        status=EpicStatus.PROPOSED if agent_proposed else EpicStatus.ACCEPTED,
        order_index=order_index,
        agent_proposed=agent_proposed,
        proposed_by_agent_id=proposed_by_agent_id,
        decided_by_user_id=None if agent_proposed else None,
    )
    db.session.add(epic)
    return epic


@goals_bp.route('/goals/<int:goal_id>/epics', methods=['POST'])
@unified_auth_required
def create_epic(goal_id: int):
    """人类直接创建 Epic（默认 accepted）。"""
    try:
        user = get_current_user()
        goal, err = _get_goal_or_error(goal_id, user)
        if err:
            return err
        data = validate_json_request(
            required_fields=['title'], optional_fields=['description'],
        )
        if isinstance(data, tuple):
            return data

        epic = _create_epic(goal, str(data['title']).strip(), data.get('description'))
        epic.goal_id = goal.id
        epic.order_index = int(goal.epics.count())
        db.session.commit()
        return ApiResponse.success(data=epic.to_dict(), message='Epic created').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create epic: {e}", 500).to_response()


@goals_bp.route('/goals/<int:goal_id>/epics/propose', methods=['POST'])
@unified_auth_required
def propose_epic(goal_id: int):
    """Agent 提议 Epic（proposed 状态，待人类裁决）。

    body: {title, description?, agent_id?}
    """
    try:
        user = get_current_user()
        goal, err = _get_goal_or_error(goal_id, user)
        if err:
            return err
        data = validate_json_request(
            required_fields=['title', 'agent_id'], optional_fields=['description'],
        )
        if isinstance(data, tuple):
            return data

        epic = _create_epic(
            goal, str(data['title']).strip(), data.get('description'),
            agent_proposed=True, proposed_by_agent_id=int(data['agent_id']),
        )
        epic.goal_id = goal.id
        epic.order_index = int(goal.epics.count())
        db.session.commit()
        return ApiResponse.success(data=epic.to_dict(), message='Epic proposed, pending human decision').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to propose epic: {e}", 500).to_response()


# ── 裁决（单条 / 批量）──

def _decide_epics(epics, decision: str, user):
    decided = []
    for epic in epics:
        if epic.status != EpicStatus.PROPOSED:
            continue
        epic.status = EpicStatus.ACCEPTED if decision == 'approved' else EpicStatus.DROPPED
        epic.decided_by_user_id = user.id
        epic.decided_at = datetime.utcnow()
        decided.append(epic)
    db.session.commit()
    return decided


@goals_bp.route('/epics/<int:epic_id>/decide', methods=['POST'])
@unified_auth_required
def decide_epic(epic_id: int):
    try:
        user = get_current_user()
        epic = db.session.get(Epic, epic_id)
        if not epic:
            return ApiResponse.error("Epic not found", 404).to_response()
        goal, err = _get_goal_or_error(epic.goal_id, user)
        if err:
            return err

        data = validate_json_request(required_fields=['decision'], optional_fields=['reason'])
        if isinstance(data, tuple):
            return data
        decision = str(data['decision']).strip().lower()
        if decision not in ('approved', 'rejected'):
            return ApiResponse.error("decision must be approved or rejected", 400).to_response()

        decided = _decide_epics([epic], decision, user)
        if not decided:
            return ApiResponse.error("Epic is not in proposed state", 409).to_response()
        return ApiResponse.success(data=epic.to_dict(), message='Epic decided').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to decide epic: {e}", 500).to_response()


@goals_bp.route('/goals/<int:goal_id>/epics/decide-batch', methods=['POST'])
@unified_auth_required
def decide_epics_batch(goal_id: int):
    """批量裁决：{decision: approved|rejected, epic_ids?: [..]}（缺省裁决全部 proposed）。"""
    try:
        user = get_current_user()
        goal, err = _get_goal_or_error(goal_id, user)
        if err:
            return err
        data = validate_json_request(
            required_fields=['decision'],
            optional_fields=['epic_ids', 'reason'],
        )
        if isinstance(data, tuple):
            return data
        decision = str(data['decision']).strip().lower()
        if decision not in ('approved', 'rejected'):
            return ApiResponse.error("decision must be approved or rejected", 400).to_response()

        epic_ids = data.get('epic_ids') or []
        proposed = goal.epics.filter_by(status=EpicStatus.PROPOSED).all() if hasattr(goal.epics, 'filter_by') else []
        if epic_ids:
            epic_ids = [int(eid) for eid in epic_ids if str(eid).isdigit()]
            proposed = [e for e in proposed if e.id in epic_ids]

        decided = _decide_epics(proposed, decision, user)
        return ApiResponse.success(data={
            'decided': [e.to_dict() for e in decided],
            'count': len(decided),
        }, message='Epics decided in batch').to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to decide epics: {e}", 500).to_response()


# ── Epic 展开为任务图 ──

@goals_bp.route('/epics/<int:epic_id>/expand', methods=['POST'])
@unified_auth_required
def expand_epic(epic_id: int):
    """将已采纳的 Epic 展开为带 DoD 的任务图（LLM 生成骨架 + 确定性落地）。"""
    try:
        user = get_current_user()
        epic = db.session.get(Epic, epic_id)
        if not epic:
            return ApiResponse.error("Epic not found", 404).to_response()
        goal, err = _get_goal_or_error(epic.goal_id, user)
        if err:
            return err
        if epic.status not in (EpicStatus.ACCEPTED, EpicStatus.IN_PROGRESS):
            return ApiResponse.error("Only accepted epics can be expanded", 409).to_response()

        data = validate_json_request(optional_fields=['workspace_context', 'project_id'])
        if isinstance(data, tuple):
            return data

        from services.goal_decomposition import expand_epic_to_tasks
        project_id = data.get('project_id')
        result = expand_epic_to_tasks(
            epic, data.get('workspace_context'),
            project_id=int(project_id) if project_id else None,
        )
        return ApiResponse.success(data=result, message='Epic expanded to task graph').to_response()
    except ValueError as e:
        return ApiResponse.error(
            f"Epic expansion unavailable: {e}", 503,
            error_details={"code": "LLM_UNAVAILABLE"},
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to expand epic: {e}", 500).to_response()
