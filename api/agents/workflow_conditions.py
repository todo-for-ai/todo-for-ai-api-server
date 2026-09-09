"""Workflow step 条件求值与运行时覆盖（纯逻辑，无 DB 依赖）。

从 _workflow_helpers.py 拆出：条件表达式求值（succeeded/failed/skipped/
completed/output_*/status_equals + all/any 组合）与步骤运行时覆盖合并。
被 _advance_workflow / workflow_runs / workflow_versions / maintenance 共用。
"""

from ._shared import StepStatus

_RUNTIME_OVERRIDABLE_KEYS = {
    "agent_id",
    "required_capabilities",
    "timeout_seconds",
    "retry_count",
    "on_failure",
    "condition",
    "task_template_id",
    "sub_workflow_id",
}


def _apply_runtime_overrides(step_def, step_run):
    """Return a view of step_def with any runtime overrides from step_run applied.

    Uses a SimpleNamespace so downstream code (which reads attributes like
    step_def.agent_id, step_def.required_capabilities, etc.) works unchanged.
    The original WorkflowStep definition is never mutated.
    """
    overrides = (step_run.runtime_overrides if step_run else None) or {}
    if not overrides:
        return step_def
    from types import SimpleNamespace
    merged = SimpleNamespace(
        step_key=step_def.step_key,
        name=step_def.name,
        description=step_def.description,
        order=step_def.order,
        required_capabilities=step_def.required_capabilities,
        agent_id=step_def.agent_id,
        task_template_id=step_def.task_template_id,
        depends_on=step_def.depends_on,
        condition=step_def.condition,
        sub_workflow_id=step_def.sub_workflow_id,
        timeout_seconds=step_def.timeout_seconds,
        retry_count=step_def.retry_count,
        on_failure=step_def.on_failure,
    )
    for k, v in overrides.items():
        if k in _RUNTIME_OVERRIDABLE_KEYS and v is not None:
            setattr(merged, k, v)
    return merged


# ── Step condition evaluation ──────────────────────────────────────────

def _evaluate_step_condition(condition, step_runs):
    """Evaluate a step's condition against the current step run states.

    Condition format:
      - Simple: {"step_key": "review", "operator": "succeeded", "value": true}
      - Negation: {"step_key": "review", "operator": "failed"}
      - Output match: {"step_key": "review", "operator": "output_contains", "value": "approved"}
      - Composite (AND): {"all": [cond1, cond2]}
      - Composite (OR): {"any": [cond1, cond2]}

    Returns True if the step should execute, False to skip.
    """
    if not condition:
        return True

    # Composite conditions
    if "all" in condition:
        return all(_evaluate_step_condition(c, step_runs) for c in condition["all"])
    if "any" in condition:
        return any(_evaluate_step_condition(c, step_runs) for c in condition["any"])

    # Simple condition
    step_key = condition.get("step_key")
    operator = condition.get("operator", "succeeded")
    value = condition.get("value")

    if not step_key:
        return True  # No step_key means no condition

    sr = step_runs.get(step_key)
    if not sr:
        return False  # Dependency step hasn't started yet

    if operator == "succeeded":
        return sr.status == StepStatus.SUCCEEDED
    elif operator == "failed":
        return sr.status == StepStatus.FAILED
    elif operator == "skipped":
        return sr.status == StepStatus.SKIPPED
    elif operator == "completed":
        return sr.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED)
    elif operator == "output_equals":
        return (sr.result_summary or "") == str(value)
    elif operator == "output_contains":
        return str(value) in (sr.result_summary or "")
    elif operator == "output_not_contains":
        return str(value) not in (sr.result_summary or "")
    elif operator == "status_equals":
        return sr.status.value == str(value) if hasattr(sr.status, 'value') else str(sr.status) == str(value)
    else:
        # Unknown operator — default to True (don't block execution)
        return True
