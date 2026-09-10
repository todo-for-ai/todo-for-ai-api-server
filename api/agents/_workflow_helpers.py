"""
Workflow run internal helpers — step evaluation, agent picking, escalation, etc.

Extracted from workflow_runs.py to keep route handlers and DAG logic separate.
"""

from datetime import datetime, timedelta

from .task_escalation import (  # noqa: F401  兼容再导出（实现已下沉）
    PRIORITY_LADDER as _PRIORITY_LADDER,
    escalate_overdue_tasks as _escalate_overdue_tasks,
)
from .workflow_conditions import (
    _RUNTIME_OVERRIDABLE_KEYS,
    _apply_runtime_overrides,
    _evaluate_step_condition,
)
from ._shared import (
    db,
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentStatus,
    Notification,
    Project,
    SharedContext,
    StepStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    AgentReputation,
    AgentExperience,
    CrossProjectAgent,
    AgentSandbox,
    SandboxExecution,
    SandboxViolation,
    SandboxExecutionStatus,
    LEASED_EXECUTION_STATES,
    ACTIVE_ASSIGNMENT_STATES,
    _queue_sse,
    _expand_capabilities,
    record_task_event,
)


# ── Runtime overrides ──────────────────────────────────────────────────

# Keys that may be dynamically overridden on a step run without touching the
# workflow definition. Validated against this allowlist when an override is set.# ── Sub-workflow propagation ───────────────────────────────────────────

def _propagate_sub_workflow_completion(sub_wf_run):
    """When a sub-workflow completes, find and advance the parent step that launched it.

    Looks for step runs whose result_summary contains "sub_workflow_run:{id}".
    When found, marks the parent step as succeeded/failed and advances the parent workflow.
    """
    if not sub_wf_run or sub_wf_run.status not in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED):
        return

    # Search for step runs that reference this sub-workflow
    parent_step_runs = WorkflowStepRun.query.filter(
        WorkflowStepRun.result_summary.like(f"%sub_workflow_run:{sub_wf_run.id}%"),
        WorkflowStepRun.status == StepStatus.RUNNING,
    ).all()

    now = datetime.utcnow()
    for psr in parent_step_runs:
        if sub_wf_run.status == WorkflowStatus.SUCCEEDED:
            psr.status = StepStatus.SUCCEEDED
            psr.result_summary = f"Sub-workflow #{sub_wf_run.id} completed successfully"
        else:
            psr.status = StepStatus.FAILED
            psr.error = f"Sub-workflow #{sub_wf_run.id} failed"
            psr.result_summary = f"Sub-workflow #{sub_wf_run.id} failed"
        psr.finished_at = now

        # Update reputation for the agent that managed the sub-workflow
        if psr.agent_id:
            AgentReputation.record_outcome(
                agent_id=psr.agent_id,
                success=(sub_wf_run.status == WorkflowStatus.SUCCEEDED),
                context={
                    "parent_workflow_run_id": psr.run_id,
                    "sub_workflow_run_id": sub_wf_run.id,
                    "step_key": psr.step_key,
                },
            )

        # Advance the parent workflow
        parent_run = WorkflowRun.query.get(psr.run_id)
        if parent_run:
            _advance_workflow(parent_run)


# ── Agent picking ──────────────────────────────────────────────────────

def _pick_agent_for_step(wf_run, step_def):
    """Pick the best Agent for a workflow step based on capabilities, role, workload, and reputation.

    Searches the workflow owner's own agents first, then extends to cross-project
    authorized agents if no suitable match is found.
    """
    agent = None
    if step_def.agent_id:
        agent = Agent.query.get(step_def.agent_id)
    if not agent and step_def.required_capabilities:
        required = set(step_def.required_capabilities)
        is_coordination_step = "coordination" in required or "management" in required

        # Phase 1: Search own agents
        candidates = Agent.query.filter(
            Agent.status == AgentStatus.ACTIVE,
            Agent.owner_id == wf_run.owner_id,
        ).all()

        scored = []
        for c in candidates:
            expanded = _expand_capabilities(set(c.capabilities or []))
            match_count = len(required.intersection(expanded))
            if match_count == 0:
                continue
            role = c.collaboration_role or "standalone"
            role_bonus = 0
            if is_coordination_step and role == "leader":
                role_bonus = 100
            elif not is_coordination_step and role == "follower":
                role_bonus = 50
            active_count = TaskAssignment.query.filter(
                TaskAssignment.agent_id == c.id,
                TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
            ).count()
            workload_penalty = active_count * 15
            # Reputation bonus
            rep = AgentReputation.query.filter_by(agent_id=c.id).first()
            rep_bonus = int((rep.score - 50) * 0.3) if rep and rep.score > 50 else 0
            scored.append((c, match_count * 10 + role_bonus - workload_penalty + rep_bonus, active_count))

        # Phase 2: If no good match, search cross-project agents
        if not scored or scored[0][1] < 20:
            # The run carries the project scope (Workflow has no project_id column)
            if wf_run.project_id:
                cross_auths = CrossProjectAgent.get_active_for_project(wf_run.project_id)
                for auth in cross_auths:
                    c = Agent.query.get(auth.agent_id)
                    if not c or c.status != AgentStatus.ACTIVE:
                        continue
                    # Use effective capabilities (may be overridden for this project)
                    caps = CrossProjectAgent.get_effective_capabilities(c.id, wf_run.project_id)
                    expanded = _expand_capabilities(set(caps or []))
                    match_count = len(required.intersection(expanded))
                    if match_count == 0:
                        continue
                    # Check concurrent task limit
                    active_count = TaskAssignment.query.filter(
                        TaskAssignment.agent_id == c.id,
                        TaskAssignment.state.in_(ACTIVE_ASSIGNMENT_STATES),
                    ).count()
                    if active_count >= (auth.max_concurrent_tasks or 3):
                        continue
                    # Cross-project agents get a small penalty (prefer own agents)
                    cross_penalty = -5
                    role = c.collaboration_role or "standalone"
                    role_bonus = 0
                    if is_coordination_step and role == "leader":
                        role_bonus = 100
                    elif not is_coordination_step and role == "follower":
                        role_bonus = 50
                    rep = AgentReputation.query.filter_by(agent_id=c.id).first()
                    rep_bonus = int((rep.score - 50) * 0.3) if rep and rep.score > 50 else 0
                    scored.append((c, match_count * 10 + role_bonus - active_count * 15 + rep_bonus + cross_penalty, active_count))

        if scored:
            scored.sort(key=lambda x: -x[1])
            agent = scored[0][0]

    if not agent:
        leader = Agent.query.filter_by(
            status=AgentStatus.ACTIVE, owner_id=wf_run.owner_id,
            collaboration_role="leader",
        ).first()
        if leader:
            agent = leader
        else:
            agent = Agent.query.filter_by(
                status=AgentStatus.ACTIVE, owner_id=wf_run.owner_id,
            ).first()
    return agent


# ── Step start ─────────────────────────────────────────────────────────

def _start_step(wf_run, step_run, step_def, now):
    """Start a single step: find a matching Agent, create a task, and claim it.

    If the step has a sub_workflow_id, launch that workflow instead of creating
    a single task. The sub-workflow's completion will be tracked as this step's
    outcome.
    """
    step_run.status = StepStatus.RUNNING
    step_run.started_at = now

    # --- Sub-workflow handling ---
    if step_def.sub_workflow_id:
        sub_wf = Workflow.query.filter_by(id=step_def.sub_workflow_id, owner_id=wf_run.owner_id).first()
        if not sub_wf:
            step_run.status = StepStatus.FAILED
            step_run.error = f"Sub-workflow {step_def.sub_workflow_id} not found"
            step_run.finished_at = now
            return

        # Find an agent to own the sub-workflow (prefer coordinator/leader)
        agent = _pick_agent_for_step(wf_run, step_def)

        # Launch the sub-workflow
        sub_run = WorkflowRun.create(
            workflow_id=sub_wf.id,
            root_task_id=wf_run.root_task_id,
            project_id=wf_run.project_id,
            owner_id=wf_run.owner_id,
            status=WorkflowStatus.PENDING,
        )
        # Create step runs for sub-workflow
        for sub_step in sub_wf.steps:
            WorkflowStepRun.create(
                run_id=sub_run.id,
                step_key=sub_step.step_key,
                status=StepStatus.PENDING,
            )
        db.session.commit()

        # Store the sub-run reference on the step_run
        step_run.result_summary = f"sub_workflow_run:{sub_run.id}"
        if agent:
            step_run.agent_id = agent.id

        # Advance the sub-workflow
        _advance_workflow(sub_run)
        db.session.commit()

        record_task_event(
            task_id=wf_run.root_task_id,
            event_type="sub_workflow_launched",
            actor_type="system",
            payload={
                "parent_run_id": wf_run.id,
                "parent_step_key": step_def.step_key,
                "sub_workflow_id": sub_wf.id,
                "sub_run_id": sub_run.id,
            },
        )
        return

    # --- Normal step handling ---
    # Find a matching Agent
    agent = _pick_agent_for_step(wf_run, step_def)

    if not agent:
        step_run.status = StepStatus.FAILED
        step_run.error = "No available Agent"
        step_run.finished_at = now
        return

    step_run.agent_id = agent.id

    # Create a task for this step
    task_title = f"[Workflow:{wf_run.id}] {step_def.name}"
    task_content = step_def.description or ""

    # Inject predecessor step outputs as context
    deps = step_def.depends_on or []
    if deps:
        predecessor_context_parts = []
        for dep_key in deps:
            dep_sr = WorkflowStepRun.query.filter_by(run_id=wf_run.id, step_key=dep_key).first()
            if dep_sr and dep_sr.task_id:
                ctx_entries = SharedContext.query.filter_by(task_id=dep_sr.task_id).order_by(SharedContext.key.asc()).all()
                if ctx_entries:
                    parts = [f"--- 前置步骤 [{dep_key}] 上下文 ---"]
                    for entry in ctx_entries:
                        parts.append(f"**{entry.key}** (by {entry.author_agent.name if entry.author_agent else 'user'}):\n{entry.value}")
                    predecessor_context_parts.append("\n".join(parts))
        if predecessor_context_parts:
            task_content = (task_content + "\n\n" if task_content else "") + "\n\n".join(predecessor_context_parts)
    task_kwargs = dict(
        project_id=wf_run.project_id,
        title=task_title,
        content=task_content,
        status=TaskStatus.TODO,
        is_ai_task=True,
        creator_id=wf_run.owner_id,
        parent_task_id=wf_run.root_task_id,
    )
    # If there's a task template, use its defaults
    if step_def.task_template_id:
        from models.task import TaskPriority
        from models.task_collab import TaskTemplate
        tmpl = TaskTemplate.query.get(step_def.task_template_id)
        if tmpl:
            task_kwargs["title"] = task_title + f" (from: {tmpl.name})"
            if tmpl.content_template:
                task_kwargs["content"] = tmpl.content_template
            if tmpl.priority:
                try:
                    task_kwargs["priority"] = TaskPriority(tmpl.priority)
                except ValueError:
                    pass
            if tmpl.tags:
                task_kwargs["tags"] = tmpl.tags
            if tmpl.is_ai_task is not None:
                task_kwargs["is_ai_task"] = tmpl.is_ai_task

    task = Task.create(**task_kwargs)
    db.session.flush()  # 立即取 task.id——否则下方 assignment/run 会拿到 None
    step_run.task_id = task.id

    # Claim the task for the agent
    assignment = TaskAssignment.create(
        task_id=task.id,
        agent_id=agent.id,
        assigned_by_user_id=wf_run.owner_id,
        state=TaskAssignmentState.ASSIGNED,
    )
    db.session.flush()  # 取 assignment.id / run.id 同理
    run = AgentRun.create(
        task_id=task.id,
        agent_id=agent.id,
        assignment_id=assignment.id,
        status=AgentRunStatus.RUNNING,
        started_at=now,
    )
    step_run.assignment_id = assignment.id

    # --- Sandbox integration: auto-start a sandboxed execution if the agent
    # has an active sandbox policy bound to it. The policy snapshot is frozen
    # so audits remain valid even if the policy changes later. ---
    _maybe_start_sandboxed_execution(agent, run, step_run)

    # Record event
    record_task_event(
        task_id=task.id,
        event_type="workflow_step_started",
        actor_type="system",
        payload={
            "workflow_run_id": wf_run.id,
            "step_key": step_def.step_key,
            "agent_id": agent.id,
            "agent_name": agent.name,
        },
    )
    # SSE so the real-time console reflects step start/assignment immediately
    _queue_sse(wf_run.owner_id, "workflow_step_started", {
        "run_id": wf_run.id,
        "step_key": step_def.step_key,
        "agent_id": agent.id,
        "agent_name": agent.name,
    })


# ── DAG advancement ───────────────────────────────────────────────────

def _advance_workflow(wf_run):
    """Examine step runs and start any whose dependencies are all satisfied.

    If all steps are terminal, mark the workflow as finished.
    """
    now = datetime.utcnow()
    step_runs = {sr.step_key: sr for sr in wf_run.step_runs}
    steps = {
        s.step_key: s
        for s in WorkflowStep.query.filter_by(workflow_id=wf_run.workflow_id).all()
    }

    # Check if the overall workflow is already terminal
    if wf_run.status in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
        return

    any_running = False
    all_terminal = True

    # If workflow is paused, don't start new steps (already-running steps continue)
    is_paused = wf_run.status == WorkflowStatus.PAUSED

    # Check max parallelism from workflow definition
    max_parallel = 0
    if wf_run.workflow:
        max_parallel = wf_run.workflow.max_parallel_steps or 0
    # Also support per-run override from definition
    if not max_parallel and wf_run.workflow and wf_run.workflow.definition:
        max_parallel = wf_run.workflow.definition.get("max_parallel_steps", 0)

    # Count currently running steps
    running_count = sum(1 for sr in step_runs.values() if sr.status == StepStatus.RUNNING)

    for step_key, sr in step_runs.items():
        if sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
            continue  # already terminal
        all_terminal = False

        if sr.status == StepStatus.RUNNING:
            any_running = True
            continue

        # PENDING or WAITING — check dependencies
        step_def = steps.get(step_key)
        if not step_def:
            continue
        # Apply runtime overrides (dynamic reconfiguration) on top of the
        # step definition. Only affects this run; the definition is unchanged.
        step_def = _apply_runtime_overrides(step_def, sr)

        # Don't start new steps if paused
        if is_paused:
            continue

        # Check max parallelism: skip starting if we've hit the limit
        if max_parallel > 0 and running_count >= max_parallel:
            continue

        deps = step_def.depends_on or []
        deps_met = all(
            step_runs.get(dep_key) and step_runs[dep_key].status == StepStatus.SUCCEEDED
            for dep_key in deps
        )

        # Check if any dependency failed
        any_dep_failed = any(
            step_runs.get(dep_key) and step_runs[dep_key].status in (StepStatus.FAILED, StepStatus.CANCELLED)
            for dep_key in deps
        )

        if any_dep_failed:
            # Handle based on on_failure policy
            if step_def.on_failure == "skip":
                sr.status = StepStatus.SKIPPED
                sr.finished_at = now
            elif step_def.on_failure == "continue":
                # Treat as if deps are met — start the step anyway
                _start_step(wf_run, sr, step_def, now)
                any_running = True
                running_count += 1
            else:
                # abort — mark step as waiting (will never start) and fail the workflow
                sr.status = StepStatus.WAITING
            continue

        if deps_met:
            # Check conditional execution
            if step_def.condition:
                if not _evaluate_step_condition(step_def.condition, step_runs):
                    # Condition not met — skip this step
                    sr.status = StepStatus.SKIPPED
                    sr.finished_at = now
                    continue

            _start_step(wf_run, sr, step_def, now)
            any_running = True
            running_count += 1

    # If nothing is running and all are terminal, the workflow is done
    if all_terminal:
        # Determine overall status
        has_failure = any(
            sr.status in (StepStatus.FAILED, StepStatus.CANCELLED)
            for sr in step_runs.values()
        )
        wf_run.status = WorkflowStatus.FAILED if has_failure else WorkflowStatus.SUCCEEDED
        wf_run.finished_at = now

        # Check if this is a sub-workflow — advance the parent step
        _propagate_sub_workflow_completion(wf_run)
    elif any_running and wf_run.status == WorkflowStatus.PENDING:
        wf_run.status = WorkflowStatus.RUNNING
        wf_run.started_at = now


# ── Sandbox helpers ────────────────────────────────────────────────────

def _maybe_start_sandboxed_execution(agent, run, step_run):
    """If the agent has an active sandbox, create a RUNNING SandboxExecution
    bound to this AgentRun + WorkflowStepRun, freezing the policy snapshot.

    Returns the created SandboxExecution or None.
    """
    sandbox = AgentSandbox.get_for_agent(agent.id)
    if not sandbox:
        return None
    execution = SandboxExecution(
        sandbox_id=sandbox.id,
        agent_id=agent.id,
        run_id=run.id,
        step_run_id=step_run.id if step_run else None,
        status=SandboxExecutionStatus.RUNNING,
        policy_snapshot=sandbox.to_policy_dict(),
        started_at=datetime.utcnow(),
        tool_calls=0,
        network_calls=0,
    )
    db.session.add(execution)
    db.session.flush()
    return execution


def _maybe_finish_sandboxed_execution(run, status, summary=None, error=None):
    """Complete any RUNNING SandboxExecution bound to an AgentRun.

    Called when a workflow step / agent run completes (success or failure).
    """
    if not run:
        return None
    execution = SandboxExecution.query.filter_by(
        run_id=run.id, status=SandboxExecutionStatus.RUNNING
    ).first()
    if not execution:
        return None
    execution.finish(status, summary=summary, error=error)
    return execution
