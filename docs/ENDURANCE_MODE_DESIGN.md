# 长跑模式（Endurance Mode）设计 —— 让 Agent 朝目标持续迭代数天，而不是跑几分钟就停

v1，2026-09-13。
配套实现：`fix/endurance-premature-stop`（本仓）、`fix/lease-renewal-resilience`（agent-runtime）。

## 1. 问题定义

平台定位是 7×24 在线的多 Agent 协作云平台，核心卖点应当是**以时间/轮次维度约束目标**
（迭代 100 个版本、连续跑 3 天/一周），而不是像客户端交互式工具那样跑几分钟就停。
但实际运行中 agent 频繁出现「目标未达成就停下、交付质量差」。根因盘点（按严重度排序）：

| # | 根因 | 位置（修复前） | 后果 |
|---|------|----------------|------|
| 1 | **循环任务 failed 提交被置 REVIEW（活跃态）** | `api/agent_runtime_commit.py` failed 分支 | GoalLoop 状态机把 REVIEW 视为活跃任务永远等待 → 一轮失败循环就挂死在人手里 |
| 2 | **续约循环首次异常即放弃** | agent-runtime `task_executor._lease_renewal_loop` | 一次网络抖动 → 租约到期 → commit 被 LEASE_EXPIRED 拒绝 → 数小时工作成果作废 |
| 3 | **租约 TTL 60s 硬编码 ×4 处** | pull/renew、goal_loop dispatch、auto_assign | 续约节奏稍一抖动就过期；与 `AgentRuntimeConfig.lease_duration_seconds=120` 的既有配置互相矛盾 |
| 4 | **护栏默认值偏保守且不可配** | `services/goal_loop/constants.py` | `rounds_limit=10`、`stall_limit=2`：默认 2 次连续受阻循环就 STALLED；想跑 100 轮/多天只能逐循环手调 |
| 5 | **失败自愈封顶 2 次不可配** | `services/failure_recovery.py` | 长跑场景 2 次修复机会太少，之后直接升级人工停下 |
| 6 | 轮次间无上下文延续 | agent-runtime：每轮新 clone、工作区销毁 | 后轮不知前轮干过什么 → 重复劳动、质量差（R3，见 §6） |
| 7 | 看门狗等后台守护默认关闭 | `GOAL_LOOP_WATCHDOG_ENABLED` 等 | agent 崩溃后循环冻结（无人踢进下一轮）或卡死 6h 才被清 |

## 2. 设计原则

1. **停不停由护栏决定，不由失败决定**：失败是循环的输入（喂给规划器重规划），不是循环的终止条件。
   终止只来自三类信号：护栏耗尽（轮数/时长）、规划器宣告完成、人工停止。
2. **时间/轮次是一等约束**：token 预算管成本，轮数/时长管「跑多久」；两者解耦，
   长跑场景放宽 `FAILURE_REPAIR_MAX_ATTEMPTS`、抬高 `GOAL_LOOP_DEFAULT_*`，预算门保持原语义。
3. **瞬时故障必须自愈**：租约、续约、派发等链路上的单点网络抖动不允许产生不可逆的成果丢失。
4. 所有默认值行为向后兼容：不设环境变量时，语义变化仅限「循环不再被失败挂死」（这是 bug fix 而非行为变更）。

## 3. 已实现（本轮）

### 3.1 循环任务失败 → 终态 + 规划器重规划（治根因 #1）

- `api/agent_runtime_commit.py`：failed 提交时先反查任务是否属于活跃（running/paused）GoalLoop
  （tags `goal-loop:{id}`，`services/goal_loop/query.py::active_loop_id_for_task`）：
  - 循环任务 → `task.status = CANCELLED`（终态），自愈通道关闭
    （`handle_failed_commit(..., auto_repair=False)` 只做归因/经验沉淀，返回 `loop_replan`），
    `maybe_advance` 由 `notify_task_finished` 触发 → 规划器评审：
    LLM 评审可 `extend` 追加剩余计划继续跑，或 `blocked` 计 stall；连续失败到 `stall_limit` 循环
    STALLED（护栏兜底，不会无限烧 token）。
  - 非循环任务 → 维持原语义（REVIEW + 修复子任务回流 + 封顶升级人工）。
- 评审上下文增强：`recent_history` 对 cancelled 轮附带最近一次失败归因
  （`failure_code: failure_reason`，截断 300 字符），规划器才能对症重规划。

### 3.2 租约 TTL 统一走配置（治根因 #3）

- 新增 `services/lease_policy.py::effective_lease_ttl(agent_id, workspace_id)`：
  Agent 激活运行时配置 `lease_duration_seconds` > 环境变量 `LEASE_DURATION_SECONDS` > 默认 120s，
  钳制 [60, 3600]；读取失败回落环境默认，绝不阻断派发。
- 接线四处：pull 建租约/续约（`api/agent_runtime_pull.py`）、GoalLoop 派发
  （`services/goal_loop/dispatch.py`）、auto_assign（`services/agent_runtime_controller.py`）。

### 3.3 续约容错（治根因 #2，agent-runtime）

- `_lease_renewal_loop`：单次失败不再 `break`，改为退避重试（2^n 秒封顶 10s）；
  连续失败 ≥ `LEASE_MAX_CONSECUTIVE_FAILURES=4`（≈ 2 倍租约时长的容错窗口，
  覆盖平台重启/短暂不可达）才放弃并打 `task.lease_renewal_abandoned`；
  成功即复位计数器。语义上：只有租约在服务端确定已死时才放弃。

### 3.4 部署级护栏缺省可调（治根因 #4/#5）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GOAL_LOOP_DEFAULT_ROUNDS_LIMIT` | 10 | 新建循环缺省轮数上限（上限 2000 不变） |
| `GOAL_LOOP_DEFAULT_STALL_LIMIT` | 2 | 新建循环缺省连续受阻容忍 |
| `GOAL_LOOP_STUCK_TASK_HOURS` | 6 | 看门狗判定卡死的轮次空闲时长 |
| `FAILURE_REPAIR_MAX_ATTEMPTS` | 2 | 失败自愈修复子任务封顶（1..10） |
| `LEASE_DURATION_SECONDS` | 120 | 租约 TTL 兜底（Agent 配置优先） |

创建/调整循环 API 未传护栏字段时改用上述缺省（`routes_goal_loops.py`、`state_machine.create_loop`）。

### 3.5 测试

- api-server：`tests/unit/services/test_lease_policy.py`（9 用例：优先级/钳制/失效回落）、
  `tests/unit/api/test_endurance_loop_commit.py`（6 用例：循环失败取消+stall 计数、
  连续两轮失败→STALLED、非循环任务回归保护、反查助手、评审上下文带失败归因）。
- agent-runtime：`tests/unit/test_task_executor.py::TestLeaseRenewalLoop`
  （4 用例：瞬时失败重试、连续失败放弃、成功复位计数、任务不在运行表即退出）。

## 4. 长跑部署配方（推荐）

```bash
# api-server
GOAL_LOOP_WATCHDOG_ENABLED=true        # 看门狗：卡死轮回收 + 漏触发自愈（多日续航必需）
ORCHESTRATOR_ENABLED=true              # 全局编排巡检（过期租约/逾期升级）
AGENT_CRON_SCHEDULER_ENABLED=true      # cron 触发器/定时建任务
GOAL_LOOP_DEFAULT_ROUNDS_LIMIT=100     # 「迭代 100 个版本」
GOAL_LOOP_DEFAULT_STALL_LIMIT=5        # 长跑容忍更多次受阻
FAILURE_REPAIR_MAX_ATTEMPTS=4

# agent-runtime
LEASE_DURATION_SECONDS=300             # 更长租约，续约窗口更宽松
```

「跑 3 天/一周」直接用既有 `time_budget_hours`（1..720h，可中途
`PUT /projects/goal-loops/<id>` 调整续期）；「迭代 N 个版本」用 `rounds_limit=N`。

## 5. 效果推演

- 单轮失败：不再终止循环。规划器看到 `cancelled + failure 归因` → 换思路 extend 下一轮；
  scripted 规划器（测试/无 LLM 部署）下连续 2 次（或 env 调整后 N 次）失败才 STALLED。
- agent 崩溃/网络断：租约续约层容忍 ~2 分钟抖动；真的死了，watchdog 按
  `GOAL_LOOP_STUCK_TASK_HOURS` 回收并踢进下一轮（而非冻结）。
- 成果保护：租约 TTL 从 60s 提到 120s+ 后，commit 被 LEASE_EXPIRED 拒绝的概率大幅下降；
  续约退避重试兜住平台滚动重启窗口。

## 6. 后续路线（按杠杆排序）

1. **R2 轮次上下文延续**（治根因 #6，质量关键）：**服务侧已于 v3 落地（见 §9）**——
   循环上下文走廊（目标+压缩摘要+近轮明细）注入每个轮次任务。剩余：workspace 级
   `PROGRESS.md`/检查点 ref 跨项目持久化。
2. **R3 turn 级续跑**：agent-runtime 已落地 turn 级 git checkpoint
   （`src/sandbox/turn_checkpoints.py`，尚未接线）。给 executor 增加「单任务多 turn」循环 +
   checkpoint 恢复 + 会话 resume 锚点（claude `--resume`），单任务内部就能跑数小时，
   与 GoalLoop 多轮形成两层长跑。
3. **R4 质量闭环硬化**：DoD 证据门已有；把 reviewer 角色评审（`review_gate.py`）作为
   GoalLoop 收尾轮的强制步骤（done_definition 需 reviewer 证据背书），规划器评审与
   独立 reviewer 分离，避免「自己给自己验收」。
4. **R5 budget 门与审批解耦**：`budget_exceeded` 审批通过后自动临时上调额度，
   避免审批死锁（当前审批不解除派发门）。
5. **R6 token 级护栏传参**：CLI argv 透传 `--max-turns`/预算 flag（引擎注册表按引擎能力映射），
   把「不考虑 token 成本只看时间」做成 per-loop 的显式选项而非隐式缺省。

## 7. 风险与兼容

- 非循环任务路径零变化（回归用例 `test_non_loop_failed_commit_keeps_review_and_repair`）。
- 循环任务失败不再产生修复子任务：修复能力由循环重规划承担；依赖修复子任务的审计请看
  `AgentTaskEvent.failure` 归因与 `AgentExperience.failure_pattern`。
- 护栏 env 只影响新建循环的缺省值，存量循环不受影响；终态循环仍需 resume。

## 8. v2（2026-09-13，fix/graceful-quota-and-loop-exits）：优雅停车两件套

长跑的对偶问题是「停不下来」和「没油了还在空转」。

### 8.1 无进展护栏（死循环必须有退出点）

用户可以要"死循环"（大 rounds_limit / 长时预算），但循环不能没有体面的退出方式：

- `services/goal_loop/query.py::trailing_failure_streak`：末尾连续失败（CANCELLED）轮数。
- 状态机 extend 分支：连续失败轮数 ≥ `GOAL_LOOP_NO_PROGRESS_LIMIT`（默认 3，env 可调）时
  **拒绝 extend**，强制计 stall（理由 `no_progress: 连续 N 轮失败`）→ 连续两次即 STALLED 终态。
  **规划器宣告 complete 仍然允许**——验收轮失败但目标已达成是合法结局。
- 语义：失败可以喂给规划器换思路，但换思路也要有尽头；stall_limit 兜底保证最终停车，
  用户调整目标/修复问题后 resume 即可继续。

### 8.2 额度熔断（LLM API token 额度/计费耗尽）

最常见的资源级故障，之前被当成普通失败反复重试（修复子任务×2 → 升级），白烧重试还不上报。
现在（`services/quota_guard.py` + `failure_recovery`）：

1. **识别**：新增归因类别 `quota_exhausted`（failure_code：QUOTA_EXCEEDED/INSUFFICIENT_QUOTA/
   INSUFFICIENT_CREDITS/BILLING_ERROR/PAYMENT_REQUIRED；reason 关键词：insufficient_quota/
   quota exceeded/credit balance/billing/payment required/usage limit）。普通 429 限流仍是
   可重试的 transient，两者区分。
2. **不可重试**：`NON_RETRYABLE_CATEGORIES` 直接走升级，跳过修复子任务与重试封顶计数。
3. **熔断**：`has_pending_quota_block(agent_id)` 在三道派发门生效（pull 返回
   `quota_block`、auto_assign 跳过该候选、goal_loop dispatch 跳过），窗口
   `QUOTA_BLOCK_WINDOW_HOURS`（默认 24h，env 可调）内不再给该 Agent 派发；
   窗口过后自动恢复（换 key/充值即自愈）。
4. **上报**：写 `interaction_request` 事件（`interaction_type=token_quota_exhausted`，
   payload 带 hint「请充值或更换 key」），审批队列/open 协议可见；一窗口一 Agent 幂等只报一次。
5. **循环停车**：循环任务的额度耗尽失败 → 循环立即 STALLED（last_error 写明
   「LLM API 额度/计费已耗尽」），不进规划器重规划——没额度时重规划只是空转。

### 8.3 测试

`tests/unit/api/test_graceful_quota.py`（11 用例）：归因码/关键词、额度失败升级不生成修复子任务、
上报幂等、窗口过期自愈、pull 熔断门、循环任务额度失败强制 STALLED、
trailing_failure_streak、extend 拒绝/complete 放行。

## 9. v3（2026-09-13，feat/loop-context-compression）：循环上下文走廊与自动压缩

问题：轮次任务逐轮物化，但执行者每轮「失忆」——不知道前几轮干了什么、失败过什么；
全量塞历史又随轮数无限膨胀，烧 token 且稀释注意力。

### 9.1 三层走廊（`services/goal_loop/context.py::build_corridor`）

注入每个轮次任务内容顶部（`create_round_task`），pull/push 两条派发路径自动携带：

1. **目标层**：goal_text + done_definition（每轮必带，防长跑跑偏）；
2. **压缩层**：`goal_loops.context_digest`——早期轮次的滚动压缩摘要；
3. **明细层**：最近 `GOAL_LOOP_CONTEXT_RECENT_ROUNDS`（默认 3）轮保留标题/状态/失败归因。

无历史轮次的首轮不注入（零开销）。走廊整体有硬性上界
`CONTEXT_MAX_CORRIDOR_CHARS=6000`，超限先裁明细、再硬截——注入 prompt 的
上下文规模有确定性上界（自动清理的确定性保证）。

### 9.2 滚动压缩（`compress_digest` / `maybe_compress`）

- **增量**：只压缩 `context_digest_upto` 之后、将滑出明细层的终态轮次，摘要滚动向前；
- **节奏**：每累积 `GOAL_LOOP_COMPRESS_EVERY`（默认 3）个新终态轮才刷一次，
  控制压缩本身的 LLM 开销；状态机在物化下一轮前调用，新轮次拿到最新记忆；
- **双引擎**：LLM 可用走语义压缩（保留产出/结论/失败原因/教训，输出 JSON digest），
  失败或无 key 自动降级抽取式（紧凑清单）——长跑记忆不因 LLM 故障断档；
- **幂等**：压缩后推进 `context_digest_upto`，重复调用零副作用；
- 任何压缩异常只记日志，绝不阻断循环推进。

### 9.3 存储

迁移 000025：`goal_loops` 增加 `context_digest`（TEXT）/`context_digest_upto`（INT）；
API `to_dict` 暴露 `has_context_digest` 布尔（不回传全文，保持载荷轻）。

### 9.4 测试

`tests/unit/services/test_goal_loop_context.py`（12 用例）：走廊三层/空历史零开销/
硬上界截断、压缩增量+幂等、LLM 语义压缩与降级、节奏控制、异常不阻断、
create_round_task 注入与首轮豁免。迁移脚本 SQLite/MySQL 双方言 + 幂等冒烟。
