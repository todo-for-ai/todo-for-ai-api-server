"""
目标分解服务（P2.1）

Epic → 带 DoD 的任务图：复用 ai_task_split 的 LLM 生产调用生成任务骨架
（title/description/dod/priority/depends_on），确定性落地为 tasks 行
（epic_id 关联、DoD 填充、依赖写入 blocking/blocked_by）。
"""

from typing import Any, Dict, List, Optional

from models import Task, TaskPriority, db


def _build_prompt(epic, workspace_context: Optional[str]) -> str:
    metrics = epic.goal.metrics or []
    metrics_text = "；".join(str(m) for m in metrics) if metrics else "未定义"
    context_text = f"\n工作区背景：{workspace_context}" if workspace_context else ""
    return (
        f"产品目标：{epic.goal.title}\n"
        f"成功指标：{metrics_text}\n"
        f"特性（Epic）：{epic.title}\n"
        f"Epic 描述：{epic.description or '（无）'}{context_text}\n\n"
        "请将该 Epic 拆分为 2-6 个可执行的开发任务（任务图）。"
        "每个任务必须携带机器可验证的完成标准（dod），类型限定为 "
        "test/build/lint/command（pr/manual 由人工与平台核验，不要生成）。"
        "返回 JSON：\n"
        "{\n"
        '  "tasks": [\n'
        '    {"title": "任务标题", "description": "描述", "priority": "medium",\n'
        '     "dod": [{"type": "test", "value": "pytest tests/"}],\n'
        '     "depends_on": []}\n'
        "  ],\n"
        '  "execution_order": "顺序/并行建议"\n'
        "}"
    )


def _normalize_dod(dod_raw) -> List[Dict[str, str]]:
    from models import TaskEvidenceRecord

    normalized = []
    if not isinstance(dod_raw, list):
        return normalized
    for item in dod_raw:
        if not isinstance(item, dict):
            continue
        dod_type = str(item.get("type") or "").strip().lower()
        if dod_type in TaskEvidenceRecord.TYPES:
            normalized.append({"type": dod_type, "value": str(item.get("value") or "")[:500]})
    return normalized


def expand_epic_to_tasks(epic, workspace_context: Optional[str] = None,
                         project_id: Optional[int] = None) -> Dict[str, Any]:
    """Epic → 任务图。LLM 生成骨架，本函数确定性落地。

    project_id 显式指定任务归属项目；缺省回退 goal workspace 内最新项目。
    返回 {tasks: [task_ids], execution_order}。
    LLM 不可用时抛 ValueError（由端点转 503）。
    """
    from services.ai_service import call_llm_production
    from api.ai_task_split import parse_llm_json_response

    prompt = _build_prompt(epic, workspace_context)
    response = call_llm_production(prompt=prompt, feature="goal_expand_epic")
    content = getattr(response, "content", None) or (response.get("content") if isinstance(response, dict) else None)
    if not content:
        raise ValueError("LLM returned empty content")

    parsed = parse_llm_json_response(content)
    if not parsed.get("success"):
        raise ValueError(f"LLM response parse failed: {parsed.get('error')}")
    data = parsed.get("data") or {}
    if isinstance(data, str):
        # 某些网关二次转义
        import json as _json
        data = _json.loads(data)
    tasks_data = data.get("tasks") or []
    if not tasks_data:
        raise ValueError("LLM returned no tasks")

    # 第一遍：创建任务（无依赖）
    id_by_index: Dict[int, int] = {}
    created_ids: List[int] = []
    for index, item in enumerate(tasks_data[:8], start=1):
        if not isinstance(item, dict) or not item.get("title"):
            continue
        priority = "medium"
        try:
            priority = TaskPriority(str(item.get("priority") or "medium")).value
        except ValueError:
            pass
        task = Task.create(
            project_id=project_id or _resolve_project_id(epic),
            owner_id=epic.goal.owner_id,
            epic_id=epic.id,
            title=str(item["title"])[:500],
            content=str(item.get("description") or ""),
            priority=TaskPriority(priority),
            is_ai_task=True,
            dod=_normalize_dod(item.get("dod")),
            human_intervention_count=0,
            created_by=f"goal:{epic.goal_id}",
        )
        db.session.add(task)
        db.session.flush()
        id_by_index[index] = task.id
        created_ids.append(task.id)

    # 第二遍：按 depends_on 写依赖（blocking/blocked_by）
    for index, item in enumerate(tasks_data[:8], start=1):
        task_id = id_by_index.get(index)
        if not task_id:
            continue
        task = db.session.get(Task, task_id)
        for dep in (item.get("depends_on") or []):
            dep_id = id_by_index.get(int(dep)) if str(dep).isdigit() else None
            if dep_id and dep_id != task_id:
                blocked = task.blocked_by_task_ids or []
                if dep_id not in blocked:
                    blocked.append(dep_id)
                    task.blocked_by_task_ids = blocked
                dep_task = db.session.get(Task, dep_id)
                blocking = dep_task.blocking_task_ids or []
                if task.id not in blocking:
                    blocking.append(task.id)
                    dep_task.blocking_task_ids = blocking

    db.session.commit()
    return {"tasks": created_ids, "execution_order": data.get("execution_order")}


def _resolve_project_id(epic) -> int:
    """Epic 归属项目：取 goal.workspace 内第一个项目（MVP 约定，可由 epic 描述覆盖）。"""
    from models import Project

    project = (
        Project.query
        .filter_by(organization_id=epic.goal.workspace_id)
        .order_by(Project.id.desc())
        .first()
    )
    if not project:
        raise ValueError("no project in goal workspace to attach tasks")
    return project.id
