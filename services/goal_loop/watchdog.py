"""多日续航看门狗巡检（幂等，供看门狗调度器/手动 kick 调用）。"""

import os
from datetime import datetime

from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease, Task, TaskStatus, db, GoalLoop, GoalLoopStatus

from .constants import DEFAULT_STUCK_TASK_HOURS
from .state_machine import _finish, maybe_advance, notify_task_finished, time_budget_exceeded
from .query import loop_tasks


def _stuck_task_hours() -> float:
    """看门狗判定轮次卡死的小时数（GOAL_LOOP_STUCK_TASK_HOURS）。"""
    try:
        return float(os.getenv('GOAL_LOOP_STUCK_TASK_HOURS', '') or DEFAULT_STUCK_TASK_HOURS)
    except ValueError:
        return DEFAULT_STUCK_TASK_HOURS


def _abandon_task_runtime(task_id: int, reason: str):
    """作废任务在途的运行时凭证（租约/attempt），避免 runtime 继续持有。"""
    AgentTaskLease.query.filter_by(task_id=task_id, active=True).update({'active': False})
    AgentTaskAttempt.query.filter(
        AgentTaskAttempt.task_id == task_id,
        AgentTaskAttempt.state == AgentTaskAttemptState.ACTIVE,
    ).update({'state': AgentTaskAttemptState.ABORTED, 'failure_code': reason[:64]})


def watchdog_sweep(limit=200) -> dict:
    """多日续航巡检（幂等，供定时任务/手动 kick 调用）。

    ① 时长预算耗尽的 RUNNING 循环 → 终态 LIMIT_REACHED；
    ② 卡死轮次：活跃任务超过 N 小时无活动（agent 掉线/租约失效）→ 任务置
       cancelled 并作废在途租约 → 走既有评审兜底（blocked → 受阻计数）；
    ③ 漏触发自愈：RUNNING 且无活跃任务（如重启丢失触发）→ 幂等推进
       （maybe_advance 自带 CAS 与护栏，重复调用安全）。
    """
    now = datetime.utcnow()
    stuck_hours = _stuck_task_hours()
    result = {'checked': 0, 'time_exhausted': 0, 'stuck_cancelled': 0, 'kicked': 0}

    loops = (
        GoalLoop.query.filter(GoalLoop.status == GoalLoopStatus.RUNNING)
        .order_by(GoalLoop.id)
        .limit(limit)
        .all()
    )
    for loop in loops:
        result['checked'] += 1
        db.session.expire(loop)
        if loop.status != GoalLoopStatus.RUNNING:
            continue

        # ① 时长预算
        if time_budget_exceeded(loop):
            _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                    last_error=f'时长预算 {loop.time_budget_hours} 小时已耗尽，目标未宣告完成')
            result['time_exhausted'] += 1
            continue

        tasks = loop_tasks(loop.id)
        active = [t for t in tasks if t.status in
                  (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED)]

        if active:
            # ② 卡死轮次：最老活跃任务超过阈值小时无活动
            # created_at 非空约束保证时间戳必然存在
            idle_hours = (now - min(
                t.updated_at or t.created_at for t in active)).total_seconds() / 3600.0
            if idle_hours < stuck_hours:
                continue
            for t in active:
                _abandon_task_runtime(t.id, 'WATCHDOG_STUCK')
                t.status = TaskStatus.CANCELLED
            db.session.commit()
            result['stuck_cancelled'] += len(active)
            for t in active:
                notify_task_finished(t.id)
            continue

        # ③ 漏触发自愈（无活跃任务时幂等推进）
        kick = maybe_advance(loop.id)
        if kick.get('advanced'):
            result['kicked'] += 1
    return result
