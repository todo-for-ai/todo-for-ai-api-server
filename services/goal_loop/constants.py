"""GoalLoop 常量与小工具。"""

import os
from datetime import datetime

from models import TaskStatus

TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.CANCELLED}
ACTIVE_TASK_STATUSES = {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED}

MAX_ROUNDS_LIMIT = 2000
MAX_TIME_BUDGET_HOURS = 24 * 30


def clamp_int(value, lo, hi, default=None):
    """护栏参数钳制；非法输入返回 default（None 表示调用方自行决定）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _env_int(name, default, lo, hi):
    """部署级默认值：环境变量可覆盖（云平台长跑场景常需要 100 轮/多天）。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    value = clamp_int(raw, lo, hi)
    return default if value is None else value


# 新建循环的护栏缺省值（创建时可逐循环覆盖；env 可调部署级默认）
DEFAULT_ROUNDS_LIMIT = _env_int('GOAL_LOOP_DEFAULT_ROUNDS_LIMIT', 10, 1, MAX_ROUNDS_LIMIT)
DEFAULT_STALL_LIMIT = _env_int('GOAL_LOOP_DEFAULT_STALL_LIMIT', 2, 1, 50)
DEFAULT_STUCK_TASK_HOURS = _env_int('GOAL_LOOP_STUCK_TASK_HOURS', 6, 1, 24 * 7)


def naive_utc_now() -> datetime:
    return datetime.utcnow()
