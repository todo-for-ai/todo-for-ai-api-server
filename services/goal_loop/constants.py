"""GoalLoop 常量与小工具。"""

import os
from datetime import datetime

from models import TaskStatus

TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.CANCELLED}
ACTIVE_TASK_STATUSES = {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED}

MAX_ROUNDS_LIMIT = 2000
MAX_TIME_BUDGET_HOURS = 24 * 30

# 目标链式接续的单次触发递归深度上限：A 终态→唤醒 B→B 若立刻终态→唤醒 C…
# 超限的后继保持 RUNNING，由看门狗漏触发自愈兜底推进（幂等）
MAX_CHAIN_DEPTH = 32


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
# 无进展护栏：用户要"死循环"也必须有退出点——规划器连续看到 N 轮失败后
# 不允许再 extend（宣告 complete 仍允许），强制计 stall 走 STALLED 退出
DEFAULT_NO_PROGRESS_ROUNDS = _env_int('GOAL_LOOP_NO_PROGRESS_LIMIT', 3, 2, 50)

# 规划器瞬时故障容忍：LLM 调用层失败（网络/超时/5xx/限流）按指数退避重试，
# 不烧语义受阻预算（stall_limit 只有 2，一次 10 分钟的供应商抖动就把全平台
# 循环打成 STALLED 是长跑大忌）。容忍上限内退避自愈，超限才回落计 stall。
DEFAULT_PLANNER_TRANSIENT_LIMIT = _env_int('GOAL_LOOP_PLANNER_TRANSIENT_LIMIT', 12, 2, 200)

_PLANNER_BACKOFF_BASE_SECONDS = 300   # 与 watchdog 默认巡检间隔对齐
_PLANNER_BACKOFF_CAP_SECONDS = 3600   # 退避上限 1 小时


def planner_backoff_seconds(streak: int) -> int:
    """瞬时故障退避时长：5min → 10min → 20min → 40min → 封顶 1h。"""
    return min(_PLANNER_BACKOFF_BASE_SECONDS * (2 ** max(0, streak - 1)),
               _PLANNER_BACKOFF_CAP_SECONDS)


def naive_utc_now() -> datetime:
    return datetime.utcnow()
