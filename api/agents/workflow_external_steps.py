"""
External workflow platform connectors — run a workflow step on Dify / Coze.

A step with ``integration_config`` set is NOT turned into an agent task.
Instead the platform calls the remote provider's workflow-run API, feeds it
rendered inputs, and closes the step with the remote outcome via the shared
``complete_step_run`` core (so DAG advancement / retry / reputation all
behave exactly like agent-executed steps).

Config schema (stored encrypted-at-rest for api_key, masked in responses):
    {"provider": "dify" | "coze",
     "base_url": "https://api.dify.ai" | "https://api.coze.cn",
     "api_key":  "app-... / pat-...",        # plaintext in, ciphertext at rest
     "workflow_id": "<coze only>",
     "inputs": {"param": "{{step_result_research}}", ...},   # optional
     "timeout_seconds": 100}                                  # optional

Input placeholders: ``{{step_result_<key>}}`` (upstream SharedContext),
``{{context.<name>}}`` (run context), ``{{root_task_title}}``,
``{{root_task_id}}``, ``{{run_id}}``, ``{{step_key}}``, ``{{step_description}}``,
以及 Dify 风格系统变量前缀 ``{{sys.run_id}}`` / ``{{sys.workflow_id}}`` /
``{{sys.workflow_name}}`` / ``{{sys.project_id}}`` / ``{{sys.root_task_id}}`` /
``{{sys.root_task_title}}`` / ``{{sys.step_key}}`` / ``{{sys.step_name}}`` /
``{{sys.step_description}}``（未识别的 sys.* 解析为空串，不透传原文）。
"""

import json
from threading import Thread

import requests as http_client
import structlog
from flask import current_app

from services.github_app import encrypt_str, decrypt_str

logger = structlog.get_logger()

PROVIDER_DIFY = "dify"
PROVIDER_COZE = "coze"
SUPPORTED_PROVIDERS = (PROVIDER_DIFY, PROVIDER_COZE)
DEFAULT_TIMEOUT_SECONDS = 100
MASK_PREFIX = "••••"
_DIFY_DEFAULT_BASE = "https://api.dify.ai/v1"
_COZE_DEFAULT_BASE = "https://api.coze.cn"


def is_external_step(step_def):
    """True when the step definition carries a usable connector config."""
    cfg = getattr(step_def, "integration_config", None)
    return isinstance(cfg, dict) and cfg.get("provider") in SUPPORTED_PROVIDERS


def validate_integration_config(raw):
    """Raise ValueError when *raw* is not a usable connector config."""
    if not isinstance(raw, dict):
        raise ValueError("integration_config must be an object")
    provider = str(raw.get("provider") or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"integration provider must be one of {', '.join(SUPPORTED_PROVIDERS)}"
        )
    if not str(raw.get("api_key") or "").strip():
        raise ValueError("integration api_key is required")
    if provider == PROVIDER_COZE and not str(raw.get("workflow_id") or "").strip():
        raise ValueError("integration workflow_id is required for coze")
    if raw.get("inputs") is not None and not isinstance(raw.get("inputs"), dict):
        raise ValueError("integration inputs must be an object")
    return raw


def normalize_incoming_integration_config(raw, previous=None):
    """Prepare an API payload for storage: validate + encrypt api_key.

    ``previous`` is the stored config of the step being replaced (PUT path) so
    a masked round-trip value or an omitted key reuses the old ciphertext
    instead of losing it. Returns None for empty input; raises ValueError on
    invalid provider/shape.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    cfg = {k: v for k, v in raw.items() if k != "api_key_set"}
    # Masked values carry no provider shape guarantee until merged with previous
    merged_for_validation = dict(cfg)
    key = str(cfg.get("api_key") or "").strip()
    prev = dict(previous or {})
    if key and key.startswith(MASK_PREFIX):
        if prev.get("api_key"):
            cfg["api_key"] = prev["api_key"]
        else:
            cfg.pop("api_key", None)
        merged_for_validation["api_key"] = "dummy-key-for-validation"
    elif key:
        merged_for_validation["api_key"] = key
        cfg["api_key"] = encrypt_str(key)
    else:
        cfg.pop("api_key", None)
        if prev.get("api_key"):
            cfg["api_key"] = prev["api_key"]
            merged_for_validation["api_key"] = "dummy-key-for-validation"
    validate_integration_config(merged_for_validation)
    return cfg or None


def _resolve_sys_placeholder(name, wf_run, step_def):
    """Dify 风格 ``sys.*`` 系统变量（借鉴其 variable_prefixes 思路）。

    返回 (resolved, handled)；未识别的 sys.* 名称 handled=True、值为空串
    （避免把 ``{{sys.xxx}}`` 原样发到远端平台）。
    """
    if name == "sys.run_id":
        return str(wf_run.id), True
    if name == "sys.workflow_id":
        return str(wf_run.workflow_id), True
    if name == "sys.workflow_name":
        return (wf_run.workflow.name if wf_run.workflow else ""), True
    if name == "sys.project_id":
        return str(wf_run.project_id or ""), True
    if name == "sys.root_task_id":
        return str(wf_run.root_task_id or ""), True
    if name == "sys.root_task_title":
        return (wf_run.root_task.title if wf_run.root_task else ""), True
    if name == "sys.step_key":
        return step_def.step_key, True
    if name == "sys.step_name":
        return getattr(step_def, "name", "") or "", True
    if name == "sys.step_description":
        return getattr(step_def, "description", "") or "", True
    return "", True


def _resolve_placeholder(name, wf_run, step_def):
    """Resolve one ``{{...}}`` placeholder name to its value ('' if unknown)."""
    if name.startswith("sys."):
        value, _ = _resolve_sys_placeholder(name, wf_run, step_def)
        return value
    if name.startswith("step_result_"):
        from ._shared import SharedContext, WorkflowStepRun
        src_key = name[len("step_result_"):]
        dep_sr = WorkflowStepRun.query.filter_by(
            run_id=wf_run.id, step_key=src_key).first()
        if dep_sr and dep_sr.task_id:
            entry = SharedContext.query.filter_by(
                task_id=dep_sr.task_id, key=f"step_result_{src_key}").first()
            if entry:
                return entry.value or ""
        return ""
    if name.startswith("context."):
        ctx = wf_run.context or {}
        return str(ctx.get(name[len("context."):], ""))
    if name in ("root_task_title",):
        return (wf_run.root_task.title if wf_run.root_task else "")
    if name == "root_task_id":
        return str(wf_run.root_task_id or "")
    if name == "run_id":
        return str(wf_run.id)
    if name == "step_key":
        return step_def.step_key
    if name == "step_description":
        return getattr(step_def, "description", "") or ""
    return ""


def _render_value(value, wf_run, step_def):
    if not isinstance(value, str):
        return value
    out, i = [], 0
    while True:
        start = value.find("{{", i)
        if start < 0:
            out.append(value[i:])
            break
        end = value.find("}}", start + 2)
        if end < 0:
            out.append(value[i:])
            break
        out.append(value[i:start])
        name = value[start + 2:end].strip()
        if name and " " not in name and "{{" not in name:
            out.append(_resolve_placeholder(name, wf_run, step_def))
            i = end + 2
        else:  # not a placeholder shape — keep literally
            out.append(value[start:end + 2])
            i = end + 2
    return "".join(out)


def render_inputs(config, wf_run, step_def):
    """Deep-render ``{{...}}`` placeholders across the configured inputs."""
    rendered = {}
    for k, v in (config.get("inputs") or {}).items():
        if isinstance(v, dict):
            rendered[k] = {kk: _render_value(vv, wf_run, step_def) for kk, vv in v.items()}
        elif isinstance(v, list):
            rendered[k] = [_render_value(vv, wf_run, step_def) for vv in v]
        else:
            rendered[k] = _render_value(v, wf_run, step_def)
    return rendered


def _post_json(url, headers, payload, timeout):
    resp = http_client.post(url, headers=headers, json=payload, timeout=timeout)
    body = resp.text or ""
    if resp.status_code >= 400:
        return False, "", f"HTTP {resp.status_code}: {body[:500]}"
    try:
        data = resp.json()
    except ValueError:
        return False, "", f"Non-JSON response: {body[:500]}"
    return True, data, ""


def resolve_api_key(config):
    """Return the plaintext api key: decrypt ciphertext, fall back to raw.

    Routes always store encrypt_str() ciphertext, but configs written
    programmatically (seeds / templates / direct model use) may hold a plain
    key — accept both rather than failing the step.
    """
    stored = config.get("api_key")
    if not stored:
        return None
    return decrypt_str(stored) or str(stored)


def call_external_workflow(config, inputs, timeout_seconds):
    """Invoke the remote workflow. Returns (ok, output_text, error)."""
    provider = config["provider"]
    api_key = resolve_api_key(config)
    if not api_key:
        return False, "", "integration api_key missing or undecryptable"
    base_url = str(config.get("base_url") or "").strip().rstrip("/")

    if provider == PROVIDER_DIFY:
        url = f"{base_url or _DIFY_DEFAULT_BASE}/workflows/run"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "inputs": inputs,
            "response_mode": "blocking",
            "user": "todo-for-ai-workflow",
        }
        ok, data, err = _post_json(url, headers, payload, timeout_seconds)
        if not ok:
            return False, "", err
        inner = data.get("data") or {}
        status = str(inner.get("status") or "")
        if status and status != "succeeded":
            return False, "", f"dify workflow status={status}: {str(inner.get('error'))[:300]}"
        outputs = inner.get("outputs")
        output_text = json.dumps(outputs, ensure_ascii=False) if outputs is not None else json.dumps(data, ensure_ascii=False)
        return True, output_text, ""

    # Coze: parameters values are stringified on the wire
    url = f"{base_url or _COZE_DEFAULT_BASE}/v1/workflow/run"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    params = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
              for k, v in inputs.items()}
    payload = {"workflow_id": config.get("workflow_id"), "parameters": params}
    ok, data, err = _post_json(url, headers, payload, timeout_seconds)
    if not ok:
        return False, "", err
    if data.get("code") not in (0, "0"):
        return False, "", f"coze code={data.get('code')}: {str(data.get('msg'))[:300]}"
    inner = data.get("data")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = data.get("data")
    output_text = json.dumps(inner, ensure_ascii=False) if not isinstance(inner, str) else inner
    return True, output_text, ""


def _execute_with_objects(wf_run, step_def, timeout_seconds=None):
    """Run the remote call for an already-loaded (wf_run, step_def).

    Must be called inside an app context. Uses whatever session is current —
    never pushes its own — so synchronous dispatch participates in the
    caller's transaction instead of racing it on a shared connection.
    """
    from ._shared import WorkflowStep
    from .workflow_completion import complete_step_run

    try:
        step_key = step_def.step_key
        sr = next((s for s in wf_run.step_runs if s.step_key == step_key), None)
        if not sr or sr.status is None or sr.status.value != "running":
            return
        config = step_def.integration_config or {}
        timeout = (
            timeout_seconds
            or config.get("timeout_seconds")
            or step_def.timeout_seconds
            or DEFAULT_TIMEOUT_SECONDS
        )
        inputs = render_inputs(config, wf_run, step_def)
        ok, output_text, err = call_external_workflow(config, inputs, timeout)
        if ok:
            # 外部步骤没有 task_id，输出只能落在 result_summary 供下游/控制台读
            sr.result_summary = output_text[:20000]
        complete_step_run(
            wf_run, step_key,
            success=ok,
            result_summary=output_text[:20000] if ok else "",
            error=err,
            source="external",
        )
    except Exception as exc:  # noqa: BLE001 — 远端调用失败只能反映到步骤终态
        logger.warning("external_step.execute_failed", run_id=wf_run.id,
                       step_key=step_def.step_key, error=str(exc))
        try:
            complete_step_run(wf_run, step_def.step_key, success=False,
                              error=f"external step crashed: {exc}",
                              source="external")
        except Exception:  # noqa: BLE001
            logger.warning("external_step.finalize_failed",
                           run_id=wf_run.id, step_key=step_def.step_key)


def execute_external_step(app, run_id, step_key, timeout_seconds=None):
    """Thread entry: fresh app context + session, re-fetch, then execute.

    The caller must have committed the run row before starting this thread.
    Never raises — all outcomes funnel into complete_step_run.
    """
    with app.app_context():
        from ._shared import WorkflowRun, WorkflowStep

        try:
            wf_run = WorkflowRun.query.get(run_id)
            if not wf_run:
                logger.warning("external_step.run_missing", run_id=run_id)
                return
            step_def = WorkflowStep.query.filter_by(
                workflow_id=wf_run.workflow_id, step_key=step_key).first()
            if not step_def:
                return
            _execute_with_objects(wf_run, step_def, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("external_step.setup_failed", run_id=run_id,
                           step_key=step_key, error=str(exc))


def dispatch_external_step(wf_run, step_run, step_def, now, synchronous=False):
    """Start an externally-executed step (Dify / Coze) and fire start events.

    Called from ``_start_step`` — step_run is already RUNNING with started_at
    set. The run/step state is committed first so the async variant's thread
    can re-fetch it safely; the synchronous variant (tests / deterministic
    contexts) executes inline on the CURRENT session.
    """
    from ._shared import db, record_task_event, _queue_sse

    cfg = step_def.integration_config or {}
    payload = {
        "run_id": wf_run.id,
        "step_key": step_def.step_key,
        "provider": cfg.get("provider"),
        "step_name": step_def.name,
    }
    try:
        record_task_event(
            task_id=wf_run.root_task_id,
            event_type="workflow_external_step_started",
            actor_type="system",
            payload=payload,
        )
    except Exception:
        pass  # 事件留痕失败不阻断派发
    _queue_sse(wf_run.owner_id, "workflow_step_started", payload)

    db.session.commit()  # 步骤 RUNNING 态先落库：线程安全 + commit 后对象刷新

    if synchronous:
        _execute_with_objects(wf_run, step_def)
    else:
        app = current_app._get_current_object()
        Thread(
            target=execute_external_step,
            args=(app, wf_run.id, step_def.step_key),
            daemon=True,
        ).start()
    return step_run
