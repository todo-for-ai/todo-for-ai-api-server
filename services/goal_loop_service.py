"""
GoalLoop 目标循环驱动器（兼容门面）

实现已按内聚职责拆分到 `services/goal_loop/` 包：
- planning      规划器（LLM/scripted 拆解与评审）
- dispatch      执行者路由与派发（含云端联动）
- state_machine 推进状态机与护栏
- watchdog      多日续航看门狗
- query         循环任务查询助手
- constants     常量与小工具

本模块仅做再导出（含历史私有名别名），保持
`services.goal_loop_service` 既有导入路径不变。
新代码请直接从 `services.goal_loop.*` 导入。
"""

from services.goal_loop import constants as _c
from services.goal_loop import dispatch as _d
from services.goal_loop import planning as _p
from services.goal_loop import query as _q
from services.goal_loop import state_machine as _sm
from services.goal_loop import watchdog as _w

# ── 常量 ──
ACTIVE_TASK_STATUSES = _c.ACTIVE_TASK_STATUSES
TERMINAL_TASK_STATUSES = _c.TERMINAL_TASK_STATUSES
DEFAULT_ROUNDS_LIMIT = _c.DEFAULT_ROUNDS_LIMIT
DEFAULT_STALL_LIMIT = _c.DEFAULT_STALL_LIMIT
MAX_ROUNDS_LIMIT = _c.MAX_ROUNDS_LIMIT
MAX_TIME_BUDGET_HOURS = _c.MAX_TIME_BUDGET_HOURS
DEFAULT_STUCK_TASK_HOURS = _c.DEFAULT_STUCK_TASK_HOURS

# ── 公共 API（既有调用方使用）──
loop_tasks = _q.loop_tasks
rounds_done = _q.rounds_done
maybe_advance = _sm.maybe_advance
notify_task_finished = _sm.notify_task_finished
create_loop = _sm.create_loop
update_guardrails = _sm.update_guardrails
set_status = _sm.set_status
watchdog_sweep = _w.watchdog_sweep

# ── 历史私有名别名（测试与既有内部引用）──
_clamp_int = _c.clamp_int
_naive_utc_now = _c.naive_utc_now
_tag_prefix = _q.tag_prefix
_loop_task_query = _q.loop_task_query
_recent_history = _q.recent_history

_role_context = _d.role_context
_director = _d.director
_executor_pool = _d.executor_pool
_available_executor_roles = _d.available_executor_roles
_pick_executor = _d.pick_executor
_create_round_task = _d.create_round_task
_assign_task_to_agent = _d.assign_task_to_agent
_ensure_cloud_executor = _d.ensure_cloud_executor
_auto_assign = _d.auto_assign

_planner_mode = _p.planner_mode
_extract_json = _p.extract_json
_valid_steps = _p.valid_steps
_llm_call = _p.llm_call
_budget_line = _p.budget_line
_decompose = _p.decompose
_review = _p.review
_scripted_decompose = _p.scripted_decompose
_scripted_review = _p.scripted_review
_call_decompose = _p.call_decompose
_call_review = _p.call_review

_time_budget_exceeded = _sm.time_budget_exceeded
_advance_locked = _sm._advance_locked
_register_stall = _sm._register_stall
_finish = _sm._finish

_stuck_task_hours = _w._stuck_task_hours
_abandon_task_runtime = _w._abandon_task_runtime
