"""Agent cron 触发器内置调度器。

扫描到期的 cron 触发器并派发动作：run_agent（建 queued AgentRun，平台托管
Runner 的触发入口）或 create_task（定时自动创建任务，让 Agent 能被定时"布置
作业"）。脚本 scripts/run_agent_cron_scheduler.py 是同一 tick 的独立进程薄壳。

通过 AGENT_CRON_SCHEDULER_ENABLED=true 开启（默认关，与编排调度器/GoalLoop
看门狗同一门控风格）。多 worker 部署只在其中一个 worker 开启，线程不做跨进程
协调；幂等键（AgentRun.idempotency_key / trigger.last_fired_key）兜底防重。
"""
import hashlib
import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_scheduler_thread = None
_scheduler_lock = threading.Lock()
_stop_event = threading.Event()
_last_run = None  # dict: {finished_at, matched, created}


def enabled() -> bool:
    return (os.getenv('AGENT_CRON_SCHEDULER_ENABLED', 'false').strip().lower()
            in ('1', 'true', 'yes', 'on'))


def interval_seconds(default=30) -> int:
    try:
        return max(10, int(os.getenv('AGENT_CRON_SCHEDULER_INTERVAL_SECONDS', '') or default))
    except ValueError:
        return default


def idempotency_key(trigger_id, fire_at):
    raw = f"cron:{trigger_id}:{fire_at.isoformat()}"
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return f"cron:{trigger_id}:{digest[:40]}"


def _fire_run_agent(trigger, fire_at, now):
    """动作 run_agent：建 queued AgentRun，由 Runner/运行时领取执行。"""
    from models import AgentRun, AgentRunState
    from api.agent_common import generate_id

    idem_key = idempotency_key(trigger.id, fire_at)
    if AgentRun.query.filter_by(idempotency_key=idem_key).first():
        return False

    db = _db()
    db.session.add(AgentRun(
        run_id=generate_id('run'),
        workspace_id=trigger.workspace_id,
        agent_id=trigger.agent_id,
        trigger_id=trigger.id,
        trigger_reason='cron.tick',
        input_payload={
            'trigger_type': 'cron',
            'cron_expr': trigger.cron_expr,
            'fire_at': fire_at.isoformat(),
        },
        state=AgentRunState.QUEUED.value,
        scheduled_at=now,
        attempt_count=0,
        idempotency_key=idem_key,
        created_by='system:cron_scheduler',
    ))
    return True


def _fire_create_task(trigger, fire_at, now):
    """动作 create_task：按 action_payload 定时创建任务（定时布置作业）。

    幂等靠 trigger.last_fired_key 记录本次触发的幂等键；创建后 emit
    task_event('created')，让下游任务事件触发器照常联动。
    """
    from models import Project, Task

    payload = trigger.action_payload if isinstance(trigger.action_payload, dict) else {}
    project_id = payload.get('project_id')
    title = str(payload.get('title') or '').strip()
    if not project_id or not title:
        logger.warning("[AGENT_CRON] trigger %s create_task payload invalid, skip", trigger.id)
        return False

    project = Project.query.filter_by(id=int(project_id), organization_id=trigger.workspace_id).first()
    if not project:
        logger.warning("[AGENT_CRON] trigger %s create_task project %s not in workspace, skip",
                       trigger.id, project_id)
        return False

    idem_key = idempotency_key(trigger.id, fire_at)
    if trigger.last_fired_key == idem_key:
        return False

    from models import TaskPriority
    priority_raw = str(payload.get('priority') or 'medium').strip().lower()
    try:
        priority = TaskPriority(priority_raw)
    except ValueError:
        priority = TaskPriority.MEDIUM

    task = Task(
        project_id=project.id,
        owner_id=project.owner_id,
        title=title[:500],
        content=str(payload.get('description') or '') or None,
        priority=priority,
        tags=[str(t) for t in (payload.get('tags') or []) if str(t).strip()],
        creator_type='ai',
        creator_identifier=f'cron-trigger:{trigger.name}',
        is_ai_task=True,
    )
    db = _db()
    db.session.add(task)
    db.session.flush()
    trigger.last_fired_key = idem_key

    try:
        from api.agent_trigger_engine import emit_task_event
        emit_task_event(task, 'created', {'source': 'cron_trigger', 'trigger_id': trigger.id},
                        actor='system:cron_scheduler')
    except Exception:  # noqa: BLE001 - 事件发射失败不阻塞任务创建
        logger.exception("[AGENT_CRON] emit created event failed for task %s", task.id)

    logger.info("[AGENT_CRON] trigger %s created task %s (%s)", trigger.id, task.id, title)
    return True


_ACTIONS = {
    'run_agent': _fire_run_agent,
    'create_task': _fire_create_task,
}


def _db():
    from models import db
    return db


def tick(limit=200):
    """扫描到期 cron 触发器并派发动作；返回 (fired, matched)。"""
    from models import db, AgentTrigger, AgentTriggerType
    from api.agent_automation import _compute_next_fire_at

    now = datetime.utcnow()
    triggers = AgentTrigger.query.filter(
        AgentTrigger.trigger_type == AgentTriggerType.CRON.value,
        AgentTrigger.enabled.is_(True),
        AgentTrigger.next_fire_at.isnot(None),
        AgentTrigger.next_fire_at <= now,
    ).order_by(AgentTrigger.next_fire_at.asc()).limit(limit).all()

    fired = 0
    for trigger in triggers:
        fire_at = trigger.next_fire_at or now
        action = str(trigger.action or 'run_agent').strip().lower()
        handler = _ACTIONS.get(action)
        if handler is None:
            logger.warning("[AGENT_CRON] trigger %s unknown action %r, skip", trigger.id, action)
        elif handler(trigger, fire_at, now):
            fired += 1

        trigger.last_triggered_at = now
        trigger.next_fire_at = _compute_next_fire_at(trigger.cron_expr or '', now)

    db.session.commit()
    return fired, len(triggers)


def _loop(app, interval):
    while not _stop_event.wait(interval):
        try:
            with app.app_context():
                fired, matched = tick()
                global _last_run
                from datetime import datetime as _dt
                _last_run = {
                    'finished_at': _dt.utcnow().isoformat(),
                    'matched': matched,
                    'fired': fired,
                }
                if fired:
                    logger.info("[AGENT_CRON] matched=%s fired=%s", matched, fired)
        except Exception as e:  # noqa: BLE001
            logger.exception("[AGENT_CRON] tick failed: %s", e)


def start_scheduler(app):
    """按环境变量门控启动 cron 调度线程；重复调用安全。"""
    global _scheduler_thread, _stop_event
    if not enabled():
        return False
    interval = interval_seconds()
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_loop,
            args=(app, interval),
            name="agent-cron-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()
        logger.info("[AGENT_CRON] Scheduler started: interval=%ss", interval)
        return True


def stop_scheduler():
    global _scheduler_thread
    _stop_event.set()
    thread = _scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _scheduler_thread = None


def scheduler_status():
    return {
        "enabled": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "last_run": _last_run,
    }
