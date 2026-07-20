"""
Agent dispatch policy, preview, and execution endpoints.
"""

from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentStatus,
    AgentRun,
    AgentRunStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Project,
    AuditLog,
    get_request_args,
    paginate_query,
    parse_enum,
    record_task_event,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    find_claimable_task,
    serialize_claim_response,
    expire_stale_assignments,
    mark_stale_agents_offline,
    normalize_dispatch_policy,
    get_coordinator_dispatch_policy,
    resolve_dispatch_options,
    collect_claimable_tasks,
    find_available_worker_agents,
    serialize_dispatch_candidate,
    create_assignment_with_run,
)

@agents_bp.route("/<int:agent_id>/dispatch/policy", methods=["GET"])
@unified_auth_required
def get_dispatch_policy(agent_id):
    """Return the reusable dispatch policy stored on a coordinator Agent."""
    try:
        current_user = get_current_user()
        coordinator, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if coordinator.kind != AgentKind.COORDINATOR:
            return ApiResponse.error("Dispatch policy is only available for coordinator Agents", 400).to_response()

        policy = get_coordinator_dispatch_policy(coordinator, current_user=current_user)
        return ApiResponse.success({"policy": policy}, "Dispatch policy retrieved").to_response()

    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve dispatch policy: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/dispatch/policy", methods=["PUT"])
@unified_auth_required
def update_dispatch_policy(agent_id):
    """Persist default task-distribution rules for a coordinator Agent."""
    try:
        current_user = get_current_user()
        coordinator, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if coordinator.kind != AgentKind.COORDINATOR:
            return ApiResponse.error("Dispatch policy is only available for coordinator Agents", 400).to_response()

        data = request.get_json(silent=True) or {}
        if "policy" in data:
            if not isinstance(data["policy"], dict):
                raise ValueError("policy must be an object")
            data = data["policy"]

        current_policy = get_coordinator_dispatch_policy(coordinator, current_user=current_user)
        policy = normalize_dispatch_policy({**current_policy, **data}, current_user=current_user)
        config = dict(coordinator.config or {})
        config["dispatch_policy"] = policy
        coordinator.config = config
        db.session.commit()

        _queue_sse(
            current_user.id,
            "agent_config_changed",
            {"agent_id": coordinator.id, "agent_name": coordinator.name, "changed_fields": ["dispatch_policy"]},
        )
        Notification.create_notification(
            user_id=current_user.id,
            event_type="agent_config_changed",
            agent_id=coordinator.id,
            payload={"changed_fields": ["dispatch_policy"]},
        )
        AuditLog.record(
            action="agent.dispatch_policy.updated", resource_type="agent", resource_id=coordinator.id,
            actor_type="human", actor_user_id=current_user.id,
            detail={"dispatch_policy": policy},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.success({"policy": policy, "coordinator": coordinator.to_dict(include_stats=True)}, "Dispatch policy updated").to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update dispatch policy: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/dispatch/preview", methods=["POST"])
@unified_auth_required
def preview_dispatch_tasks(agent_id):
    """Dry-run coordinator dispatch without creating assignments or runs."""
    try:
        current_user = get_current_user()
        coordinator, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if coordinator.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Coordinator Agent is not available to dispatch tasks", 409).to_response()

        data = request.get_json(silent=True) or {}
        options, policy = resolve_dispatch_options(coordinator, data, current_user=current_user)
        max_assignments = options["max_assignments"]
        match_capabilities = options["match_capabilities"]
        require_capability_match = options["require_capability_match"]
        include_self = options["include_self"]
        candidate_agent_ids = options["candidate_agent_ids"]
        project_id = options["project_id"]

        mark_stale_agents_offline(owner_id=current_user.id)
        workers = find_available_worker_agents(
            current_user, coordinator, candidate_agent_ids=candidate_agent_ids, include_self=include_self
        )
        tasks = collect_claimable_tasks(current_user, project_id=project_id)

        result = {
            "coordinator": coordinator.to_dict(include_stats=True),
            "proposed_assignments": [],
            "task_candidates": [],
            "unmatched_tasks": [],
            "summary": {
                "claimable_tasks": len(tasks),
                "available_agents": len(workers),
                "planned": 0,
                "skipped_no_match": 0,
                "skipped_capacity": 0,
                "max_assignments": max_assignments,
            },
            "policy": policy,
            "options": options,
        }

        if not workers or not tasks:
            db.session.rollback()
            return ApiResponse.success(result, "Dispatch preview generated").to_response()

        scored = []
        candidates_by_task = {}
        for task in tasks:
            task_candidates = []
            for worker in workers:
                match = (
                    score_task_for_agent(task, worker)
                    if match_capabilities
                    else {"score": 0, "matched_capabilities": [], "matched_tags": [], "matched_text": [], "missing_required": []}
                )
                if not require_capability_match or match["score"] > 0:
                    task_candidates.append((worker, match))
                    scored.append((task, worker, match))

            task_candidates.sort(key=lambda item: -item[1]["score"])
            candidates_by_task[task.id] = task_candidates
            result["task_candidates"].append(
                {
                    "task": task.to_dict(include_project=True),
                    "candidates": [
                        serialize_dispatch_candidate(worker, match, match_capabilities)
                        for worker, match in task_candidates[:DISPATCH_PREVIEW_CANDIDATE_LIMIT]
                    ],
                }
            )

        scored.sort(key=lambda item: -item[2]["score"])

        used_tasks = set()
        used_agents = set()
        for task, worker, match in scored:
            if len(result["proposed_assignments"]) >= max_assignments:
                break
            if task.id in used_tasks or worker.id in used_agents:
                continue

            candidate = serialize_dispatch_candidate(worker, match, match_capabilities)
            result["proposed_assignments"].append(
                {
                    "task": task.to_dict(include_project=True),
                    **candidate,
                }
            )
            used_tasks.add(task.id)
            used_agents.add(worker.id)

        for task in tasks:
            if task.id in used_tasks:
                continue
            candidates = candidates_by_task.get(task.id, [])
            reason = "no_matching_agent" if not candidates else "agent_capacity_exhausted"
            result["unmatched_tasks"].append(
                {
                    "task": task.to_dict(include_project=True),
                    "reason": reason,
                    "candidate_count": len(candidates),
                    "best_candidate": (
                        serialize_dispatch_candidate(candidates[0][0], candidates[0][1], match_capabilities)
                        if candidates else None
                    ),
                }
            )

        result["summary"]["planned"] = len(result["proposed_assignments"])
        result["summary"]["skipped_no_match"] = sum(
            1 for item in result["unmatched_tasks"] if item["reason"] == "no_matching_agent"
        )
        result["summary"]["skipped_capacity"] = sum(
            1 for item in result["unmatched_tasks"] if item["reason"] == "agent_capacity_exhausted"
        )

        # Preview may mark stale agents or expire stale assignments inside the
        # session to evaluate the current dispatch pool, but it must not persist
        # those maintenance writes.
        db.session.rollback()
        return ApiResponse.success(result, "Dispatch preview generated").to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to preview dispatch: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/dispatch", methods=["POST"])
@unified_auth_required
def dispatch_tasks(agent_id):
    """Coordinator auto-dispatch: assign claimable tasks to suitable worker Agents.

    This is the orchestration primitive of the multi-Agent platform. A coordinator
    Agent distributes the owner's unassigned, claimable tasks across available
    (online, idle) worker Agents using capability scoring. Each match becomes a
    fresh assignment + run, and a ``task_dispatched`` event records that the
    coordinator handed the work out, so the timeline shows who routed what to whom.

    Matching is greedy by capability score; each worker takes at most one task per
    round to spread load. Body options: ``project_id``, ``max_assignments``,
    ``lease_seconds``, ``match_capabilities``, ``require_capability_match``,
    ``candidate_agent_ids``, ``include_self``.
    """
    try:
        current_user = get_current_user()
        coordinator, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        if coordinator.status in [AgentStatus.DISABLED, AgentStatus.PAUSED]:
            return ApiResponse.error("Coordinator Agent is not available to dispatch tasks", 409).to_response()

        data = request.get_json(silent=True) or {}
        options, policy = resolve_dispatch_options(coordinator, data, current_user=current_user)
        lease_seconds = options["lease_seconds"]
        max_assignments = options["max_assignments"]
        match_capabilities = options["match_capabilities"]
        require_capability_match = options["require_capability_match"]
        include_self = options["include_self"]
        candidate_agent_ids = options["candidate_agent_ids"]
        project_id = options["project_id"]

        now = datetime.utcnow()
        mark_stale_agents_offline(owner_id=current_user.id)

        workers = find_available_worker_agents(
            current_user, coordinator, candidate_agent_ids=candidate_agent_ids, include_self=include_self
        )
        tasks = collect_claimable_tasks(current_user, project_id=project_id)

        result = {
            "coordinator": coordinator.to_dict(include_stats=True),
            "assignments": [],
            "summary": {
                "claimable_tasks": len(tasks),
                "available_agents": len(workers),
                "dispatched": 0,
                "skipped_no_match": 0,
            },
            "policy": policy,
            "options": options,
        }

        if not workers or not tasks:
            db.session.commit()
            return ApiResponse.success(result, "No tasks dispatched").to_response()

        # Greedy best-score matching: each worker takes at most one task this round.
        scored = []
        for task in tasks:
            for worker in workers:
                match = (
                    score_task_for_agent(task, worker)
                    if match_capabilities
                    else {"score": 0, "matched_capabilities": [], "matched_tags": [], "matched_text": []}
                )
                scored.append((task, worker, match))

        scored.sort(key=lambda item: -item[2]["score"])

        used_tasks = set()
        used_agents = set()
        for task, worker, match in scored:
            if len(result["assignments"]) >= max_assignments:
                break
            if task.id in used_tasks or worker.id in used_agents:
                continue
            if require_capability_match and match["score"] <= 0:
                continue

            strategy = "capability_match" if (match_capabilities and match["score"] > 0) else "priority_fifo"
            run_metadata = {
                "claim_mode": "auto_dispatch",
                "dispatched_by_agent_id": coordinator.id,
                "capability_match": {**match, "strategy": strategy},
            }
            assignment, run = create_assignment_with_run(
                task, worker, current_user, now, lease_seconds, run_metadata
            )
            if task.status == TaskStatus.TODO:
                task.status = TaskStatus.IN_PROGRESS

            db.session.flush()

            record_task_event(
                task.id,
                "task_dispatched",
                agent=coordinator,
                payload={
                    "assignment_id": assignment.id,
                    "run_id": run.id,
                    "to_agent_id": worker.id,
                    "dispatched_by_agent_id": coordinator.id,
                    "lease_seconds": lease_seconds,
                    "strategy": strategy,
                    "score": match["score"],
                    "matched_capabilities": match["matched_capabilities"],
                    "content": f"Dispatched to {worker.name}",
                },
            )

            used_tasks.add(task.id)
            used_agents.add(worker.id)
            result["assignments"].append(
                {
                    "assignment": assignment.to_dict(include_task=True, include_agent=True),
                    "run": run.to_dict(),
                    "agent": worker.to_dict(include_stats=False),
                    "strategy": strategy,
                    "score": match["score"],
                    "matched_capabilities": match["matched_capabilities"],
                }
            )

        result["summary"]["dispatched"] = len(result["assignments"])
        result["summary"]["skipped_no_match"] = max(0, len(tasks) - len(used_tasks))

        db.session.commit()
        flush_sse_notifications()

        return ApiResponse.success(
            result,
            f"Dispatched {len(result['assignments'])} task(s)",
            201 if result["assignments"] else 200,
        ).to_response()

    except ValueError as e:
        db.session.rollback()
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to dispatch tasks: {str(e)}", 500).to_response()

