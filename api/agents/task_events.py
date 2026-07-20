"""
Agent collaboration API - task event routes.

Task collaboration events: list and post events for a task.
"""

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    Task,
    TaskEvent,
    get_request_args,
    paginate_query,
    validate_json_request,
    get_owned_agent_or_response,
    get_owned_task_or_response,
    record_task_event,
    expire_stale_assignments_for_task,
    flush_sse_notifications,
    POSTABLE_EVENT_TYPES,
    POSTABLE_EVENT_CONTENT_MAX,
)


@agents_bp.route("/tasks/<int:task_id>/events", methods=["GET"])
@unified_auth_required
def list_task_events(task_id):
    """List collaboration events for a task."""
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        expired_assignments = expire_stale_assignments_for_task(task.id)
        if expired_assignments:
            db.session.commit()

        args = get_request_args()
        query = TaskEvent.query.filter_by(task_id=task.id)

        # Incremental polling: only return events newer than a known id. This
        # lets the UI / Agents cheaply poll for live collaboration updates.
        since_id = request.args.get("since_id", type=int)
        if since_id:
            query = query.filter(TaskEvent.id > since_id)
            events = query.order_by(TaskEvent.id.asc()).limit(args["per_page"]).all()
            latest_id = events[-1].id if events else since_id
            return ApiResponse.success(
                {
                    "items": [event.to_dict() for event in events],
                    "latest_id": latest_id,
                    "since_id": since_id,
                },
                "Task events retrieved successfully",
            ).to_response()

        query = query.order_by(TaskEvent.created_at.desc())
        result = paginate_query(query, args["page"], args["per_page"])

        return ApiResponse.success(result, "Task events retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve task events: {str(e)}", 500).to_response()


@agents_bp.route("/tasks/<int:task_id>/events", methods=["POST"])
@unified_auth_required
def post_task_event(task_id):
    """Post a collaboration message to a task timeline as a human or an Agent.

    This is the inter-agent communication primitive: an Agent (identified by
    ``agent_id``, which must belong to the caller) or the human owner can leave
    messages, hand off work, raise blockers, or record decisions that other
    Agents read via ``GET /tasks/<id>/events``.
    """
    try:
        current_user = get_current_user()
        task, response = get_owned_task_or_response(task_id, current_user)
        if response:
            return response

        data = validate_json_request(
            optional_fields=["event_type", "content", "agent_id", "to_agent_id", "payload"],
        )
        if isinstance(data, tuple):
            return data

        event_type = (data.get("event_type") or "message").strip().lower()
        if event_type not in POSTABLE_EVENT_TYPES:
            valid = ", ".join(sorted(POSTABLE_EVENT_TYPES))
            return ApiResponse.error(
                f"Invalid event_type. Must be one of: {valid}", 400
            ).to_response()

        content = data.get("content")
        if content is not None and not isinstance(content, str):
            return ApiResponse.error("content must be a string", 400).to_response()
        if content is not None:
            content = content.strip()
            if len(content) > POSTABLE_EVENT_CONTENT_MAX:
                return ApiResponse.error(
                    f"content exceeds {POSTABLE_EVENT_CONTENT_MAX} characters", 400
                ).to_response()

        extra_payload = data.get("payload")
        if extra_payload is not None and not isinstance(extra_payload, dict):
            return ApiResponse.error("payload must be an object", 400).to_response()

        if not content and not extra_payload:
            return ApiResponse.error("content or payload is required", 400).to_response()

        actor_agent = None
        agent_id = data.get("agent_id")
        if agent_id is not None:
            actor_agent, response = get_owned_agent_or_response(agent_id, current_user)
            if response:
                return response

        # Optional directed @mention: address this message to a specific Agent so
        # it surfaces in that Agent's inbox (GET /agents/<id>/inbox).
        target_agent = None
        to_agent_id = data.get("to_agent_id")
        if to_agent_id is not None:
            target_agent, response = get_owned_agent_or_response(to_agent_id, current_user)
            if response:
                return response

        payload = dict(extra_payload) if extra_payload else {}
        if content:
            payload["content"] = content
        if target_agent is not None:
            payload["to_agent_id"] = target_agent.id
            payload["to_agent_name"] = target_agent.name

        # --- request/response pairing for question/answer protocol ---
        # When an Agent posts a "question", mark it as awaiting an answer so
        # other Agents / the inbox can surface it.  When an "answer" is posted,
        # automatically link it to the most recent unanswered question on this
        # task directed at the answering Agent (or the most recent question
        # overall if to_agent_id is not set).
        if event_type == "question":
            payload["awaiting_answer"] = True

        if event_type == "answer":
            # Find the most recent unanswered question on this task.
            # Use Python-side filtering for JSON payload compatibility across
            # SQLite and PostgreSQL.
            recent_questions = (
                TaskEvent.query
                .filter(TaskEvent.task_id == task.id, TaskEvent.event_type == "question")
                .order_by(TaskEvent.id.desc())
                .limit(20)
                .all()
            )
            latest_question = None
            for q in recent_questions:
                q_payload = q.payload or {}
                if not q_payload.get("awaiting_answer", False):
                    continue
                if actor_agent and q_payload.get("to_agent_id") == actor_agent.id:
                    latest_question = q
                    break
                if not actor_agent and not q_payload.get("to_agent_id"):
                    latest_question = q
                    break
            # Fallback: any unanswered question
            if not latest_question:
                for q in recent_questions:
                    if (q.payload or {}).get("awaiting_answer", False):
                        latest_question = q
                        break
            if latest_question:
                payload["reply_to_event_id"] = latest_question.id
                # Mark the question as answered
                q_payload = dict(latest_question.payload or {})
                q_payload["awaiting_answer"] = False
                q_payload["answered_by_event_id"] = None  # filled after flush
                latest_question.payload = q_payload
                db.session.add(latest_question)
                # We'll fill answered_by_event_id after we get our event id
                payload["_pending_answered_question_id"] = latest_question.id

        event = record_task_event(
            task.id,
            event_type,
            current_user=current_user if not actor_agent else None,
            agent=actor_agent,
            payload=payload,
        )

        # Back-fill answered_by_event_id on the original question
        pending_q_id = payload.pop("_pending_answered_question_id", None)
        if pending_q_id and event.id:
            q_event = TaskEvent.query.get(pending_q_id)
            if q_event:
                q_payload = dict(q_event.payload or {})
                q_payload["answered_by_event_id"] = event.id
                q_event.payload = q_payload
                db.session.add(q_event)

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            event.to_dict(), "Task event posted successfully", 201
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to post task event: {str(e)}", 500).to_response()


