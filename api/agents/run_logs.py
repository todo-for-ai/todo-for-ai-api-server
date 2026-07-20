"""
Agent collaboration API - run log routes.

Append and list execution run logs (structured agent output streaming).
"""

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    AgentRun,
    RunLog,
    Task,
    get_request_args,
    validate_json_request,
    flush_sse_notifications,
)


@agents_bp.route("/runs/<int:run_id>/logs", methods=["GET"])
@unified_auth_required
def list_run_logs(run_id):
    """Return log entries for a specific AgentRun (oldest first).

    Query params:
        since_id  – only return entries with id > since_id (incremental)
        level     – filter by level (debug/info/warn/error)
        per_page  – page size (default 100, max 500)
    """
    try:
        current_user = get_current_user()

        # Verify the run belongs to the user
        run = AgentRun.query.get(run_id)
        if not run:
            return ApiResponse.error("Run not found", 404).to_response()
        task = Task.query.get(run.task_id)
        if not task or task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied", 403).to_response()

        args = get_request_args()
        per_page = min(args["per_page"], 500)
        since_id = request.args.get("since_id", type=int)
        level_filter = request.args.get("level")

        query = RunLog.query.filter_by(run_id=run_id)
        if since_id:
            query = query.filter(RunLog.id > since_id)
        if level_filter and level_filter in _RUN_LOG_LEVELS:
            query = query.filter_by(level=level_filter)

        query = query.order_by(RunLog.id.asc())
        items = query.limit(per_page).all()

        latest_id = items[-1].id if items else (since_id or 0)

        return ApiResponse.success(
            {
                "items": [item.to_dict() for item in items],
                "latest_id": latest_id,
                "since_id": since_id,
                "run_id": run_id,
            },
            "Run logs retrieved",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve run logs: {str(e)}", 500).to_response()


@agents_bp.route("/runs/<int:run_id>/logs", methods=["POST"])
@unified_auth_required
def append_run_logs(run_id):
    """Append log entries to a specific AgentRun.

    Body (JSON):
        entries: list of { level, message, meta? }
    """
    try:
        current_user = get_current_user()

        run = AgentRun.query.get(run_id)
        if not run:
            return ApiResponse.error("Run not found", 404).to_response()
        task = Task.query.get(run.task_id)
        if not task or task.project.owner_id != current_user.id:
            return ApiResponse.error("Access denied", 403).to_response()

        data = validate_json_request(required_fields=["entries"])
        if isinstance(data, tuple):
            return data

        entries = data["entries"]
        if not isinstance(entries, list) or len(entries) > _RUN_LOG_MAX_PER_CALL:
            return ApiResponse.error(
                f"entries must be a list of at most {_RUN_LOG_MAX_PER_CALL} items", 400
            ).to_response()

        created = []
        for entry in entries:
            level = entry.get("level", "info")
            if level not in _RUN_LOG_LEVELS:
                level = "info"
            msg = str(entry.get("message", ""))
            meta = entry.get("meta")

            log = RunLog(run_id=run_id, level=level, message=msg, meta=meta)
            db.session.add(log)
            created.append(log)

        db.session.commit()
        return ApiResponse.created(
            [item.to_dict() for item in created],
            f"Appended {len(created)} log entries",
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to append run logs: {str(e)}", 500).to_response()


# ---------------------------------------------------------------------------
# Task Templates — reusable task blueprints
# ---------------------------------------------------------------------------



