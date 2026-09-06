"""GoalLoop 目标循环 — 内聚分包

- constants      : 常量与小工具（钳制、时间）
- query          : 循环任务的查询助手
- planning       : 规划器（LLM/scripted 拆解与评审、提示词）
- dispatch       : 执行者路由与任务派发（含云端联动）
- state_machine  : 推进状态机与护栏（maybe_advance/limit/stall）
- watchdog       : 多日续航看门狗巡检

`services/goal_loop_service.py` 是兼容门面，保持既有导入路径不变。
"""
