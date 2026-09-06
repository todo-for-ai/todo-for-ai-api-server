"""GoalLoop 常量与小工具。"""

from datetime import datetime

from models import TaskStatus

TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.CANCELLED}
ACTIVE_TASK_STATUSES = {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED}

DEFAULT_ROUNDS_LIMIT = 10
DEFAULT_STALL_LIMIT = 2
MAX_ROUNDS_LIMIT = 2000
MAX_TIME_BUDGET_HOURS = 24 * 30
DEFAULT_STUCK_TASK_HOURS = 6


def clamp_int(value, lo, hi, default=None):
    """护栏参数钳制；非法输入返回 default（None 表示调用方自行决定）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def naive_utc_now() -> datetime:
    return datetime.utcnow()
