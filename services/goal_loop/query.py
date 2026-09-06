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


def recent_history(loop, limit=5):
    rows = loop_tasks(loop.id)[-limit:]
    return [
        {
            'title': t.title,
            'status': t.status.value if t.status else None,
        }
        for t in rows
    ]
