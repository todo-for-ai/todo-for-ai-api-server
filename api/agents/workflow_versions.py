"""
Agent collaboration API — workflow versioning and step override routes.

Handles workflow version management (list/get/rollback/diff),
step parameter overrides, and step dependency bottleneck analysis.
"""

from datetime import datetime, timedelta

from flask import request

from ._shared import (  # noqa: E402
    agents_bp,
    ApiResponse,
    get_request_args,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentRun,
    AuditLog,
    Notification,
    Project,
    StepStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskEvent,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    WorkflowTrigger,
    WorkflowVersion,
    AgentChannel,
    AgentChannelMessage,
    CollaborationTemplate,
    KnowledgeEntry,
    AgentReputation,
    AgentExperience,
    CrossProjectAgent,
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxExecutionStatus,
    AgentConflict,
    OrchestrationRun,
    HUMAN_BLOCKING_ASSIGNMENT_STATES,
    LEASED_EXECUTION_STATES,
    notify_sse,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    POSTABLE_EVENT_TYPES,
    POSTABLE_EVENT_CONTENT_MAX,
    ACTIVE_ASSIGNMENT_STATES,
    record_task_event,
    apply_assignment_update,
)
@agents_bp.route("/workflows/<int:workflow_id>/versions", methods=["GET"])
@unified_auth_required
def list_workflow_versions(workflow_id):
    """List version history for a workflow."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    versions = WorkflowVersion.query.filter_by(workflow_id=workflow_id).order_by(
        WorkflowVersion.version_number.desc()
    ).all()
    return ApiResponse.success({
        "current_version": workflow.version,
        "versions": [v.to_dict() for v in versions],
    }).to_response()


@agents_bp.route("/workflows/<int:workflow_id>/versions/<int:version_number>", methods=["GET"])
@unified_auth_required
def get_workflow_version(workflow_id, version_number):
    """Get a specific workflow version snapshot."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    version = WorkflowVersion.query.filter_by(
        workflow_id=workflow_id, version_number=version_number
    ).first()
    if not version:
        return ApiResponse.not_found("Version not found").to_response()

    return ApiResponse.success(version.to_dict()).to_response()


@agents_bp.route("/workflows/<int:workflow_id>/rollback", methods=["POST"])
@unified_auth_required
def rollback_workflow(workflow_id):
    """Rollback a workflow to a specific version.

    Creates a snapshot of the current version before rolling back.
    """
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    data = validate_json_request()
    target_version = data.get("version")
    if not target_version:
        return ApiResponse.error("version is required", 400).to_response()

    target = WorkflowVersion.query.filter_by(
        workflow_id=workflow_id, version_number=target_version
    ).first()
    if not target:
        return ApiResponse.not_found(f"Version {target_version} not found").to_response()

    # Snapshot current before rollback
    current_version = workflow.version or 1
    WorkflowVersion.create(
        workflow_id=workflow.id,
        version_number=current_version,
        definition=workflow.definition or {},
        steps_snapshot=[s.to_dict() for s in workflow.steps],
        change_summary=f"Auto-snapshot before rollback (v{current_version} → v{target_version})",
        created_by=user.email,
    )

    # Apply target version
    workflow.version = current_version + 1  # New version number
    workflow.definition = target.definition or {}

    # Replace steps with the snapshot
    for old_step in workflow.steps:
        db.session.delete(old_step)
    for step_data in (target.steps_snapshot or []):
        step_key = step_data.get("step_key", "").strip()
        if not step_key:
            continue
        WorkflowStep.create(
            workflow_id=workflow.id,
            step_key=step_key,
            name=step_data.get("name", step_key),
            description=step_data.get("description", ""),
            order=step_data.get("order", 0),
            required_capabilities=step_data.get("required_capabilities", []),
            agent_id=step_data.get("agent_id"),
            task_template_id=step_data.get("task_template_id"),
            depends_on=step_data.get("depends_on", []),
            condition=step_data.get("condition"),
            sub_workflow_id=step_data.get("sub_workflow_id"),
            timeout_seconds=step_data.get("timeout_seconds"),
            retry_count=step_data.get("retry_count", 0),
            on_failure=step_data.get("on_failure", "abort"),
        )

    db.session.commit()

    AuditLog.record("workflow_rollback", target_type="workflow",
                     target_id=workflow.id, actor_type="human", actor_user_id=user.id,
                     detail={"from_version": current_version, "to_version": target_version,
                             "new_version_number": workflow.version},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.success(
        workflow.to_dict(include_steps=True),
        f"Rolled back to version {target_version} (now at v{workflow.version})"
    ).to_response()


@agents_bp.route("/workflows/<int:workflow_id>/diff/<int:v1>/<int:v2>", methods=["GET"])
@unified_auth_required
def diff_workflow_versions(workflow_id, v1, v2):
    """Compare two versions of a workflow. Returns a summary of differences."""
    user = get_current_user()
    workflow = _workflow_owned_by_user(workflow_id, user)
    if not workflow:
        return ApiResponse.not_found("Workflow not found").to_response()

    ver1 = WorkflowVersion.query.filter_by(workflow_id=workflow_id, version_number=v1).first()
    ver2 = WorkflowVersion.query.filter_by(workflow_id=workflow_id, version_number=v2).first()
    if not ver1 or not ver2:
        return ApiResponse.not_found("One or both versions not found").to_response()

    # Compute step-level diff
    steps1 = {s["step_key"]: s for s in (ver1.steps_snapshot or [])}
    steps2 = {s["step_key"]: s for s in (ver2.steps_snapshot or [])}

    added = [k for k in steps2 if k not in steps1]
    removed = [k for k in steps1 if k not in steps2]
    modified = []
    for k in steps1:
        if k in steps2 and steps1[k] != steps2[k]:
            modified.append(k)

    return ApiResponse.success({
        "v1": v1,
        "v2": v2,
        "added_steps": added,
        "removed_steps": removed,
        "modified_steps": modified,
        "v1_definition": ver1.definition,
        "v2_definition": ver2.definition,
        "v1_change_summary": ver1.change_summary,
        "v2_change_summary": ver2.change_summary,
    }).to_response()


# =========================================================================
# Collaboration Protocols (Proposal / Vote / Consensus / Auction / Handoff)
# =========================================================================






# ---------------------------------------------------------------------------
# Increment 85: Agent collaboration sandbox — secure execution isolation
# ---------------------------------------------------------------------------

_VALID_SANDBOX_LEVELS = {"strict", "moderate", "permissive"}


def _sandbox_body(body, partial=False):
    """Extract and validate sandbox fields from a request body."""
    fields = {
        "name": body.get("name"),
        "description": body.get("description"),
        "agent_id": body.get("agent_id"),
        "security_level": body.get("security_level", "moderate"),
        "allowed_tools": body.get("allowed_tools", []),
        "blocked_tools": body.get("blocked_tools", []),
        "allowed_network_hosts": body.get("allowed_network_hosts", []),
        "fs_write_paths": body.get("fs_write_paths", []),
        "fs_read_paths": body.get("fs_read_paths", []),
        "max_memory_mb": body.get("max_memory_mb", 0),
        "max_cpu_seconds": body.get("max_cpu_seconds", 0),
        "max_output_tokens": body.get("max_output_tokens", 0),
        "timeout_seconds": body.get("timeout_seconds", 0),
        "is_active": body.get("is_active", True),
    }
    if not partial:
        if not fields["name"]:
            return None, "Sandbox name is required"
    if fields["security_level"] not in _VALID_SANDBOX_LEVELS:
        return None, f"security_level must be one of {_VALID_SANDBOX_LEVELS}"
    # Coerce list fields
    for k in ("allowed_tools", "blocked_tools", "allowed_network_hosts", "fs_write_paths", "fs_read_paths"):
        if fields[k] is None:
            fields[k] = []
        elif not isinstance(fields[k], list):
            return None, f"{k} must be a list"
    # Coerce int fields
    for k in ("max_memory_mb", "max_cpu_seconds", "max_output_tokens", "timeout_seconds"):
        try:
            fields[k] = int(fields[k] or 0)
        except (TypeError, ValueError):
            return None, f"{k} must be an integer"
    if fields["agent_id"] is not None:
        try:
            fields["agent_id"] = int(fields["agent_id"])
        except (TypeError, ValueError):
            return None, "agent_id must be an integer"
    return fields, None


# ---------------------------------------------------------------------------
# Preset sandbox policy templates (Increment 90)
# ---------------------------------------------------------------------------


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/override", methods=["PUT"])
@unified_auth_required
def set_step_runtime_override(run_id, step_key):
    """Dynamically reconfigure a not-yet-terminal step within a running workflow.

    Sets runtime overrides on the WorkflowStepRun that take precedence over the
    workflow definition when the step starts. Only allowed while the step is
    still PENDING/WAITING (not yet started) — except for timeout_seconds, which
    may be adjusted on a RUNNING step.

    Body: { overrides: { agent_id?, required_capabilities?, timeout_seconds?,
             retry_count?, on_failure?, condition?, task_template_id?,
             sub_workflow_id? }, merge?: bool (default true) }
    """
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    if sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
        return ApiResponse.error(f"Cannot override a terminal step (status={sr.status.value})").to_response()

    body = validate_json_request()
    overrides = body.get("overrides") or {}
    if not isinstance(overrides, dict) or not overrides:
        return ApiResponse.error("overrides must be a non-empty object").to_response()

    # Validate keys and value types
    validated = {}
    for k, v in overrides.items():
        if k not in _RUNTIME_OVERRIDABLE_KEYS:
            return ApiResponse.error(f"Cannot override '{k}' (not in allowlist)").to_response()
        if k in ("agent_id", "task_template_id", "sub_workflow_id", "timeout_seconds", "retry_count"):
            if v is not None:
                try:
                    validated[k] = int(v)
                except (TypeError, ValueError):
                    return ApiResponse.error(f"{k} must be an integer or null").to_response()
            else:
                validated[k] = None
        elif k == "required_capabilities":
            if not isinstance(v, list):
                return ApiResponse.error("required_capabilities must be a list").to_response()
            validated[k] = v
        elif k == "on_failure":
            if v not in ("abort", "skip", "continue"):
                return ApiResponse.error("on_failure must be abort|skip|continue").to_response()
            validated[k] = v
        elif k == "condition":
            if v is not None and not isinstance(v, dict):
                return ApiResponse.error("condition must be an object or null").to_response()
            validated[k] = v

    # Reject reconfig of start-time-only params on a RUNNING step
    start_time_only = {"agent_id", "required_capabilities", "task_template_id", "sub_workflow_id", "condition", "on_failure"}
    if sr.status == StepStatus.RUNNING:
        forbidden = set(validated.keys()) & start_time_only
        if forbidden:
            return ApiResponse.error(
                f"Cannot override {sorted(forbidden)} on a running step (only timeout_seconds/retry_count allowed)"
            ).to_response()

    merge = body.get("merge", True)
    if merge:
        current = dict(sr.runtime_overrides or {})
        current.update(validated)
        sr.runtime_overrides = current
    else:
        sr.runtime_overrides = validated

    AuditLog.record(
        action="workflow_step_overridden", resource_type="workflow_step_run", resource_id=sr.id,
        actor_type="human", actor_user_id=user.id, project_id=wf_run.project_id,
        detail={"run_id": run_id, "step_key": step_key, "overrides": validated, "merge": merge},
    )
    db.session.commit()
    _queue_sse(user.id, "workflow_step_overridden", {
        "run_id": run_id, "step_key": step_key, "overrides": validated,
    })
    flush_sse_notifications()

    # Compute effective params for the response
    effective = {}
    for k in _RUNTIME_OVERRIDABLE_KEYS:
        effective[k] = sr.get_effective_param(k)
    return ApiResponse.success({
        "step_run": sr.to_dict(),
        "overrides": sr.runtime_overrides or {},
        "effective_params": effective,
    }, "Step runtime overrides applied").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/override", methods=["DELETE"])
@unified_auth_required
def clear_step_runtime_override(run_id, step_key):
    """Clear runtime overrides for a step run, reverting to the workflow definition."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    sr.runtime_overrides = {}
    AuditLog.record(
        action="workflow_step_override_cleared", resource_type="workflow_step_run", resource_id=sr.id,
        actor_type="human", actor_user_id=user.id, project_id=wf_run.project_id,
        detail={"run_id": run_id, "step_key": step_key},
    )
    db.session.commit()
    effective = {}
    for k in _RUNTIME_OVERRIDABLE_KEYS:
        effective[k] = sr.get_effective_param(k)
    return ApiResponse.success({
        "step_run": sr.to_dict(),
        "effective_params": effective,
    }, "Step runtime overrides cleared").to_response()


@agents_bp.route("/workflow-runs/<int:run_id>/steps/<step_key>/effective-params", methods=["GET"])
@unified_auth_required
def get_step_effective_params(run_id, step_key):
    """Get the effective parameters for a step run (overrides merged with definition)."""
    user = get_current_user()
    wf_run = WorkflowRun.query.filter_by(id=run_id, owner_id=user.id).first()
    if not wf_run:
        return ApiResponse.not_found("Workflow run not found").to_response()
    sr = WorkflowStepRun.query.filter_by(run_id=run_id, step_key=step_key).first()
    if not sr:
        return ApiResponse.not_found("Step run not found").to_response()
    effective = {}
    for k in _RUNTIME_OVERRIDABLE_KEYS:
        effective[k] = sr.get_effective_param(k)
    return ApiResponse.success({
        "step_run": sr.to_dict(),
        "overrides": sr.runtime_overrides or {},
        "effective_params": effective,
    }).to_response()


# Global collaboration orchestrator
# =========================================================================




@agents_bp.route("/workflows/step-dependency-bottleneck", methods=["GET"])
@unified_auth_required
def workflow_step_dependency_bottleneck():
    """Identify bottleneck steps in workflow DAG critical paths.

    For each workflow with step dependency information, computes:
    - The critical path (longest total duration path through the DAG)
    - Average duration per step across completed runs
    - Bottleneck score: step's share of total critical path time

    Returns per-workflow critical path with step durations and bottleneck scores.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 30
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    # Find workflows owned by user that have step definitions with depends_on
    workflows = (
        Workflow.query
        .filter(Workflow.owner_id == user.id)
        .all()
    )

    results = []
    for wf in workflows:
        steps = wf.steps or []
        if not steps:
            continue

        # Build step_key -> depends_on mapping from definitions
        step_defs = {}  # step_key -> {depends_on: [...], name: ...}
        for s in steps:
            dep = s.depends_on or []
            if not isinstance(dep, list):
                dep = []
            step_defs[s.step_key] = {"depends_on": dep, "name": s.name or s.step_key}

        # Only analyze workflows with at least one dependency edge
        has_dep = any(v["depends_on"] for v in step_defs.values())
        if not has_dep:
            continue

        # Get average duration per step_key from completed step runs
        step_dur_rows = (
            db.session.query(
                WorkflowStepRun.step_key,
                func.avg(
                    func.extract("epoch", WorkflowStepRun.finished_at - WorkflowStepRun.started_at)
                ),
            )
            .join(WorkflowRun, WorkflowStepRun.run_id == WorkflowRun.id)
            .filter(
                WorkflowRun.owner_id == user.id,
                WorkflowRun.workflow_id == wf.id,
                WorkflowStepRun.started_at.isnot(None),
                WorkflowStepRun.finished_at.isnot(None),
                WorkflowStepRun.started_at >= since,
            )
            .group_by(WorkflowStepRun.step_key)
            .all()
        )
        avg_durations = {row[0]: float(row[1]) if row[1] else 0.0 for row in step_dur_rows}

        # Only include steps that have actual execution data
        active_steps = {k: v for k, v in step_defs.items() if k in avg_durations}
        if not active_steps:
            continue

        # Topological sort using Kahn's algorithm
        in_degree = {k: 0 for k in active_steps}
        adj = {k: [] for k in active_steps}  # dep -> [dependents]
        for sk, info in active_steps.items():
            for dep in info["depends_on"]:
                if dep in active_steps:
                    in_degree[sk] += 1
                    adj[dep].append(sk)

        queue = [k for k, d in in_degree.items() if d == 0]
        topo_order = []
        while queue:
            node = queue.pop(0)
            topo_order.append(node)
            for nb in adj[node]:
                in_degree[nb] -= 1
                if in_degree[nb] == 0:
                    queue.append(nb)

        # If cycle detected, skip this workflow
        if len(topo_order) != len(active_steps):
            continue

        # Compute longest path (critical path) using DP
        # dist[sk] = longest total duration to reach sk
        dist = {k: 0.0 for k in active_steps}
        parent = {k: None for k in active_steps}
        for sk in topo_order:
            for dep in active_steps[sk]["depends_on"]:
                if dep in active_steps:
                    candidate = dist[dep] + avg_durations.get(sk, 0.0)
                    if candidate > dist[sk]:
                        dist[sk] = candidate
                        parent[sk] = dep
            # If no dependencies, dist = own duration
            if not active_steps[sk]["depends_on"] or all(d not in active_steps for d in active_steps[sk]["depends_on"]):
                dist[sk] = max(dist[sk], avg_durations.get(sk, 0.0))

        # Find the endpoint with the longest distance
        end_node = max(topo_order, key=lambda k: dist[k]) if topo_order else None
        if end_node is None:
            continue

        # Trace back the critical path
        critical_path = []
        cur = end_node
        visited = set()
        while cur is not None and cur not in visited:
            visited.add(cur)
            critical_path.append(cur)
            cur = parent[cur]
        critical_path.reverse()

        total_cp_duration = sum(avg_durations.get(sk, 0.0) for sk in critical_path)
        if total_cp_duration <= 0:
            continue

        path_steps = []
        for sk in critical_path:
            dur = avg_durations.get(sk, 0.0)
            path_steps.append({
                "step_key": sk,
                "name": active_steps[sk]["name"],
                "depends_on": active_steps[sk]["depends_on"],
                "avg_duration": round(dur, 1),
                "bottleneck_score": round(dur / total_cp_duration * 100, 1),
            })

        # Also include all steps with duration for reference
        all_steps_info = []
        for sk, info in sorted(active_steps.items(), key=lambda kv: avg_durations.get(kv[0], 0.0), reverse=True):
            all_steps_info.append({
                "step_key": sk,
                "name": info["name"],
                "depends_on": info["depends_on"],
                "avg_duration": round(avg_durations.get(sk, 0.0), 1),
                "is_on_critical_path": sk in critical_path,
            })

        results.append({
            "workflow_id": wf.id,
            "workflow_name": wf.name or f"Workflow#{wf.id}",
            "critical_path": path_steps,
            "critical_path_duration": round(total_cp_duration, 1),
            "all_steps": all_steps_info,
            "total_steps": len(step_defs),
            "active_steps": len(active_steps),
        })

    # Sort by critical path duration descending, limit
    results.sort(key=lambda r: r["critical_path_duration"], reverse=True)
    return ApiResponse.success({"workflows": results[:limit]}).to_response()














