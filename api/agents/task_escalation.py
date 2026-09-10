"""逾期任务优先级自动升级（周期维护职责）。

从 _workflow_helpers.py 拆出：与 DAG 推进引擎无关的定时维护逻辑——
逾期未完结任务的优先级沿 low→medium→high→urgent 阶梯逐级上调，
并给任务所属项目的 owner 发通知。
被 api/agents/maintenance.py 的三个维护端点调用。
"""

from datetime import datetime, timedelta

from ._shared import (
    db,
    Notification,
    Project,
    Task,
    TaskStatus,
)

# 优先级阶梯：当前级别 → 上一级
PRIORITY_LADDER = {
    "low": "medium",
    "medium": "high",
    "high": "urgent",
}


def escalate_overdue_tasks(owner_id=None, overdue_after_days=1):
    """Auto-escalate the priority of overdue tasks that are not yet urgent.

    Tasks whose due_date is in the past and whose status is not in a terminal
    state (done / cancelled) will have their priority bumped one level.
    Returns the list of escalated task IDs.
    """
    from models.task import TaskPriority

    now = datetime.utcnow()
    cutoff = now - timedelta(days=overdue_after_days)

    query = Task.query.filter(
        Task.due_date.isnot(None),
        Task.due_date < cutoff,
        Task.status.notin_([TaskStatus.DONE, TaskStatus.CANCELLED]),
        Task.priority != TaskPriority.URGENT,
    )
    if owner_id:
        project_ids = [p.id for p in Project.query.filter_by(owner_id=owner_id).all()]
        query = query.filter(Task.project_id.in_(project_ids))

    escalated = []
    for task in query.all():
        current = task.priority.value if task.priority else "medium"
        next_level = PRIORITY_LADDER.get(current)
        if next_level:
            task.priority = TaskPriority(next_level)
            db.session.add(task)
            escalated.append(task.id)
            Notification.create_notification(
                user_id=task.project.owner_id if task.project and task.project.owner_id else None,
                event_type="task_priority_escalated",
                task_id=task.id,
                payload={
                    "old_priority": current,
                    "new_priority": next_level,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                },
            )

    if escalated:
        db.session.commit()
    return escalated
