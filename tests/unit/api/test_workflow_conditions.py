"""Workflow step 条件求值与运行时覆盖（api/agents/workflow_conditions.py）
回归测试：纯逻辑，全操作符/组合条件/覆盖合并到行。
"""

from types import SimpleNamespace

from api.agents.workflow_conditions import (
    _RUNTIME_OVERRIDABLE_KEYS,
    _apply_runtime_overrides,
    _evaluate_step_condition,
)
from models import StepStatus


def _sr(status, result_summary=None):
    return SimpleNamespace(status=status, result_summary=result_summary)


def _step_runs(**by_key):
    return {k: _sr(v) for k, v in by_key.items()}


# ────────────────────────── 条件求值 ──────────────────────────

class TestEvaluateStepCondition:
    def test_no_condition_returns_true(self):
        assert _evaluate_step_condition(None, {}) is True
        assert _evaluate_step_condition({}, {}) is True

    def test_composite_all(self):
        runs = _step_runs(review=StepStatus.SUCCEEDED, test=StepStatus.FAILED)
        cond = {"all": [
            {"step_key": "review", "operator": "succeeded"},
            {"step_key": "test", "operator": "failed"},
        ]}
        assert _evaluate_step_condition(cond, runs) is True
        cond_fail = {"all": [
            {"step_key": "review", "operator": "succeeded"},
            {"step_key": "test", "operator": "succeeded"},
        ]}
        assert _evaluate_step_condition(cond_fail, runs) is False

    def test_composite_any(self):
        runs = _step_runs(review=StepStatus.SUCCEEDED)
        cond = {"any": [
            {"step_key": "review", "operator": "failed"},
            {"step_key": "review", "operator": "succeeded"},
        ]}
        assert _evaluate_step_condition(cond, runs) is True
        cond_none = {"any": [{"step_key": "review", "operator": "failed"}]}
        assert _evaluate_step_condition(cond_none, runs) is False

    def test_simple_without_step_key_returns_true(self):
        assert _evaluate_step_condition({"operator": "succeeded"}, {}) is True

    def test_dependency_not_started_returns_false(self):
        assert _evaluate_step_condition(
            {"step_key": "review", "operator": "succeeded"}, {}) is False

    def test_operators(self):
        runs = {
            "ok": _sr(StepStatus.SUCCEEDED, "approved by lead"),
            "bad": _sr(StepStatus.FAILED, "rejected"),
            "skip": _sr(StepStatus.SKIPPED, ""),
            "run": _sr(StepStatus.RUNNING, ""),
        }
        ev = _evaluate_step_condition
        assert ev({"step_key": "ok", "operator": "succeeded"}, runs) is True
        assert ev({"step_key": "bad", "operator": "succeeded"}, runs) is False
        assert ev({"step_key": "bad", "operator": "failed"}, runs) is True
        assert ev({"step_key": "skip", "operator": "skipped"}, runs) is True
        assert ev({"step_key": "ok", "operator": "skipped"}, runs) is False
        # completed = succeeded | failed | skipped
        for key in ("ok", "bad", "skip"):
            assert ev({"step_key": key, "operator": "completed"}, runs) is True
        assert ev({"step_key": "run", "operator": "completed"}, runs) is False
        # output 系列
        assert ev({"step_key": "ok", "operator": "output_equals",
                   "value": "approved by lead"}, runs) is True
        assert ev({"step_key": "ok", "operator": "output_equals",
                   "value": "nope"}, runs) is False
        assert ev({"step_key": "ok", "operator": "output_contains",
                   "value": "approved"}, runs) is True
        assert ev({"step_key": "bad", "operator": "output_contains",
                   "value": "approved"}, runs) is False
        assert ev({"step_key": "ok", "operator": "output_not_contains",
                   "value": "rejected"}, runs) is True
        assert ev({"step_key": "ok", "operator": "output_not_contains",
                   "value": "approved"}, runs) is False

    def test_status_equals_enum_and_string(self):
        runs = {
            "run": _sr(StepStatus.RUNNING),
            "plain": SimpleNamespace(status="running"),
        }
        assert _evaluate_step_condition(
            {"step_key": "run", "operator": "status_equals", "value": "running"}, runs) is True
        assert _evaluate_step_condition(
            {"step_key": "plain", "operator": "status_equals", "value": "running"}, runs) is True
        assert _evaluate_step_condition(
            {"step_key": "run", "operator": "status_equals", "value": "done"}, runs) is False

    def test_unknown_operator_defaults_true(self):
        runs = _step_runs(ok=StepStatus.SUCCEEDED)
        assert _evaluate_step_condition(
            {"step_key": "ok", "operator": "magic"}, runs) is True


# ────────────────────────── 运行时覆盖 ──────────────────────────

def _step_def(**kw):
    defaults = dict(
        step_key="s1", name="step", description=None, order=1,
        required_capabilities=["testing"], agent_id=None,
        task_template_id=None, depends_on=[], condition=None,
        sub_workflow_id=None, timeout_seconds=None, retry_count=0,
        on_failure="abort",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _run(overrides=None):
    return SimpleNamespace(runtime_overrides=overrides)


class TestApplyRuntimeOverrides:
    def test_no_overrides_returns_original_def(self):
        step_def = _step_def()
        assert _apply_runtime_overrides(step_def, None) is step_def
        assert _apply_runtime_overrides(step_def, _run({})) is step_def
        assert _apply_runtime_overrides(step_def, _run(None)) is step_def

    def test_allowed_keys_are_merged(self):
        merged = _apply_runtime_overrides(_step_def(), _run({
            "agent_id": 42, "retry_count": 3, "on_failure": "continue",
        }))
        assert merged.agent_id == 42
        assert merged.retry_count == 3
        assert merged.on_failure == "continue"
        assert merged.step_key == "s1"  # 未覆盖字段保持

    def test_allowlist_blocks_unknown_keys(self):
        merged = _apply_runtime_overrides(_step_def(), _run({"evil": 1}))
        assert not hasattr(merged, "evil")

    def test_none_override_values_skipped(self):
        merged = _apply_runtime_overrides(_step_def(), _run({"agent_id": None}))
        assert merged.agent_id is None  # 原值本就是 None；未改变
        assert _RUNTIME_OVERRIDABLE_KEYS & {"agent_id"} == {"agent_id"}

    def test_original_step_def_not_mutated(self):
        step_def = _step_def(retry_count=0)
        _apply_runtime_overrides(step_def, _run({"retry_count": 9}))
        assert step_def.retry_count == 0

    def test_all_overridable_keys_are_known(self):
        assert _RUNTIME_OVERRIDABLE_KEYS == {
            "agent_id", "required_capabilities", "timeout_seconds",
            "retry_count", "on_failure", "condition", "task_template_id",
            "sub_workflow_id",
        }
