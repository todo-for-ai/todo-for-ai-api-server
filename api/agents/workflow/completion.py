"""
Workflow step completion core — shared by the human/HTTP callback route and
the task-completion hook that closes the agent execution loop.

Before this module existed, ``POST /workflow-runs/<id>/steps/<key>/complete``
was the ONLY way a step reached a terminal state. Steps run by real agents
(their tasks get pulled and committed via the runtime protocol) stayed RUNNING
forever unless a human clicked "complete" in the web console — the loop was
broken. Now ``maybe_autocomplete_for_task`` performs the same transitions when
the step's task reaches a terminal state through ANY completion path.
"""

from datetime import datetime

import structlog

from .conditions import _apply_runtime_overrides
from .._shared import (
    db,
    AgentRun,
    AgentRunStatus,
    AgentReputation,
    AgentExperience,
    SandboxExecutionStatus,
    SharedContext,
    StepStatus,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    LEASED_EXECUTION_STATES,
    _queue_sse,
    flush_sse_notifications,
    record_task_event,
)
from .._workflow_helpers import _advance_workflow, _maybe_finish_sandboxed_execution

logger = structlog.get_logger()


def complete_step_run(wf_run, step_key, success, result_summary="", error="", source="human"):
    """Mark a step run terminal and advance the DAG.

    Extracted from the ``complete_workflow_step`` route so that task-driven
    completions (agent commit / human task update / review approval) share the
    exact same semantics: SharedContext write-back, auto-retry, reputation,
    experience extraction, sandbox finalization, SSE notification.

    Returns the updated WorkflowStepRun.
    """
    sr = WorkflowStepRun.query.filter_by(run_id=wf_run.id, step_key=step_key).first()
    if not sr:
        raise ValueError(f"Step run not found: run={wf_run.id} step={step_key}")

    now = datetime.utcnow()

    if success:
        sr.status = StepStatus.SUCCEEDED
        # Auto-save step result to SharedContext for downstream steps
        if sr.task_id and result_summary:
            existing = SharedContext.query.filter_by(
                task_id=sr.task_id, key=f"step_result_{step_key}"
            ).first()
            if existing:
                existing.value = result_summary
                if sr.agent_id:
                    existing.author_agent_id = sr.agent_id
            else:
                SharedContext.create(
                    task_id=sr.task_id,
                    key=f"step_result_{step_key}",
                    value=result_summary,
                    author_agent_id=sr.agent_id,
                )
    else:
        sr.status = StepStatus.FAILED
        sr.error = error or ""

        # Auto-retry: if the step definition has retry_count and we haven't exhausted attempts
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=step_key
        ).first()
        # Apply runtime overrides so a dynamically-reconfigured retry_count takes effect
        step_def = _apply_runtime_overrides(step_def, sr) if step_def else step_def
        if step_def and (step_def.retry_count or 0) > 0:
            current_attempt = sr.attempt or 1
            if current_attempt <= step_def.retry_count:
                # Reset step for retry
                old_task_id = sr.task_id
                sr.status = StepStatus.PENDING
                sr.error = None
                sr.finished_at = None
                sr.attempt = current_attempt + 1
                # Cancel the old assignment/run
                if sr.assignment_id:
                    old_assignment = TaskAssignment.query.get(sr.assignment_id)
                    if old_assignment and old_assignment.state in LEASED_EXECUTION_STATES:
                        old_assignment.state = TaskAssignmentState.CANCELLED
                        old_assignment.completed_at = now
                if sr.assignment_id:
                    old_runs = AgentRun.query.filter_by(
                        assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                    ).all()
                    for r in old_runs:
                        r.status = AgentRunStatus.CANCELLED
                        r.ended_at = now
                sr.assignment_id = None
                sr.agent_id = None
                sr.task_id = None
                # Record retry event on the discarded task (kept for the audit trail)
                if old_task_id:
                    record_task_event(
                        task_id=old_task_id,
                        event_type="workflow_step_auto_retry",
                        actor_type="system",
                        payload={
                            "step_key": step_key,
                            "attempt": current_attempt + 1,
                            "max_retries": step_def.retry_count,
                            "source": source,
                        },
                    )

    sr.finished_at = now

    # Update Agent reputation based on step outcome
    if sr.agent_id and sr.status != StepStatus.PENDING:  # Don't update on auto-retry
        completion_time = None
        if sr.started_at and sr.finished_at:
            completion_time = (sr.finished_at - sr.started_at).total_seconds()
        AgentReputation.record_outcome(
            agent_id=sr.agent_id,
            success=(sr.status == StepStatus.SUCCEEDED),
            completion_time=completion_time,
            context={
                "task_id": sr.task_id,
                "step_key": sr.step_key,
                "workflow_run_id": sr.run_id,
                "duration_sec": round(completion_time, 1) if completion_time else None,
            },
        )

        # Auto-extract experience from step outcome
        step_def = WorkflowStep.query.filter_by(
            workflow_id=wf_run.workflow_id, step_key=step_key
        ).first()
        task = Task.query.get(sr.task_id) if sr.task_id else None
        try:
            AgentExperience.extract_from_step_outcome(
                agent_id=sr.agent_id,
                step_run=sr,
                step_def=step_def,
                task=task,
            )
        except Exception:
            pass  # Don't fail the step completion if experience extraction fails

        # Complete any sandboxed execution bound to this step's AgentRun
        try:
            if sr.assignment_id:
                bound_run = AgentRun.query.filter_by(
                    assignment_id=sr.assignment_id, status=AgentRunStatus.RUNNING
                ).first()
                if bound_run:
                    sandbox_status = (
                        SandboxExecutionStatus.COMPLETED
                        if sr.status == StepStatus.SUCCEEDED
                        else SandboxExecutionStatus.FAILED
                    )
                    _maybe_finish_sandboxed_execution(
                        bound_run,
                        sandbox_status,
                        summary=result_summary or None,
                        error=error or None,
                    )
        except Exception:
            pass  # Don't fail step completion if sandbox finalization fails

    db.session.commit()

    # Notify clients that a step reached a terminal/intermediate state so the
    # real-time console can refresh without polling.
    _queue_sse(wf_run.owner_id, "workflow_step_finished", {
        "run_id": wf_run.id,
        "step_key": step_key,
        "status": sr.status.value if sr.status else None,
        "agent_id": sr.agent_id,
        "attempt": sr.attempt,
        "source": source,
    })

    # Advance the workflow
    _advance_workflow(wf_run)
    db.session.commit()
    flush_sse_notifications()

    return sr


def maybe_autocomplete_for_task(task_id, success, result_summary="", error="", source="agent"):
    """Auto-complete the workflow step bound to *task_id*, if any.

    Called whenever a task reaches a terminal state outside the workflow API
    (agent commit via runtime protocol, human status update, review approval).
    Guards against double completion: only a RUNNING step (one that actually
    started and owns the task) is completed; terminal steps are left alone.

    Returns the completed WorkflowStepRun, or None when the task is not bound
    to an active step run.
    """
    if not task_id:
        return None
    sr = WorkflowStepRun.query.filter_by(task_id=task_id).order_by(
        WorkflowStepRun.id.desc()
    ).first()
    if not sr or sr.status != StepStatus.RUNNING:
        return None
    wf_run = WorkflowRun.query.get(sr.run_id)
    if not wf_run:
        return None
    try:
        return complete_step_run(
            wf_run, sr.step_key,
            success=success, result_summary=result_summary or "", error=error or "",
            source=source,
        )
    except Exception:
        # 闭环失败绝不反噬调用方（commit / 任务更新路径）
        db.session.rollback()
        logger.warning("workflow.autocomplete_failed", task_id=task_id, exc_info=True)
        return None
