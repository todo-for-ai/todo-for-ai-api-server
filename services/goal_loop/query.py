"""循环任务查询助手（tags 反查，量级 = 单循环轮数）。"""

from sqlalchemy import cast, String

from models import Task


def tag_prefix() -> str:
    from models.goal_loop import GOAL_LOOP_TAG_PREFIX
    return GOAL_LOOP_TAG_PREFIX


def loop_task_query(loop_id):
    tag = f'{tag_prefix()}{loop_id}'
    return Task.query.filter(
        Task.project_id.isnot(None),
        cast(Task.tags, String).like(f'%{tag}%'),
    )


def loop_tasks(loop_id):
    """循环的全部轮次任务（按创建顺序）。"""
    return loop_task_query(loop_id).order_by(Task.id).all()


def rounds_done(loop_id) -> int:
    return loop_task_query(loop_id).count()


def trailing_failure_streak(loop_id) -> int:
    """末尾连续失败（CANCELLED）轮数；遇到 DONE 清零。

    无进展护栏的输入：规划器可以不断 extend，但连续失败轮数只增不减，
    达到阈值后状态机拒绝 extend，保证「死循环」也有退出点。
    """
    from models import TaskStatus

    streak = 0
    for t in reversed(loop_tasks(loop_id)):
        if t.status == TaskStatus.CANCELLED:
            streak += 1
        else:
            break
    return streak


def active_loop_id_for_task(task):
    """任务 tags 反查其所属且未终态（running/paused）的循环 ID；无则 None。

    供 commit 失败路径判断「该任务由目标循环驱动」——循环任务的失败
    应交给循环规划器重规划（任务置终态推进下一轮），而不是进 REVIEW
    等人处置把循环挂死。
    """
    from models import GoalLoop, GoalLoopStatus, db

    for tag in (task.tags or []):
        s = str(tag)
        if not s.startswith(tag_prefix()):
            continue
        try:
            loop_id = int(s[len(tag_prefix()):])
        except ValueError:
            continue
        loop = db.session.get(GoalLoop, loop_id)
        if loop and loop.status in (GoalLoopStatus.RUNNING, GoalLoopStatus.PAUSED):
            return loop_id
    return None


def _last_failure_reason(task_id: int) -> str:
    """该任务最近一次失败 attempt 的归因摘要（供评审器重规划参考）。"""
    from models import AgentTaskAttempt, AgentTaskAttemptState

    attempt = (
        AgentTaskAttempt.query
        .filter_by(task_id=task_id, state=AgentTaskAttemptState.ABORTED)
        .order_by(AgentTaskAttempt.id.desc())
        .first()
    )
    if not attempt:
        return ''
    code = (attempt.failure_code or '').strip()
    reason = (attempt.failure_reason or '').strip()
    return f'{code}: {reason}'.strip(': ').strip()[:300]


def recent_history(loop, limit=5):
    rows = loop_tasks(loop.id)[-limit:]
    history = []
    for t in rows:
        row = {
            'title': t.title,
            'status': t.status.value if t.status else None,
        }
        if t.status and t.status.name == 'CANCELLED':
            failure = _last_failure_reason(t.id)
            if failure:
                row['failure'] = failure
        history.append(row)
    return history

