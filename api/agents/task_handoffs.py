"""
Agent collaboration API - task handoff and subtask routes.

Task handoff (live work transfer between Agents) and Agent-driven task
decomposition (subtask creation).
"""

from datetime import datetime

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    AuditLog,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskEvent,
    TaskStatus,
    validate_json_request,
    get_owned_agent_or_response,
    get_owned_task_or_response,
    record_task_event,
    expire_stale_assignments_for_task,
    find_active_assignment,
    flush_sse_notifications,
    _client_ip,
    parse_enum,
)
from ._dispatch_helpers import create_assignment_with_run, cancel_assignment_for_handoff


@agents_bp.route("/tasks/<int:task_id>/handoff", methods=["POST"])
@unified_auth_required
def handoff_task(task_id):
    """Hand off a task from its current Agent to another Agent.

    This is the collaboration primitive for transferring live work: the active
    assignment (if any) is cancelled and a fresh assignment + run is created for
    the target Agent, with a ``handoff`` event recording the transfer so the
    timeline shows who passed the work to whom and why.
    """
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        data = validate_json_request(
            required_fields=["to_agent_id"],
            optional_fields=["from_assignment_id", "lease_seconds", "reason", "notes"],
        )
        if isinstance(data, tuple):
            return data

        target_agent, response = get_owned_agent_or_response(data["to_agent_id"], current_user)
        if response:
            return response

        if target_agent.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Target Agent is not available to receive a handoff", 409).to_response()

        lease_seconds = int(data.get("lease_seconds") or 1800)
        lease_seconds = max(60, min(lease_seconds, 24 * 60 * 60))

        now = datetime.utcnow()
        expire_stale_assignments_for_task(task.id)

        source_assignment = find_active_assignment(task.id)
        if data.get("from_assignment_id") is not None:
            requested = TaskAssignment.query.filter_by(
                id=data["from_assignment_id"], task_id=task.id
            ).first()
            if not requested:
                return ApiResponse.error("Source assignment not found", 404).to_response()
            if source_assignment and requested.id != source_assignment.id:
                return ApiResponse.error(
                    "from_assignment_id does not match the task's active assignment", 409
                ).to_response()
            source_assignment = requested

        if source_assignment and source_assignment.agent_id == target_agent.id:
            return ApiResponse.error("Task is already assigned to the target Agent", 409).to_response()

        from_agent_id = source_assignment.agent_id if source_assignment else None
        if source_assignment and not source_assignment.is_terminal:
            cancel_assignment_for_handoff(source_assignment, now)

        reason = data.get("reason")
        notes = data.get("notes")
        run_metadata = {
            "claim_mode": "handoff",
            "handoff": {
                "from_agent_id": from_agent_id,
                "from_assignment_id": source_assignment.id if source_assignment else None,
                "reason": reason,
            },
        }

        assignment, run = create_assignment_with_run(
            task, target_agent, current_user, now, lease_seconds, run_metadata
        )
        if notes:
            assignment.notes = notes

        if task.status == TaskStatus.TODO:
            task.status = TaskStatus.IN_PROGRESS

        db.session.flush()

        record_task_event(
            task.id,
            "handoff",
            current_user=current_user,
            payload={
                "from_agent_id": from_agent_id,
                "from_assignment_id": source_assignment.id if source_assignment else None,
                "to_agent_id": target_agent.id,
                "assignment_id": assignment.id,
                "run_id": run.id,
                "lease_seconds": lease_seconds,
                "reason": reason,
                "content": reason or notes,
            },
        )

        db.session.commit()
        flush_sse_notifications()

        AuditLog.record(
            action="task.handoff", resource_type="task", resource_id=task_id,
            actor_type="human", actor_user_id=current_user.id,
            project_id=task.project_id,
            detail={
                "from_agent_id": source_assignment.agent_id if source_assignment else None,
                "to_agent_id": to_agent.id,
                "assignment_id": assignment.id,
            },
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.success(
            {
                "from_assignment": source_assignment.to_dict(include_agent=True) if source_assignment else None,
                "assignment": assignment.to_dict(include_task=True, include_agent=True),
                "run": run.to_dict(),
            },
            "Task handed off successfully",
            201,
        ).to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to hand off task: {str(e)}", 500).to_response()



@agents_bp.route("/tasks/<int:task_id>/subtasks", methods=["POST"])
@unified_auth_required
def create_subtask(task_id):
    """Create a child task under a parent task (Agent-driven task decomposition).

    This is the multi-Agent decomposition primitive: an Agent can break a large
    task into smaller pieces that other Agents can claim independently. The
    parent-child relationship is recorded via ``parent_task_id`` and a
    ``subtask_created`` event is posted to the parent's timeline.
    """
    try:
        current_user = get_current_user()
        parent, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        data = validate_json_request(
            required_fields=["title"],
            optional_fields=["content", "priority", "tags", "agent_id"],
        )
        if isinstance(data, tuple):
            return data

        title = data["title"].strip()
        if not title or len(title) > 500:
            return ApiResponse.error("title must be 1-500 characters", 400).to_response()

        actor_agent = None
        if data.get("agent_id"):
            actor_agent, response = get_owned_agent_or_response(data["agent_id"], current_user)
            if response:
                return response

        subtask = Task(
            project_id=parent.project_id,
            owner_id=current_user.id,
            title=title,
            content=data.get("content"),
            priority=parse_enum(TaskPriority, data.get("priority", TaskPriority.MEDIUM.value), "priority"),
            tags=data.get("tags") if isinstance(data.get("tags"), list) else None,
            parent_task_id=parent.id,
            is_ai_task=True,
            creator_type="agent" if actor_agent else "human",
            creator_identifier=str(actor_agent.id) if actor_agent else current_user.email,
            created_by=current_user.email,
        )
        db.session.add(subtask)

        record_task_event(
            parent.id,
            "subtask_created",
            current_user=current_user if not actor_agent else None,
            agent=actor_agent,
            payload={
                "subtask_id": None,  # filled after flush
                "subtask_title": title,
                "content": f"Created subtask: {title}",
            },
        )

        db.session.flush()

        # Back-fill subtask_id
        parent_event = TaskEvent.query.filter_by(task_id=parent.id).order_by(TaskEvent.id.desc()).first()
        if parent_event and parent_event.payload.get("subtask_title") == title:
            p = dict(parent_event.payload)
            p["subtask_id"] = subtask.id
            parent_event.payload = p
            db.session.add(parent_event)

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            subtask.to_dict(include_project=True),
            "Subtask created successfully",
            201,
        ).to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create subtask: {str(e)}", 500).to_response()

