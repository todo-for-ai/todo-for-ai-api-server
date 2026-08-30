"""
Agent 触发引擎（任务事件侧）
"""

import hashlib
from datetime import datetime
from models import (
    db,
    AgentTrigger,
    AgentTriggerType,
    AgentRun,
    AgentRunState,
    TaskEventOutbox,
    Project,
)
from .agent_common import generate_id


def _is_trigger_match(trigger, task, event_type, payload):
    event_types = [str(item).strip().lower() for item in (trigger.task_event_types or [])]
    if event_type not in event_types:
        return False

    task_filter = trigger.task_filter or {}
    project_ids = task_filter.get('project_ids') or []
    if project_ids and int(task.project_id) not in [int(pid) for pid in project_ids if str(pid).isdigit()]:
        return False

    tag_whitelist = [str(item).strip().lower() for item in (task_filter.get('tags') or []) if str(item).strip()]
    if tag_whitelist:
        task_tags = [str(item).strip().lower() for item in (task.tags or []) if str(item).strip()]
        if not any(tag in task_tags for tag in tag_whitelist):
            return False

    if event_type == 'status_changed':
        from_statuses = [str(item).strip().lower() for item in (task_filter.get('from_status') or []) if str(item).strip()]
        to_statuses = [str(item).strip().lower() for item in (task_filter.get('to_status') or []) if str(item).strip()]
        from_status = str(payload.get('from_status') or '').strip().lower()
        to_status = str(payload.get('to_status') or '').strip().lower()
        if from_statuses and from_status not in from_statuses:
            return False
        if to_statuses and to_status not in to_statuses:
            return False

    return True


def _build_idempotency_key(trigger, event_type, task_id, payload):
    dedup_window = max(10, int(trigger.dedup_window_seconds or 60))
    ts = datetime.utcnow().timestamp()
    bucket = int(ts // dedup_window)

    status_key = ''
    if event_type == 'status_changed':
        status_key = f"{payload.get('from_status')}->{payload.get('to_status')}"

    raw = f"trigger:{trigger.id}|event:{event_type}|task:{task_id}|status:{status_key}|bucket:{bucket}"
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return f"evt:{trigger.id}:{digest[:40]}"


def emit_task_event(task, event_type, payload, actor):
    event_name = str(event_type or '').strip().lower()
    if event_name not in {'created', 'updated', 'status_changed', 'completed', 'assigned', 'mentioned'}:
        return None

    project = task.project
    if not project and task.project_id:
        project = Project.query.get(task.project_id)
    workspace_id = project.organization_id if project else None

    outbox = TaskEventOutbox(
        event_id=generate_id('tev'),
        event_type=f'task.{event_name}',
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=workspace_id,
        payload=payload or {},
        occurred_at=datetime.utcnow(),
        created_by=actor,
    )
    db.session.add(outbox)
    event_id = outbox.event_id

    if not workspace_id:
        return event_id

    triggers = AgentTrigger.query.filter_by(
        workspace_id=workspace_id,
        trigger_type=AgentTriggerType.TASK_EVENT.value,
        enabled=True,
    ).all()

    for trigger in triggers:
        if not _is_trigger_match(trigger, task, event_name, payload or {}):
            continue

        idem_key = _build_idempotency_key(trigger, event_name, task.id, payload or {})
        exists = AgentRun.query.filter_by(idempotency_key=idem_key).first()
        if exists:
            continue

        run = AgentRun(
            run_id=generate_id('run'),
            workspace_id=workspace_id,
            agent_id=trigger.agent_id,
            trigger_id=trigger.id,
            trigger_reason=f'task.{event_name}',
            input_payload={
                'task_id': task.id,
                'project_id': task.project_id,
                'event': f'task.{event_name}',
                'payload': payload or {},
            },
            state=AgentRunState.QUEUED.value,
            scheduled_at=datetime.utcnow(),
            attempt_count=0,
            idempotency_key=idem_key,
            created_by=actor,
        )
        db.session.add(run)
        trigger.last_triggered_at = datetime.utcnow()
    return event_id
