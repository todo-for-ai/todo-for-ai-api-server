"""依赖门交接（task handoff）：上游产出流向下游 + 终态解锁通知。

多 Agent 按 blocked_by 任务图接力时，两个交接动作都在这里：

- upstream_context_entries：下游任务被领取时，聚合所有已解除阻塞者的
  shared_context，随 pull payload 注入——上游 Agent 写的中间产出
  （研究结论、方案、代码计划等）无需下游显式拉取即到手。
- notify_downstream_unlocked：任务提交到终态后，找出因它而完全解锁的
  下游任务，写 ``dependency.unlocked`` 事件进下游任务时间线留痕。
"""

from models import db, SharedContext, Task, TaskEvent, TaskStatus

# 注入 payload 的体量上限：防止上游写过量的 shared_context 把下游
# 任务的首轮上下文撑爆（超限截断，完整内容仍可经 shared-context API 读）。
_UPSTREAM_VALUE_MAX_CHARS = 2000
_UPSTREAM_KEYS_PER_TASK = 20


def _blocker_ids(task):
    ids = []
    for raw in (task.blocked_by_task_ids or []):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def upstream_context_entries(task):
    """聚合 task 各上游（blocked_by）任务的交接上下文。

    返回按上游任务 id 排序的列表：
    ``[{"task_id", "title", "shared_context": {key: value}}]``，
    无上游或上游无产出时返回空列表。只在上游已进终态（交接语义成立）
    时才有意义，但为保持函数无副作用，终态过滤交由调用方（依赖门）保证。
    """
    blocker_ids = _blocker_ids(task)
    if not blocker_ids:
        return []

    blockers = db.session.query(Task).filter(Task.id.in_(blocker_ids)).all()
    entries = []
    for blocker in sorted(blockers, key=lambda t: t.id):
        rows = (
            SharedContext.query.filter_by(task_id=blocker.id)
            .order_by(SharedContext.key.asc())
            .limit(_UPSTREAM_KEYS_PER_TASK)
            .all()
        )
        if not rows:
            continue
        context = {
            row.key: row.value[:_UPSTREAM_VALUE_MAX_CHARS]
            for row in rows
        }
        entries.append({
            'task_id': blocker.id,
            'title': blocker.title,
            'shared_context': context,
        })
    return entries


def downstream_unblocked_by(task):
    """找出因 task 进入终态而完全解锁的 TODO 任务列表。

    blocked_by_task_ids 是 JSON 列无法索引查询，按同项目扫描后
    在应用层过滤；候选只取 TODO（IN_PROGRESS/REVIEW 已在跑，谈不上解锁）。
    """
    from api.agent_runtime_pull import _DEPENDENCY_TERMINAL_STATUSES, _unsatisfied_blocker_ids

    if task.status not in _DEPENDENCY_TERMINAL_STATUSES:
        return []

    candidates = Task.query.filter(
        Task.project_id == task.project_id,
        Task.status == TaskStatus.TODO,
        Task.blocked_by_task_ids.isnot(None),
    ).all()

    unblocked = []
    for candidate in candidates:
        blocker_ids = _blocker_ids(candidate)
        if task.id not in blocker_ids:
            continue
        if not _unsatisfied_blocker_ids(candidate):
            unblocked.append(candidate)
    return unblocked


def notify_downstream_unlocked(task):
    """task 到终态后，向所有刚解锁的下游任务写解锁事件（时间线留痕）。

    返回解锁的下游任务 id 列表（空即无事发生）；事件由调用方统一提交。
    """
    unblocked = downstream_unblocked_by(task)
    for candidate in unblocked:
        db.session.add(TaskEvent.record(
            task_id=candidate.id,
            event_type='dependency.unlocked',
            actor_type='system',
            payload={
                'unblocked_by_task_id': task.id,
                'unblocked_by_status': task.status.value if task.status else None,
            },
        ))
    return [candidate.id for candidate in unblocked]
