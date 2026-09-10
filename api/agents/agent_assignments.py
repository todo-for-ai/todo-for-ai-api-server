"""Agent 任务认领与分配路由：审查队列 / 推荐 / 认领 / 分配查询与更新。

从 ``_core.py`` 拆出（迭代 47）。修历史缺陷：``list_review_queue`` 使用
``or_``/``and_`` 但原文件从未导入（默认 ``action=all`` 请求必 500）。
"""

from datetime import datetime, timedelta

from flask import request
from sqlalchemy import and_, or_

from services.agent_working_schedule import evaluate_working_window, is_in_working_window

from ._shared import (
    ACTIVE_ASSIGNMENT_STATES,
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    ApiResponse,
    apply_assignment_update,
    AssignmentUpdateError,
    Project,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    agents_bp,
    build_task_snapshot,
    db,
    mark_stale_agents_offline,
    expire_assignment,
    expire_stale_assignments,
    expire_stale_assignments_for_task,
    active_assignment_filter,
    find_active_assignment,
    find_claimable_task,
    flush_sse_notifications,
    get_current_user,
    get_owned_agent_or_response,
    get_owned_task_or_response,
    get_request_args,
    parse_enum,
    paginate_serialized,
    record_assignment_update_rejected,
    record_task_event,
    score_task_for_agent,
    serialize_claim_response,
    serialize_review_queue_item,
    unified_auth_required,
    validate_json_request,
    _client_ip,
)
from models import AuditLog


@agents_bp.route("/review-queue", methods=["GET"])
@unified_auth_required
def list_review_queue():
    """List Agent assignments that need human attention."""
    try:
        current_user = get_current_user()
        args = get_request_args()

        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        expired_assignments = expire_stale_assignments(current_user=current_user)
        if stale_agents or expired_assignments:
            db.session.commit()

        query = TaskAssignment.query.join(Task, TaskAssignment.task_id == Task.id).join(
            Project,
            Task.project_id == Project.id,
        ).filter(Project.owner_id == current_user.id)

        action = request.args.get("action", "all")
        if action == "human_feedback":
            query = query.filter(TaskAssignment.state == TaskAssignmentState.WAITING_HUMAN)
        elif action == "final_review":
            query = query.filter(
                TaskAssignment.state.in_([TaskAssignmentState.REVIEW, TaskAssignmentState.DONE]),
                Task.status == TaskStatus.REVIEW,
            )
        elif action == "all":
            query = query.filter(
                or_(
                    TaskAssignment.state == TaskAssignmentState.WAITING_HUMAN,
                    TaskAssignment.state == TaskAssignmentState.REVIEW,
                    and_(TaskAssignment.state == TaskAssignmentState.DONE, Task.status == TaskStatus.REVIEW),
                )
            )
        else:
            return ApiResponse.error("Invalid action. Must be one of: all, human_feedback, final_review", 400).to_response()

        query = query.order_by(TaskAssignment.updated_at.desc(), TaskAssignment.created_at.desc())
        result = paginate_serialized(query, args["page"], args["per_page"], serialize_review_queue_item)

        return ApiResponse.success(result, "Agent review queue retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve Agent review queue: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/recommended-tasks", methods=["GET"])
@unified_auth_required
def list_recommended_tasks(agent_id):
    """List unassigned tasks that match this Agent's capabilities, sorted by score.

    Query params:
      limit — max tasks to return (default 10, max 50)
      project_id — filter by project
    """
    user = get_current_user()
    agent, response = get_owned_agent_or_response(agent_id, user)
    if response:
        return response

    limit = min(request.args.get("limit", 10, type=int), 50)
    project_id = request.args.get("project_id", type=int)

    # Find unassigned TODO tasks
    assigned_task_ids = {a.task_id for a in TaskAssignment.query.filter(
        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
    ).all()}

    query = Task.query.filter(
        Task.status == TaskStatus.TODO,
        Task.is_ai_task == True,  # noqa: E712
        ~Task.id.in_(assigned_task_ids),
    )
    if project_id:
        query = query.filter_by(project_id=project_id)
    tasks = query.order_by(Task.created_at.desc()).limit(200).all()

    # Score each task
    scored = []
    for task in tasks:
        result = score_task_for_agent(task, agent)
        if result["score"] > 0:
            scored.append({
                "task": task.to_dict(),
                "score": result["score"],
                "matched_capabilities": result["matched_capabilities"],
                "matched_tags": result["matched_tags"],
                "matched_text": result["matched_text"],
                "missing_required": result["missing_required"],
            })

    scored.sort(key=lambda x: -x["score"])
    return ApiResponse.success(scored[:limit], f"Found {len(scored)} recommended tasks").to_response()


@agents_bp.route("/<int:agent_id>/assignments", methods=["GET"])
@unified_auth_required
def list_agent_assignments(agent_id):
    """List assignments for an Agent."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        expired_assignments = expire_stale_assignments(current_user=current_user, agent_id=agent.id)
        if stale_agents or expired_assignments:
            db.session.commit()

        args = get_request_args()
        query = TaskAssignment.query.filter_by(agent_id=agent.id)

        state = request.args.get("state")
        if state:
            try:
                query = query.filter_by(state=parse_enum(TaskAssignmentState, state, "state"))
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        query = query.order_by(TaskAssignment.created_at.desc())
        result = paginate_serialized(
            query,
            args["page"],
            args["per_page"],
            lambda assignment: assignment.to_dict(include_task=True, include_agent=True),
        )

        return ApiResponse.success(result, "Agent assignments retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve Agent assignments: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/claim", methods=["POST"])
@unified_auth_required
def claim_task(agent_id):
    """Claim a task for execution."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if agent.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Agent is not available to claim tasks", 409).to_response()

        # 工作时间区间门禁：区间外不允许领取新任务（进行中的任务不受影响）
        schedule = agent.working_schedule or {}
        if not is_in_working_window(schedule):
            evaluation = evaluate_working_window(schedule)
            return ApiResponse.error(
                "AGENT_OUT_OF_WORKING_WINDOW",
                409,
                next_window_at=evaluation.get("next_window_at"),
            ).to_response()

        data = request.get_json(silent=True) or {}
        lease_seconds = int(data.get("lease_seconds") or 1800)
        lease_seconds = max(60, min(lease_seconds, 24 * 60 * 60))

        task = None
        capability_match = None
        is_manual_dispatch = bool(data.get("task_id") and data.get("dispatch_source") == "human")
        if data.get("task_id"):
            task, response = get_owned_task_or_response(data["task_id"], current_user)
            if response:
                return response
            expire_stale_assignments_for_task(task.id)
            active_assignment = find_active_assignment(task.id, for_update=True)
            if active_assignment:
                return ApiResponse.error(
                    "Task already has an active assignment",
                    409,
                    assignment=active_assignment.to_dict(include_agent=True),
                ).to_response()
        else:
            task, capability_match = find_claimable_task(
                current_user,
                agent=agent,
                project_id=data.get("project_id"),
                match_capabilities=data.get("match_capabilities", True) is not False,
            )

        if not task:
            db.session.commit()
            return ApiResponse.success(None, "No claimable task found").to_response()

        now = datetime.utcnow()
        assignment = TaskAssignment(
            task_id=task.id,
            agent_id=agent.id,
            assigned_by_user_id=current_user.id,
            state=TaskAssignmentState.CLAIMED,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            claimed_at=now,
            last_heartbeat_at=now,
            progress_rate=0,
            created_by=current_user.email,
        )
        db.session.add(assignment)

        if task.status == TaskStatus.TODO:
            task.status = TaskStatus.IN_PROGRESS

        agent.last_seen_at = now
        if agent.status == AgentStatus.OFFLINE:
            agent.status = AgentStatus.ACTIVE
        run_metadata = data.get("run_metadata") if isinstance(data.get("run_metadata"), dict) else {}
        if capability_match:
            run_metadata = {
                **run_metadata,
                "capability_match": capability_match,
            }
        run_metadata = {
            **run_metadata,
            "claim_mode": "manual_dispatch" if is_manual_dispatch else "agent_claim",
        }

        run = AgentRun(
            task_id=task.id,
            agent_id=agent.id,
            assignment=assignment,
            status=AgentRunStatus.RUNNING,
            started_at=now,
            input_snapshot=build_task_snapshot(task),
            run_metadata=run_metadata,
            created_by=current_user.email,
        )
        db.session.add(run)
        db.session.flush()

        record_task_event(
            task.id,
            "task_claimed",
            current_user=current_user if is_manual_dispatch else None,
            agent=None if is_manual_dispatch else agent,
            payload={
                "assignment_id": assignment.id,
                "agent_id": agent.id,
                "run_id": run.id,
                "lease_seconds": lease_seconds,
                "claim_mode": run_metadata["claim_mode"],
                "run_metadata": run_metadata,
                "capability_match": capability_match,
            },
        )

        db.session.commit()
        flush_sse_notifications()

        AuditLog.record(
            action="task.claimed", resource_type="task", resource_id=task.id,
            actor_type="agent" if not is_manual_dispatch else "human",
            actor_user_id=current_user.id if is_manual_dispatch else None,
            actor_agent_id=agent.id if not is_manual_dispatch else None,
            project_id=task.project_id,
            detail={"agent_id": agent.id, "assignment_id": assignment.id, "run_id": run.id},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.created(serialize_claim_response(agent, assignment, run), "Task claimed successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to claim task: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/assignments/<int:assignment_id>", methods=["PUT"])
@unified_auth_required
def update_assignment(agent_id, assignment_id):
    """Update assignment progress/state."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        assignment = TaskAssignment.query.filter_by(id=assignment_id, agent_id=agent.id).first()
        if not assignment:
            return ApiResponse.error("Assignment not found", 404).to_response()

        task, response = get_owned_task_or_response(assignment.task_id, current_user)
        if response:
            return response

        expired_assignment = expire_assignment(assignment)
        if expired_assignment:
            db.session.commit()
            return ApiResponse.error(
                "Assignment lease has expired",
                409,
                assignment=assignment.to_dict(include_task=True, include_agent=True),
            ).to_response()

        data = validate_json_request(
            optional_fields=[
                "state",
                "progress_rate",
                "notes",
                "feedback_content",
                "output_summary",
                "error",
                "lease_seconds",
                "task_status",
                "run_metadata",
            ],
        )

        if isinstance(data, tuple):
            return data

        try:
            run = apply_assignment_update(current_user, assignment, task, data, actor_agent=agent)
        except AssignmentUpdateError as e:
            record_assignment_update_rejected(current_user, assignment, data, agent, str(e))
            db.session.commit()
            return ApiResponse.error(str(e), e.status_code).to_response()
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            {
                "assignment": assignment.to_dict(include_task=True, include_agent=True),
                "run": run.to_dict() if run else None,
            },
            "Assignment updated successfully",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update assignment: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/assignments", methods=["GET"])
@unified_auth_required
def list_task_assignments(task_id):
    """List Agent assignments for a task."""
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        expire_stale_assignments_for_task(task.id)
        db.session.commit()

        args = get_request_args()
        query = TaskAssignment.query.filter_by(task_id=task.id)

        state = request.args.get("state")
        if state == "active":
            now = datetime.utcnow()
            query = query.filter(
                active_assignment_filter(now),
            )
        elif state:
            try:
                query = query.filter_by(state=parse_enum(TaskAssignmentState, state, "state"))
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        query = query.order_by(TaskAssignment.created_at.desc())
        result = paginate_serialized(
            query,
            args["page"],
            args["per_page"],
            lambda assignment: assignment.to_dict(include_task=False, include_agent=True, include_runs=True),
        )

        return ApiResponse.success(result, "Task assignments retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve task assignments: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/assignments/<int:assignment_id>", methods=["PUT"])
@unified_auth_required
def update_task_assignment(task_id, assignment_id):
    """Update a task assignment as the current human user."""
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        assignment = TaskAssignment.query.filter_by(id=assignment_id, task_id=task.id).first()
        if not assignment:
            return ApiResponse.error("Assignment not found", 404).to_response()

        expired_assignment = expire_assignment(assignment)
        if expired_assignment:
            db.session.commit()
            return ApiResponse.error(
                "Assignment lease has expired",
                409,
                assignment=assignment.to_dict(include_task=True, include_agent=True),
            ).to_response()

        data = validate_json_request(
            optional_fields=[
                "state",
                "progress_rate",
                "notes",
                "feedback_content",
                "output_summary",
                "error",
                "lease_seconds",
                "task_status",
                "run_metadata",
            ],
        )

        if isinstance(data, tuple):
            return data

        try:
            run = apply_assignment_update(current_user, assignment, task, data, actor_agent=None)
        except AssignmentUpdateError as e:
            record_assignment_update_rejected(current_user, assignment, data, None, str(e))
            db.session.commit()
            return ApiResponse.error(str(e), e.status_code).to_response()
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            {
                "assignment": assignment.to_dict(include_task=True, include_agent=True),
                "run": run.to_dict() if run else None,
            },
            "Task assignment updated successfully",
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update task assignment: {str(e)}", 500).to_response()
