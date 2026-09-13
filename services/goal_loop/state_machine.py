"""推进状态机与护栏（maybe_advance / limit / stall / 生命周期操作）。"""

from datetime import datetime, timedelta

from models import db, GoalLoop, GoalLoopStatus, Task, TaskStatus, Project
from models.agent import Agent

from .constants import (
    ACTIVE_TASK_STATUSES,
    DEFAULT_NO_PROGRESS_ROUNDS,
    DEFAULT_ROUNDS_LIMIT,
    DEFAULT_STALL_LIMIT,
    MAX_ROUNDS_LIMIT,
    MAX_TIME_BUDGET_HOURS,
    clamp_int,
    naive_utc_now,
)
from .dispatch import (
    auto_assign,
    create_round_task,
    ensure_cloud_executor,
    pick_executor,
)
from .planning import call_decompose, call_review
from .query import loop_tasks, rounds_done, trailing_failure_streak


def time_budget_exceeded(loop) -> bool:
    if not loop.time_budget_hours or not loop.started_at:
        return False
    elapsed = naive_utc_now() - loop.started_at
    return elapsed.total_seconds() >= loop.time_budget_hours * 3600


def maybe_advance(loop_id, trigger_task_id=None) -> dict:
    """推进一次循环（幂等、并发安全）。返回 {advanced: bool, reason: str}。"""
    loop = db.session.get(GoalLoop, loop_id)
    if not loop:
        return {'advanced': False, 'reason': 'loop_not_found'}

    if loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': f'not_running:{loop.status.value}'}

    claimed = GoalLoop.query.filter_by(id=loop_id, advancing=0).update({'advancing': 1})
    db.session.commit()
    if not claimed:
        return {'advanced': False, 'reason': 'already_advancing'}

    try:
        return _advance_locked(loop_id, trigger_task_id)
    finally:
        try:
            GoalLoop.query.filter_by(id=loop_id).update({'advancing': 0})
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()


def _advance_locked(loop_id, trigger_task_id=None) -> dict:
    loop = db.session.get(GoalLoop, loop_id)
    if not loop or loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': 'not_running'}

    done = rounds_done(loop.id)
    if done >= loop.rounds_limit:
        _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                last_error=f'轮数上限 {loop.rounds_limit} 已耗尽，目标未宣告完成')
        return {'advanced': False, 'reason': 'rounds_limit'}

    if time_budget_exceeded(loop):
        _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                last_error=f'时长预算 {loop.time_budget_hours} 小时已耗尽，目标未宣告完成')
        return {'advanced': False, 'reason': 'time_budget_exhausted'}

    tasks_all = loop_tasks(loop.id)
    active = [t for t in tasks_all if t.status in ACTIVE_TASK_STATUSES]
    if active:
        return {'advanced': False, 'reason': 'active_task_exists'}

    last_status = None
    if trigger_task_id:
        trigger = db.session.get(Task, trigger_task_id)
        if trigger is not None:
            last_status = trigger.status.value if trigger.status else None

    # ── ① 无计划：先拆解 ──
    if not loop.plan:
        try:
            steps = call_decompose(loop)
        except Exception as exc:  # noqa: BLE001
            return _register_stall(loop, f'decompose_failed: {exc}')
        db.session.expire(loop)
        if loop.status != GoalLoopStatus.RUNNING:
            return {'advanced': False, 'reason': 'not_running_after_planner'}
        loop.plan = steps
        loop.plan_index = 0
        loop.plan_revision = (loop.plan_revision or 0) + 1

    plan = loop.plan or []
    plan_index = loop.plan_index or 0

    # ── ② 计划有剩余步骤 且 上轮成功（或首轮）：直接物化下一步 ──
    if plan_index < len(plan) and (last_status in (None, 'done')):
        # 上下文自动压缩：物化前按节奏滚动刷新历史摘要，
        # 保证新轮次的执行者拿到最新记忆（失败只记日志不阻断）
        from .context import maybe_compress
        maybe_compress(loop)
        step = plan[plan_index]
        executor = pick_executor(loop, step)
        task = create_round_task(loop, step, executor)
        loop.plan_index = plan_index + 1
        loop.last_task_id = task.id
        loop.stall_count = 0
        loop.last_error = None
        if loop.started_at is None:
            loop.started_at = naive_utc_now()
        db.session.commit()
        ensure_cloud_executor(loop, executor)
        auto_assign(task, executor)
        return {'advanced': True, 'reason': 'task_created', 'task_id': task.id}

    # ── ③ 计划耗尽 或 上轮失败：评审 ──
    try:
        decision = call_review(loop, last_status or 'unknown')
    except Exception as exc:  # noqa: BLE001
        return _register_stall(loop, f'review_failed: {exc}')

    db.session.expire(loop)
    if loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': 'not_running_after_planner'}

    action = (decision.get('action') or '').strip().lower()

    if action == 'complete':
        loop.status = GoalLoopStatus.DONE
        loop.completion_summary = decision.get('reason') or ''
        loop.stall_count = 0
        loop.finished_at = naive_utc_now()
        _record_success_experience(loop)
        db.session.commit()
        # 记忆沉淀：会话级总结 + 项目级持久结论（失败不影响循环终态）
        from services.memory.loop_hooks import on_loop_completed
        on_loop_completed(loop, loop.completion_summary, rounds_done(loop.id))
        return {'advanced': False, 'reason': 'completed'}

    if action == 'extend':
        steps = decision.get('steps')
        from .planning import valid_steps
        if not valid_steps(steps, loop.rounds_limit):
            return _register_stall(loop, 'extend_without_valid_steps')
        # 无进展护栏：用户要"死循环"也必须有退出点——连续 N 轮失败后
        # 拒绝继续 extend（宣告 complete 仍被允许），强制计 stall 走
        # STALLED 退出，避免规划器无限换着花样空转烧预算。
        streak = trailing_failure_streak(loop.id)
        if streak >= DEFAULT_NO_PROGRESS_ROUNDS:
            return _register_stall(
                loop,
                f'no_progress: 连续 {streak} 轮失败（阈值 {DEFAULT_NO_PROGRESS_ROUNDS}），'
                '拒绝继续 extend；请人工介入或调整目标后 resume',
            )
        remaining = list(plan[max(plan_index, 0):])
        loop.plan = remaining + steps
        loop.plan_index = max(plan_index, 0)
        loop.plan_revision = (loop.plan_revision or 0) + 1
        from .context import maybe_compress
        maybe_compress(loop)
        step = loop.plan[loop.plan_index]
        executor = pick_executor(loop, step)
        task = create_round_task(loop, step, executor)
        loop.plan_index += 1
        loop.stall_count = 0
        loop.last_error = None
        db.session.commit()
        ensure_cloud_executor(loop, executor)
        auto_assign(task, executor)
        return {'advanced': True, 'reason': 'plan_extended', 'task_id': task.id}

    return _register_stall(loop, f"blocked: {decision.get('reason') or '未给出原因'}")


def _register_stall(loop, reason: str) -> dict:
    db.session.expire(loop)
    loop.stall_count = (loop.stall_count or 0) + 1
    loop.last_error = reason[:2000]
    status_changed = False
    if loop.status == GoalLoopStatus.RUNNING and loop.stall_count >= (loop.stall_limit or DEFAULT_STALL_LIMIT):
        loop.status = GoalLoopStatus.STALLED
        loop.finished_at = naive_utc_now()
        status_changed = True
    db.session.commit()
    if status_changed:
        # 记忆沉淀：项目级受阻教训（同类目标重跑时被召回，避免重蹈覆辙）
        from services.memory.loop_hooks import on_loop_blocked
        on_loop_blocked(loop, reason)
    return {
        'advanced': False,
        'reason': 'stalled' if status_changed else 'stall_counted',
        'stall_count': loop.stall_count,
    }


def _finish(loop, status: GoalLoopStatus, last_error: str = None):
    loop.status = status
    if last_error:
        loop.last_error = last_error
    loop.finished_at = naive_utc_now()
    db.session.commit()


def _record_success_experience(loop):
    """循环达成 → success_pattern 经验入库（与失败路径对称的记忆写入）。

    失败经验由 failure_recovery 写入；成功经验此前没有落点，达成策略
    （拆解方式/收敛路径）就此丢失。写入失败只记日志，绝不影响循环终态。
    """
    import structlog

    from models import AgentExperience

    logger = structlog.get_logger()
    try:
        rounds = rounds_done(loop.id)
        db.session.add(AgentExperience(
            agent_id=int(loop.agent_id),
            experience_type='success_pattern',
            task_type='goal_loop',
            strategy=(loop.goal_text or '')[:200],
            outcome_pattern=f"目标达成，共 {rounds} 轮",
            key_learnings=(loop.completion_summary or '目标达成')[:500],
            confidence=0.7,
            source_task_id=loop.last_task_id,
        ))
        db.session.flush()
    except Exception:  # noqa: BLE001 - 经验沉淀绝不影响循环收尾
        logger.warning("goal_loop.success_experience_failed", exc_info=True)
        db.session.rollback()


def create_loop(*, project: Project, agent, title: str, goal_text: str,
                done_definition: str = None, rounds_limit: int = DEFAULT_ROUNDS_LIMIT,
                created_by: int = None, director: Agent = None,
                time_budget_hours: int = None, stall_limit: int = DEFAULT_STALL_LIMIT) -> GoalLoop:
    loop = GoalLoop(
        workspace_id=project.organization_id,
        project_id=project.id,
        agent_id=agent.id,
        director_agent_id=director.id if director else None,
        title=title.strip()[:500],
        goal_text=goal_text,
        done_definition=(done_definition or '').strip() or None,
        status=GoalLoopStatus.RUNNING,
        rounds_limit=clamp_int(rounds_limit, 1, MAX_ROUNDS_LIMIT, DEFAULT_ROUNDS_LIMIT),
        time_budget_hours=clamp_int(time_budget_hours, 1, MAX_TIME_BUDGET_HOURS, None) if time_budget_hours else None,
        stall_limit=clamp_int(stall_limit, 1, 50, DEFAULT_STALL_LIMIT),
        created_by=created_by,
    )
    db.session.add(loop)
    db.session.commit()
    maybe_advance(loop.id)
    return loop


def update_guardrails(loop_id: int, *, rounds_limit=None, time_budget_hours=None,
                      stall_limit=None) -> GoalLoop:
    """调整长跑护栏（用户可设置"跑多久/多少轮才停"）；仅非终态循环可调。

    time_budget_hours 传 0/None 表示清除时长预算（改为不限时，仅受轮数约束）。
    预算以 started_at 为基准绝对计时：延长预算即延长总时长。
    """
    loop = db.session.get(GoalLoop, loop_id)
    if not loop:
        raise LookupError('goal_loop_not_found')
    if loop.status in (GoalLoopStatus.DONE, GoalLoopStatus.STOPPED):
        raise ValueError('goal_loop_terminal')

    if rounds_limit is not None:
        loop.rounds_limit = clamp_int(rounds_limit, 1, MAX_ROUNDS_LIMIT, loop.rounds_limit)
    if time_budget_hours is not None:
        loop.time_budget_hours = (
            clamp_int(time_budget_hours, 1, MAX_TIME_BUDGET_HOURS, None)
            if time_budget_hours else None
        )
    if stall_limit is not None:
        loop.stall_limit = clamp_int(stall_limit, 1, 50, loop.stall_limit)
    db.session.commit()
    return loop


def notify_task_finished(task_id: int):
    """任务终态钩子：任务属于某个循环时推进它。任何路由都可安全调用。"""
    try:
        task = db.session.get(Task, task_id)
        if not task or not task.tags:
            return
        from .query import tag_prefix
        prefix = tag_prefix()
        loop_id = None
        for tag in task.tags:
            if isinstance(tag, str) and tag.startswith(prefix):
                try:
                    loop_id = int(tag[len(prefix):])
                except ValueError:
                    continue
                break
        if loop_id:
            maybe_advance(loop_id, trigger_task_id=task_id)
    except Exception:  # noqa: BLE001
        db.session.rollback()


def set_status(loop_id: int, status: GoalLoopStatus) -> GoalLoop:
    loop = db.session.get(GoalLoop, loop_id)
    if not loop:
        raise LookupError('goal_loop_not_found')
    loop.status = status
    if status in (GoalLoopStatus.PAUSED, GoalLoopStatus.RUNNING):
        # 人工干预重置受阻计数
        loop.stall_count = 0
    if status in (GoalLoopStatus.STOPPED, GoalLoopStatus.DONE):
        loop.finished_at = naive_utc_now()
    db.session.commit()
    return loop
