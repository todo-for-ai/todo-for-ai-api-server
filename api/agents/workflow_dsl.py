"""
Workflow DSL import/export — portable, shareable workflow definitions.

Modeled on Dify's app DSL (api/services/app_dsl_service.py) but shaped for
todo-for-ai's Workflow/WorkflowStep model. Borrowed concepts, original code:

- Export cleans environment-specific fields the way Dify scrubs credentials:
  api_key never leaves the platform, agent_id/task_template_id are nulled
  (cross-environment ids are meaningless), sub-workflow links are exported
  by NAME as a dependency marker.
- Import validates version compatibility, step_key uniqueness, dependency
  references and acyclicity, then resolves sub-workflow dependencies by name
  (missing ones are rejected with an explicit list — Dify's "leaked
  dependencies" behaviour).

DSL shape (YAML):
    todo_for_ai: workflow-dsl
    version: "1.0"
    workflow: {name, description, max_parallel_steps}
    steps: [{step_key, name, description, order, required_capabilities,
             depends_on, condition, on_failure, retry_count, timeout_seconds,
             integration_config: {provider, base_url, workflow_id, inputs},
             sub_workflow_name}]
    layout: {step_key: {x, y}}
"""

from datetime import datetime

import yaml

from ._shared import Workflow, WorkflowStep

DSL_MAGIC = "todo_for_ai"
DSL_MARKER = "workflow-dsl"
DSL_VERSION = "1.0"
SUPPORTED_MAJOR = 1


class DslError(ValueError):
    """Raised on invalid DSL — routes map this to HTTP 400."""


# ── Export ─────────────────────────────────────────────────────────────


def _clean_integration_config(config):
    """Keep connector shape, drop the secret (api_key never leaves)."""
    if not isinstance(config, dict) or not config:
        return None
    cleaned = {
        k: v for k, v in config.items()
        if k in ("provider", "base_url", "workflow_id", "inputs", "timeout_seconds",
                 # http 连接器：url/headers/body/method 可移植（api_key 仍被剔除）
                 "method", "url", "headers", "body", "allow_private_hosts")
    }
    return cleaned or None


def export_workflow_dsl(wf):
    """Build the DSL dict for a workflow (caller serializes to YAML)."""
    warnings = []
    steps = sorted(wf.steps, key=lambda s: (s.order or 0, s.id))
    name_by_id = {s.id: s.name for s in steps}

    exported_steps = []
    for s in steps:
        sub_name = None
        if s.sub_workflow_id:
            sub = Workflow.query.get(s.sub_workflow_id)
            if sub:
                sub_name = sub.name
            else:
                warnings.append(
                    f"步骤 {s.step_key} 的子工作流 #{s.sub_workflow_id} 不存在，已移除该链接")
        exported_steps.append({
            "step_key": s.step_key,
            "name": s.name,
            "description": s.description or "",
            "order": s.order or 0,
            "required_capabilities": s.required_capabilities or [],
            "depends_on": s.depends_on or [],
            "condition": s.condition or None,
            "on_failure": s.on_failure or "abort",
            "retry_count": s.retry_count or 0,
            "timeout_seconds": s.timeout_seconds or 0,
            "integration_config": _clean_integration_config(s.integration_config),
            # 跨环境无意义的 id 一律不导出（agent_id/task_template_id/sub_workflow_id）
            "sub_workflow_name": sub_name,
        })

    layout = {}
    definition = wf.definition or {}
    raw_layout = definition.get("layout")
    if isinstance(raw_layout, dict):
        layout = {k: v for k, v in raw_layout.items()
                  if isinstance(v, dict) and isinstance(v.get("x"), (int, float))}

    dsl = {
        DSL_MAGIC: DSL_MARKER,
        "version": DSL_VERSION,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workflow": {
            "name": wf.name,
            "description": wf.description or "",
            "max_parallel_steps": wf.max_parallel_steps or 0,
        },
        "steps": exported_steps,
    }
    if layout:
        dsl["layout"] = layout
    if warnings:
        dsl["warnings"] = warnings
    return dsl


def dumps_workflow_dsl(dsl_dict):
    return yaml.safe_dump(dsl_dict, allow_unicode=True, sort_keys=False)


# ── Import ─────────────────────────────────────────────────────────────


def loads_workflow_dsl(dsl_text):
    """Parse YAML (or a plain JSON object) into a dict; wrap errors."""
    try:
        data = yaml.safe_load(dsl_text)
    except yaml.YAMLError as exc:
        raise DslError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise DslError("DSL 必须是映射结构（YAML object）")
    return data


def check_version_compatibility(version):
    """Dify-style: reject missing/newer/major-mismatched DSL versions."""
    if not version or not isinstance(version, str):
        raise DslError("DSL 缺少 version 字段")
    try:
        major = int(str(version).split(".")[0])
    except ValueError as exc:
        raise DslError(f"无法识别的 DSL version: {version}") from exc
    if major != SUPPORTED_MAJOR:
        raise DslError(
            f"DSL 版本不兼容：文件 v{version}，本平台支持 v{SUPPORTED_MAJOR}.x")
    if str(version) != DSL_VERSION:
        # minor 落后/超前均放行，由调用方提示
        pass


def _validate_graph(steps):
    keys = [s.get("step_key") for s in steps]
    if any(not k or not isinstance(k, str) for k in keys):
        raise DslError("存在空的 step_key")
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise DslError(f"step_key 重复: {sorted(dupes)}")
    key_set = set(keys)
    for s in steps:
        for dep in (s.get("depends_on") or []):
            if dep not in key_set:
                raise DslError(
                    f"步骤 {s['step_key']} 依赖了不存在的步骤 {dep}")
        cond = s.get("condition")
        if isinstance(cond, dict) and cond.get("step_key") and \
                cond["step_key"] not in key_set:
            raise DslError(
                f"步骤 {s['step_key']} 的条件引用了不存在的步骤 {cond['step_key']}")
    # Kahn 检环
    indeg = {k: 0 for k in keys}
    for s in steps:
        for _ in (s.get("depends_on") or []):
            indeg[s["step_key"]] += 1
    queue = [k for k, d in indeg.items() if d == 0]
    seen = 0
    dependents = {k: [] for k in keys}
    for s in steps:
        for dep in (s.get("depends_on") or []):
            dependents[dep].append(s["step_key"])
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in dependents[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(keys):
        raise DslError("步骤依赖构成环，无法导入")


def import_workflow_dsl(owner_id, data, name_override=None):
    """Create a Workflow (with steps) from a validated DSL dict.

    Returns (workflow, warnings). Raises DslError on invalid input.
    Sub-workflow dependencies are resolved by NAME within the owner's
    workflows; unknown names are collected and rejected up-front.
    """
    if data.get(DSL_MAGIC) != DSL_MARKER:
        raise DslError(f"缺少标记字段 {DSL_MAGIC}: workflow-dsl，不是本平台的 DSL 文件")
    check_version_compatibility(data.get("version"))

    wf_meta = data.get("workflow") or {}
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DslError("DSL 缺少 steps 或 steps 为空")
    for s in steps:
        if not isinstance(s, dict):
            raise DslError("steps 中存在非对象条目")
    _validate_graph(steps)

    # 依赖解析：sub_workflow_name → 本 owner 的同名工作流
    warnings = list(data.get("warnings") or [])
    missing = []
    name_map = {}
    for s in steps:
        sub_name = s.get("sub_workflow_name")
        if not sub_name:
            continue
        if sub_name not in name_map:
            sub = Workflow.query.filter_by(owner_id=owner_id, name=sub_name).first()
            name_map[sub_name] = sub
        if not name_map[sub_name]:
            missing.append(sub_name)
    if missing:
        raise DslError(
            "以下子工作流依赖在本平台不存在（请先导入/创建同名工作流）: "
            + ", ".join(sorted(set(missing))))

    from .workflow_external_steps import validate_integration_config

    wf = Workflow.create(
        owner_id=owner_id,
        name=(name_override or wf_meta.get("name") or "").strip()
        or f"导入的工作流 {datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        description=wf_meta.get("description") or "",
        definition={
            "steps": [{"step_key": s.get("step_key"),
                       "depends_on": s.get("depends_on") or []} for s in steps],
            **({"layout": data["layout"]} if isinstance(data.get("layout"), dict) else {}),
        },
        is_active=True,
        max_parallel_steps=wf_meta.get("max_parallel_steps") or 0,
    )
    from ._shared import db
    db.session.flush()  # 取 workflow.id

    for i, s in enumerate(steps):
        integration = None
        raw_integ = s.get("integration_config")
        if isinstance(raw_integ, dict) and raw_integ:
            # 导入的连接器不含 api_key，先占位校验形状；密钥由用户在 UI 补填
            probe = dict(raw_integ)
            probe.setdefault("api_key", "placeholder-needs-key")
            try:
                validate_integration_config(probe)
            except ValueError as exc:
                db.session.rollback()
                raise DslError(f"步骤 {s.get('step_key')} 连接器配置无效: {exc}") from exc
            integration = dict(raw_integ)
        sub = name_map.get(s.get("sub_workflow_name"))
        WorkflowStep.create(
            workflow_id=wf.id,
            step_key=(s.get("step_key") or "").strip(),
            name=s.get("name") or s.get("step_key"),
            description=s.get("description") or "",
            order=s.get("order") if isinstance(s.get("order"), int) else i,
            required_capabilities=s.get("required_capabilities") or [],
            depends_on=s.get("depends_on") or [],
            condition=s.get("condition") or None,
            on_failure=s.get("on_failure") or "abort",
            retry_count=s.get("retry_count") or 0,
            timeout_seconds=s.get("timeout_seconds") or 0,
            integration_config=integration,
            sub_workflow_id=sub.id if sub else None,
        )
    return wf, warnings
