"""
Single-step test run for workflow steps — borrowed from Dify's
``WorkflowEntry.single_step_run`` (run one node with sample input), adapted
to todo-for-ai's agent-centred model:

- agent step  → NO side effects: return the agent that would be picked and a
  preview of the task (title/content) that a real run would create. This is
  the canvas debugging aid ("what will this step actually do?").
- external step (dify/coze/http) → real remote call with the rendered inputs,
  result returned for inspection without creating a WorkflowRun.
"""

from ._shared import Agent
from .workflow_external_steps import (
    call_external_workflow,
    is_external_step,
    render_inputs,
)
from ._workflow_helpers import _pick_agent_for_step

DEFAULT_TEST_TIMEOUT_SECONDS = 30
MAX_TEST_TIMEOUT_SECONDS = 120


def test_run_step(wf, step_def, instructions="", context=None, timeout_seconds=None):
    """Run one step in isolation. Returns a JSON-serialisable dict.

    ``context`` seeds the run-context variables (``{{context.x}}``). The
    caller has committed no WorkflowRun; agent steps never touch the DB
    beyond reads.
    """
    from types import SimpleNamespace

    # 轻量 wf_run 替身：渲染占位符只需 id/context/relationships
    wf_run = SimpleNamespace(
        id=0,
        workflow=wf,
        workflow_id=wf.id,
        project_id=None,
        owner_id=wf.owner_id,
        root_task_id=None,
        root_task=None,
        context=dict(context or {}),
    )

    if is_external_step(step_def):
        config = dict(step_def.integration_config or {})
        inputs = render_inputs(config, wf_run, step_def)
        timeout = timeout_seconds or DEFAULT_TEST_TIMEOUT_SECONDS
        timeout = max(1, min(int(timeout), MAX_TEST_TIMEOUT_SECONDS))
        try:
            ok, output_text, error = call_external_workflow(config, inputs, timeout)
        except Exception as exc:  # noqa: BLE001 — 远端异常即测试结果
            ok, output_text, error = False, "", f"{type(exc).__name__}: {exc}"
        return {
            "mode": "external",
            "provider": config.get("provider"),
            "ok": ok,
            "inputs": inputs,
            "output": output_text if ok else "",
            "error": error,
            "note": "真实调用了远端工作流（未创建运行记录）",
        }

    # Agent 步骤：只做预览，不落库、不派发
    picked = _pick_agent_for_step(wf_run, step_def)
    content = step_def.description or ""
    if instructions:
        content = (content + "\n\n" if content else "") + instructions
    return {
        "mode": "agent_preview",
        "agent": (
            {"id": picked.id, "name": picked.name} if isinstance(picked, Agent)
            else ({"id": picked.id, "name": getattr(picked, "name", "")} if picked else None)
        ),
        "task_preview": {
            "title": f"{step_def.name}",
            "content": content,
            "required_capabilities": step_def.required_capabilities or [],
        },
        "note": "预览模式：未创建任务/运行。真实运行时标题带 [Workflow:<run_id>] 前缀，"
                "并自动注入上游步骤输出作为上下文。",
    }
