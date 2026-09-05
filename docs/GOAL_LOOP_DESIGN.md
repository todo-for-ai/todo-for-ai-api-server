# GoalLoop 目标循环 —— 产品与技术设计（v1，2026-09-06）

## 解决什么问题

现在 agent 干活依赖「人往任务队列里喂任务」。用户想要的是：**给定一个目标，agent 自己循环推进直到达成**，人只定目标、看产出。

平台已有零件：runtime worker 常驻轮询（`agent-runtime` `_task_poll_loop`）、任务创建即自动派发（`auto_assign_task` + WebSocket 推送）、MCP 全套自我供料工具（含 `create_task`，且 agent 建的后续任务同样即时回派）。缺的是：目标不是循环的驱动源、没有「队列空了→从目标推导下一步」的平台侧驱动器、没有护栏。

## 产品设计

一次 GoalLoop = 「某 agent 在某项目上朝一个目标循环做任务，直到宣告完成或触发护栏停止」。

- **创建**：项目详情页（治理 Tab）「目标循环」卡片 → 新建：标题 / 目标描述 / 完成标准 / 轮数上限（默认 10）/ 执行 agent（默认该工作区第一个活跃 agent）。权限 = 项目 can_manage_project。
- **运行**：每轮 = 一个普通 AI 任务（挂在项目上，带 `goal-loop:{id}` 标签与 loop 外键）。任务由平台自动派发给绑定的 agent；任务到达终态后驱动器自动推进下一轮。
- **推进决策**（规划器）：输入目标 + 完成标准 + 最近轮次历史（标题/状态/失败原因），输出三选一：
  - `continue`：给出下一轮任务标题+内容 → 建任务并派发
  - `complete`：宣告目标达成（附总结），循环收尾
  - `blocked`：本轮无法推进（信息不足等）
- **护栏**：轮数上限（达到 → `limit_reached`）；连续 blocked/失败 ≥ 容忍次数（默认 2）→ `stalled`；人工随时暂停/继续/停止；LLM 不可用不会伪装推进，只会 stalled 并记录原因。
- **状态机**：`running → paused（人工）→ running；running → done（规划器宣告）/ limit_reached（轮数耗尽）/ stalled（连续受阻）/ stopped（人工）`。

## 技术设计

- **模型** `goal_loops`（迁移 000017）：project_id（必填，agent 平面按组织过滤所以必须有项目）、agent_id、workspace_id、title、goal_text、done_definition、status、rounds_limit、stall_limit、last_error、last_task_id、created_by。任务侧不加列：用 `tags` 里的 `goal-loop:{id}` + `Task.title` 前缀关联即可反查轮次列表（避免动 tasks 表）。
  ——修正：tags 是 JSON 数组字符串匹配太脆，v1 直接加 `task.goal_loop_id` 列？否，避免第三张表迁移。用 `AgentTaskEvent`? 最简单可靠：Task 已有 JSON `tags`，查询用 `Task.tags LIKE '%goal-loop:<id>%'`（SQLite/MySQL 皆可，量级=单 loop 轮数，可接受）。
- **驱动器** `services/goal_loop_service.py`：`create_loop` / `maybe_advance(loop_id)` / `notify_task_finished(task_id)` / `pause|resume|stop|kick`。并发防护：推进前对 loop 行 `SELECT ... FOR UPDATE`（MySQL）/ 事务内重读状态，保证同一循环不双发任务。
- **规划器可插拔**：`_call_planner()` 默认走平台 LLM（`call_llm_production(feature='goal_loop')`，与 AI 拆分同通道）；环境变量 `GOAL_LOOP_PLANNER=scripted` 启用脚本规划器（确定性：第 N 轮生成固定任务、达到 scripted_done_round 宣告完成）——**仅供测试/E2E**，生产默认 llm。
- **触发点**：① 创建循环时；② 循环任务到终态：`agent_runtime_commit` 提交成功、`api/tasks` 状态更新、MCP `update_task_status` 三处各挂一行 `notify_task_finished(task_id)`（内部查任务是否挂 loop，未挂零开销）；③ `POST /goal-loops/{id}/kick` 手动兜底。
- **API**：`GET/POST /projects/{pid}/goal-loops`、`POST /goal-loops/{id}/pause|resume|stop|kick`、`GET /goal-loops/{id}`（含轮次任务列表）。
- **前端**：项目详情页治理 Tab 增「目标循环」卡片（列表 + 状态徽标 + 轮数 + 新建/暂停/继续/停止/立即推进）。

## 已知边界（v1）

- 真实 LLM 规划依赖管理员配置有效 LLM（当前本地 key 401 失效，模型名已修复为 LongCat-2.0；换有效 key 后无需改码）。
- agent 宕机导致任务租约过期的场景，由既有租约机制处理；v1 循环触发不依赖定时器，遗漏触发可用 kick 兜底。
