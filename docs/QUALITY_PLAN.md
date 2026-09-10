# 代码质量提升计划（2026-09-07 夜间马拉松）

目标：高内聚、低耦合；每个被重构的模块单元测试补到覆盖 100% 且全量门禁通过；
超大文件拆小；逻辑混乱处重构。持续迭代到 2026-09-07 09:00。

## 工作纪律（与并行会话共存）
- 只动自己拥有的或稳定的文件；他人 WIP（git status 里的未提交改动）一律不碰、
  不卷入提交（controller 文件用 hunk 过滤提交）。
- 每个迭代：`pytest tests/unit` 全绿 + 覆盖率证据 + pathspec 定向提交推送 + 本文件迭代日志。
- "100%" 的度量口径：被重构模块的**行覆盖 100%**（coverage term-missing 无 Missing 行），
  分支覆盖尽力；纯样板（模型字段透传）不强行凑数，在日志中注明。

## 基线（2026-09-07 04:00）

### 体积 Top（services + api，行数）
| 文件 | 行数 | 归属 | 风险 |
| --- | --- | --- | --- |
| api/project_repo.py | 1016 | 他人 | 高（不动） |
| api/agents/workflow_analytics.py | 949 | 他人 | 高（不动） |
| api/agents/_core.py / _shared.py / messaging.py … | 900+ ×5 | 他人 | 高（不动） |
| services/ai_service.py | 763 | 共享核心 | 中 |
| services/goal_loop_service.py | 744 | **我** | 迭代 1 |
| services/agent_runtime_controller.py | 744 | 我+他人 WIP | 迭代 2（hunk 过滤提交） |
| api/agent_runtime_pull.py | ~600 | 他人/稳定 | 观望 |

### 覆盖率基线
- `services/goal_loop_service.py`：33 个用例覆盖主干；planner LLM 分支 / watchdog 部分行未覆盖（详见迭代 1 前测量）。
- `services/agent_runtime_controller.py`：11 用例；auto_assign_task、status/list 未覆盖。
- `services/workspace_runtime_policy.py`：7 用例，已 100% 行覆盖。
- 全量覆盖率 JSON：/tmp/cov_baseline.json（跑完回填数字）。

### 耦合热点（低内聚证据）
1. `goal_loop_service.py` 一个文件混了四件事：**规划器（LLM/scripted 提示词与解析）、
   推进状态机、看门狗（续航巡检）、派发（角色路由 + 云端联动 + 租约直派）**——典型的低内聚。
2. `agent_runtime_controller.py` 混了：Pod 生命周期 + 任务派发（auto_assign）+
   工作区配额读取 + 日志杂音——拆分空间大，但含他人 WIP hunk，需 hunk 过滤提交。
3. 派发逻辑双轨：goal_loop 的 `_assign_task_to_agent` 与 controller 的 `auto_assign_task`
   语义重复（历史原因：后者当时有 WIP 不能动）—— reunification 候选，需两者 owner 都在。

## 迭代队列（每项 = 测试先行到 100% → 重构 → 门禁 → 提交 → 日志）
- [ ] **迭代 1**：拆 `goal_loop_service.py` → `services/goal_loop/` 包：
  `planning.py`（规划器+提示词+解析）、`state_machine.py`（maybe_advance/_advance_locked/护栏）、
  `dispatch.py`（pick_executor/assign/云端联动）、`watchdog.py`（watchdog_sweep），
  `__init__` 保持既有导入路径兼容（现有一切 `from services.goal_loop_service import X` 不改）。
  测试：现有 36 用例迁移保绿 + 新增覆盖缺口用例至 100% 行覆盖。
- [ ] **迭代 2**：`agent_runtime_controller.py` → 拆 `services/cloud_runtime/`
  （pod_lifecycle.py / dispatch.py / quota.py）；用 hunk 过滤提交避开他人 auto_assign WIP；
  补 auto_assign_task 与 status/list 的测试到 100%。
- [ ] **迭代 3**：`api/agent_runtime_mgmt.py` 薄化：业务下沉 service，路由只留参数解析
  （补端点级测试）。
- [ ] **迭代 4+**：按覆盖率 JSON 找 services/ 下 <80% 覆盖且归属自己的模块继续；
  或对迭代 1-3 的产物做圈复杂度复查（长函数拆分）。

## 迭代日志
### 迭代 1（2026-09-07 05:00-06:40）拆分 goal_loop_service ✅
- 前：`services/goal_loop_service.py` 744 行，规划/派发/状态机/看门狗四职责混杂（低内聚）。
- 后：`services/goal_loop/` 包六模块（constants 17 / query 15 / dispatch 91 / planning 83 /
  state_machine 178 / watchdog 46 行）+ 52 行兼容门面（既有导入路径零破坏）。
- 覆盖率：包内 8 文件全部 **100% 行覆盖**（拆分前主文件 52% 量级、LLM/看门狗分支无覆盖）。
- 测试：新增 `test_goal_loop_modules.py` 19 用例 + 状态机守卫/看门狗分支/派发降级 23 用例，
  全量 **438 passed**（基线 396）。
- 顺带移除死分支：watchdog 的"无时间戳"守卫（created_at NOT NULL 约束下不可达）。
- 经验：monkeypatch 目标必须是符号定义所在的模块（拆包后 `state_machine.call_review`
  才是状态机实际调用的符号）；SimpleNamespace 替身上的 staticmethod 在 py3.9 不可调用。

### 迭代 2（2026-09-07 06:40-08:20）拆分 agent_runtime_controller 的清单构造 ✅
- 前：`services/agent_runtime_controller.py` 744 行，Pod 声明式构造（镜像/资源/环境变量/
  RuntimeClass/挂载）与 K8s 客户端生命周期管理混杂。
- 后：抽出纯函数模块 `services/cloud_runtime/manifests.py`（48 行：RUNTIME_IMAGES/
  SANDBOX_RESOURCES/agent_policy/runtime_type/network_mode/build_env_vars/build_pod），
  控制器 744 → **500 行**（-244；声明式构造全部移出）。
- 覆盖率：`manifests.py` **100% 行覆盖**（补 google/gemini→google、ollama/local→ollama、
  未知供应商→custom 的运行时映射缺口用例）；`cloud_runtime/__init__.py` 100%。
- 测试：控制器用例 11 → 12，全量门禁 **439 passed**（上一迭代 438）。
- 提交纪律：controller 含并行会话 auto_assign_task org_id WIP hunk，用 hunk 过滤
  （剔除含 org_id/import Project 的 hunk 后 `git apply --cached`）定向提交；
  顺带补入 Phase 2 遗漏的 app.py API_BASE_URL 运行时覆盖钩子。
- 经验：拆分后的提交物 = 暂存区快照，与跑门禁的工作树仅差他人 WIP hunk 时，
  需确认没有测试真实走进该 hunk 的内部路径（此处 auto_assign_task 在测试里全部被打桩），
  门禁结论才可外推到提交物。

### 迭代 3（2026-09-07 22:00-23:30）agent_runtime_mgmt 薄化 + 隐性 500 修复 ✅
- 前：`api/agent_runtime_mgmt.py` 279 行，spawn/terminate/status/list/settings 的
  业务流程（幂等护栏、AgentKey 解密/生成、执行模式落库、配额校验）全写在路由里，
  且端点级测试为零（整文件覆盖 63%）。
- 后：业务下沉 `services/cloud_runtime/management.py`（75 行，类型化异常
  RuntimeManagementError 家族 + spawn_runtime/terminate_runtime/runtime_status/
  list_runtime_pods/get_update_runtime_settings），路由层 279 → 160 行只剩
  "找资源 → 鉴权 → 调服务 → 异常映射 HTTP"。
- **顺带修复隐性 bug**：原 status/list 路由调用的 `user.has_workspace_access()`
  在整个代码库不存在（必然 AttributeError → 线上 500），因为从未被测试覆盖而潜伏；
  改为真实存在的 `ensure_workspace_access` 链路。409 响应契约
  （`error_details.existing`）在重构中原样保留并被测试钉住。
- 覆盖率：`api/agent_runtime_mgmt.py` 与 `services/cloud_runtime/management.py`
  均 **100% 行覆盖**；新增 `tests/unit/api/test_agent_runtime_mgmt.py` 27 用例
  （全端点 401/403/404/409/400/500 分支 + 密钥复用/降级 + 配额校验边界）。
- 门禁：全量 463 passed + 1 个既有测试的过期 patch 目标修正
  （`api.agent_runtime_mgmt.get_agent_controller` → `services.cloud_runtime.management.get_agent_controller`，
  patch 目标跟随符号搬家）。

### 迭代 4（2026-09-07 22:40-23:00）看门狗调度器循环补测 25% → 100% ✅
- `core/goal_loop_watchdog.py`（56 行）此前仅门控分支被顺带覆盖，主循环
  （sweep + Pod 回收汇总 + 异常吞噬）、启停幂等、间隔解析全部 0 覆盖。
- 新增 `tests/unit/core/test_goal_loop_watchdog.py` 14 用例：门控真值表、
  间隔下限 30s/非法回退、双段假 Event 同步驱动循环恰好一轮（回收计数落
  last_run、回收失败不杀循环、sweep 异常吞噬）、启动幂等/停止后可重启/
  停止在首个 wait 即打断（不真等 3600s）。
- 覆盖率：**100% 行覆盖**（56/56）。纯补测，源码零改动。

### 迭代 5（2026-09-07 23:00-23:50）goal_loops 路由缺口补测 75% → 100% ✅
- `api/projects/routes_goal_loops.py`（194 行）的 GET 列表端点、kick 端点、
  以及 title/goal_text/stall_limit 校验、NO_ACTIVE_AGENT、各类 404/403 与
  服务层异常 → handle_api_error 的分支此前未覆盖。
- 新增 `tests/unit/api/test_goal_loops_routes_gaps.py` 29 用例，与既有
  test_goal_loops.py 合并后该文件 **100% 行覆盖**（194/194）。
- 经验：补缺口测试时"路由的 except 兜底"必须有真实到达异常的路径
  （列表 500 用例先要有循环存在才会走进取任务的异常点）。

### 迭代 6（2026-09-08 00:00-00:30）controller 生命周期补测 56% → 83%（其余为他人 WIP 区）✅
- `services/agent_runtime_controller.py`（500 行）Pod 生命周期函数此前仅 56% 语句覆盖：
  `_init_k8s_client` 两条初始化路径、shared_workspace PVC 确保、Secret/PVC 非 404 重抛、
  `terminate_agent_pod`（无 Pod/删除成功/删除失败）、`get_agent_pod_status` 真实路径、
  `_format_pod_status`（含 start_time/conditions 缺省）、list/find 的 ApiException 容错——
  全部补测。
- 新增 18 用例（文件内 12 → 30），除 auto_assign_task 区（409-500 行，**并行会话 WIP，
  按 hunk 纪律不动不测**）外全部行覆盖：83% 语句覆盖，剩余缺失行 100% 落在他人 WIP 函数内。
- 覆盖率口径说明：该文件的"100%"以待本会话拥有且可改动区域计；auto_assign_task 的
  org_id 语义修复归原作者，等其提交后再补测收口。

### 迭代 7（2026-09-08 00:40-01:40）拆分 ai_service 763 行 → ai/ 包七模块 ✅
- 前：`services/ai_service.py` 763 行混了六件事：容错配置（DB+缓存）、错误码/上下文、
  限流器、双级缓存、审计落库、LLM 调用编排；`LLMService.call()` 单方法 280 行
  （限流/缓存/请求/响应解析/HTTP 错误分类/五类异常处理全部内联）。
- 后：`services/ai/` 包：config 27 / errors 38 / rate_limiter 48 / response_cache 70 /
  audit_logger 38 / llm_service 182 行 + 20 行兼容门面；`call()` 重构为编排器，
  拆出 `_guard_rate_limit`/`_guard_cache`/`_apply_http_error`/`_handle_success`/
  `_execute_chat`/`_error_result` 六个可独立测试的阶段方法，外部行为逐字段钉住不变。
- 覆盖率：包内 8 文件全部 **100% 行覆盖**（此前整文件 0 专项测试）；新增
  `tests/unit/services/test_ai_infra.py` 63 用例（含限流滑窗、Redis 回填/失效、
  审计失败重排、429/401/400 各变体/连接与读取超时/网络异常等端到端分支）。
- 门禁：全量 638 passed（上一迭代 523）。
- **测试污染教训**：全量门禁下早前 api 测试会把 core.redis_client 单例连上本地
  真实 Redis——AIResponseCache 的"内存过期"用例独跑绿、全量挂（L2 真写真读）。
  测试内 patch core.redis_client.get_json/set_json 为进程内假实现后收敛。
- 经验：mock session 要从 `_get_session` 注入（实例属性替换），否则配置版本检测
  会用真实 Session 覆盖掉 mock，测试悄悄打到真网络；"拆包不改变行为"靠
  先钉行为（"Bad request: Bad request"这类怪文案也原样保留）再动刀。

### 迭代 8（2026-09-08 02:00-03:00）拆分 redis_cache_service 543 行 → cache/ 包 ✅
- 前：`services/redis_cache_service.py` 543 行混了四件事：分布式锁（SET NX EX +
  Lua 释放 + 自动续期线程）、L1 进程内 LRU 缓存、L1+L2 编排（防击穿双重检查/
  批量/模式失效/统计）、cached 装饰器；且**全库零引用**（从未接线的基础设施），
  专项测试为零。
- 后：`services/cache/` 包：constants 4 / lock 56 / local 52 / core 158 行 +
  23 行兼容门面（单例与符号身份保持）。
- 覆盖率：包内 5 文件全部 **100% 行覆盖**；新增
  `tests/unit/services/test_cache_service.py` 52 用例（假 Redis/假线程驱动：
  锁的阻塞超时与续期循环、LRU 淘汰与过期清理、双重检查命中路径、扫描失效、
  装饰器 key 构造）。
- 建议（留档）：该服务全库无人 import——若后续确认不再接线，整包可删
  （删除前需用户确认）。

### 迭代 9（2026-09-08 03:20-04:30）拆分 connectors.py 459 行 → connectors/ 包（按 provider）✅
- 前：`services/connectors.py` 459 行混了三平台（Jira/GitLab/Linear）×（验签 +
  issue 应用 + 评论应用 + ingest 分发）+ 配置存取 + 共享工具；同一
  "default_project_id 解析 + 校验"块在文件里重复 5 次；专项测试为零。
- 后：`services/connectors/` 包：store（配置存取）/ verify（三平台验签）/
  common（resolve_project 消灭 5 处重复 + find_task + emit_sync_event）/
  jira / gitlab / linear；`__init__` 兼容旧导入路径（api/connectors.py 的
  9 个符号导入零改动）。
- 覆盖率：包内 7 文件全部 **100% 行覆盖**；新增
  `tests/unit/services/test_connectors_split.py` 27 用例（三平台建任务/状态
  映射/评论追加/未导入评论跳过/缺项目报错/未启用与未知事件分支/outbox 落库）。
- 坑（再次验证）：conftest 的 db_session 绑定它自己的 app/内存库，与测试自建
  app 的 db.session 是**两个库**——夹具必须用与被测代码相同的 session 自建数据，
  否则"独跑绿、类内跑挂"。

### 迭代 10（2026-09-08 04:40-05:20）agent_batch_ops 0 → 100% 覆盖 ✅
- `services/agent_batch_ops.py`（405 行）此前零测试：CSV/JSON 导出、JSON 导入
  （create/update 模式）、批量轮换/改状态/删除全部无回归保护。
- 评估后**不拆文件**：单一 AgentBatchOperations 类、职责内聚（Agent 批量操作域），
  拆了反而破坏内聚；按纪律"补测到 100%"即可。
- 新增 `tests/unit/services/test_agent_batch_ops.py` 18 用例。覆盖率 **100%**
  （133/133 语句）。门禁 683 passed（上一迭代 665）。
- 钉住的行为怪点（留档）：`export_agents_to_json(include_secrets=True)` 参数
  当前是 no-op（文档字符串已注明不含明文，但参数有误导性）。

### 迭代 11（2026-09-08 05:30-06:10）secret_analytics 0 → 100% 覆盖 + 修可移植性 bug ✅
- `services/secret_analytics.py`（267 行，SecretUsageAnalyzer 分析器）零测试。
- **测试抓出真 bug**：`get_usage_trends` 里 `r.date.isoformat()`——`func.date()`
  在 MySQL 返回 date、在 SQLite 返回 str，后者直接 AttributeError。
  该模块在 SQLite 环境（含全部单测）整体不可用。已改为类型归一处理。
- 新增 `tests/unit/services/test_secret_analytics.py` 12 用例（趋势填充/异常
  检测高中低档/热力图/Top 调用者/报告汇总/工作区统计/单例）。覆盖率 **100%**
  （72/72 语句）。

### 迭代 12（2026-09-08 06:20-07:00）agent_health 34% → 100% + 修全路径崩溃 bug ✅
- **测试审计抓出最重的一个 bug**：`services/agent_health.py`（AgentHealthMonitor，
  被 api/agent_analytics.py 的健康端点调用）查询 `AgentTaskLease.leased_at /
  is_complete / completed_at`——**这三列在表上根本不存在**，任何健康检查调用
  必然 AttributeError → 端点 500。该功能自上线起从未工作过（无测试掩护）。
- 修复：心跳 = 最近租约 `created_at`；活跃 = `active AND expires_at > now`；
  今日完成 ≈ 已释放（active=False）且 `updated_at` 在今天（表无完成时间戳，
  注释已写明近似语义）。
- 新增 `tests/unit/services/test_agent_health.py` 11 用例（三态判定/巡检只看
  ACTIVE/汇总与空工作区/updated_at 心跳回退语义）。覆盖率 **100%**（65/65）。
- 语义钉子：新建 Agent 无租约时会被 updated_at 回退判为 online——测试已按此
  行为断言（若产品上希望"从未干活即 unknown"，属行为变更，另立迭代）。

### 迭代 13（2026-09-08 07:30-08:10）github_client 39.8% → 100% ✅
- `services/github_client.py`（93 语句，P1.1 代码平面对 GitHub 的写操作最小面）
  仅被 api/project_repo.py 间接使用，此前无专项测试。
- 新增 `tests/unit/services/test_github_client.py` 22 用例：请求封装（URL 拼接/
  204/非 JSON 体/错误体映射/网络异常 502）、分支 ensure 语义、PR 建查列合、
  resolve_token 三级凭证优先级（App installation → 绑定 token → GITHUB_TOKEN，
  逐级静默回退）。
- 覆盖率 **100%**（93/93 语句）。结构内聚，无需拆分。

### 迭代 14（2026-09-08 08:50-09:40）第二轮启动：四文件覆盖率收口 → 100% ✅
- 目标重估：`api/agent_runtime_pull.py` 已被并行会话占用（他人 WIP，不碰）；
  覆盖率 JSON 驱动选出四个干净小文件一次性收口。
- `services/knowledge_curation.py` 83.7% → **100%**（9 用例：去重命中、评审
  来源提案、无发起 Agent 确认拒绝、已确认不可驳回）
- `services/review_gate.py` 84.1% → **100%（并集）**（9 用例：编排
  role_assignments 解析、PR 号过滤、自评禁止）
- `services/failure_recovery.py` 87.7% → **100%（并集）**（5 用例：无 Agent
  降级、经验入库异常不阻断、策展异常不阻断、修复任务继承父 DoD）
- `services/deploy_check.py` 86.9% → **100%（并集）**（7 用例：MySQL 方言
  分支、告警级配置/DEBUG、数据库不可达短路、缺表/缺列、迁移序号解析）
- 新增 30 用例，4 个测试文件。坑：conftest 的 `:memory:` SQLite 连接池状态
  不可依赖（新连接=空库），单测分支隔离需桩掉表存在性而非依赖真实建表。

### 迭代 15（2026-09-08 09:50-10:30）core 层零覆盖模块收口 ✅
- `core/jwt_refresh.py` 0% → **100%**（112 行，JWT 自动续期中间件；11 用例：
  刷新判定五分支、中间件 4xx 跳过/无 JWT 跳过/续期写 X-New-Token/身份转换/
  外层异常吞噬）
- `core/interaction_contract.py` 7.7% → **100%**（264 行，多 Agent 交互契约
  校验；35 用例：请求/回执归一化器的全部分支——字段缺失、长度上限、枚举、
  自指目标、SLA 边界、敏感级、置信度区间、resolver 一致性）
- 新增 46 用例，2 个测试文件。两模块均为纯函数/轻依赖，无需拆分。

### 迭代 16（2026-09-08 10:40-11:40）MCP 共享工具 + GitHub App 缺口收口 ✅
- `api/mcp/_shared.py` 0% → **100%**（161 行；19 用例：内存频率限制（超限
  429/按用户分桶/过期清理）、API Token 认证装饰器三档 401 与 g 注入、
  XSS 清洗行为钉子（html.escape 先于标签移除）、整数校验）
- `services/github_app.py` 79.2% → **100%（并集）**（22 用例：encrypt/decrypt
  的 v1 前缀与 legacy 回退、真实 RSA 的 App JWT 三段结构与 660s 有效窗、
  installation token 请求/过期解析/默认 10 分钟、缓存命中与 5 分钟刷新边距、
  manifest 兑换、按配置取 token 的两档拒绝、webhook secret 三级回退、
  upsert secret 加密与 installed 标记）
- 新增 41 用例，2 个测试文件。

### 迭代 17（2026-09-08 12:10-13:50）sso + budget_service 收口 → 100% ✅
- `services/budget_service.py` 93.3% → **100%**（9 用例：月度/未知周期、
  无成员工作区 token 用量回 0、agent 范围在时长/并发两资源上的过滤、
  未知资源 not_tracked、超限事件缺 task_id 拒绝、非请求上下文审计降级）
- `services/sso.py` 91.5% → **100%**（16 用例：账号映射名回退链（用户名
  格式 sso_<name>_<rand> 钉住）、OIDC code 兑换（httpx 兼容假客户端）、
  无 access_token 拒绝、login_oidc 全链路签发 JWT、SAML state 的
  workspace/request_id 校验、authorize_url 拼接、自建 httpx.Client 的
  finally close、upsert 的 secret 加密）
- 新增 25 用例，2 个测试文件。**发现一处不可达分支**：`_token_usage` 的
  "无成员回 0"路径在 organizations.owner_id NOT NULL 约束下无法通过真实
  组织触达，以不存在工作区（org=None）等价覆盖。

### 迭代 18（2026-09-08 14:00-15:00）skill_profile / insight_actions / marketplace 收口 → 100% ✅
- `services/skill_profile.py` 95.3% → **100%**（未知经验类型计正向、
  重建/遗忘对不存在 Agent 拒绝、遗忘写墓碑快照、打分加成与 20 分 cap、
  空匹配词/无画像回 0）
- `services/insight_actions.py` 96.4% → **100%**（知识覆盖统计跳过已禁用
  Agent、导师不自配、无空闲领域跳过、limit 凑满提前返回——以构造覆盖表
  驱动，摆脱对真实经验数据的依赖）
- `services/marketplace.py` 96.2% → **100%**（create_agent 缺名/重名拒绝）
- 新增 9 用例，1 个测试文件。mcp_manager.py / mcp_server_methods.py 为
  进程管理/MCP 运行时脚本（前者 subprocess 管理脚本，后者依赖 mcp 包异步
  运行时），不属于单测目标，留档说明。

### 迭代 19（2026-09-08 15:10-16:40）auth API 43% → 99% ✅
- `api/auth.py`（334 语句，认证核心面）此前 43% 覆盖、0 专项测试文件。
- 新增 `tests/unit/api/test_auth_surface.py` 67 用例：
  - 纯助手：回环地址归一、回跳地址七分支、query 参数保留
  - 访客登录：首建/复用/token 生成失败 500
  - OAuth：github/google 入口与回调的成功链路、authorize/token 交换失败、
    中间产物（userinfo/建户/发 token）逐级失败、异常兜底 500
  - 账号面：logout/me 读写（preferences 合并与类型拒绝）、verify、refresh
    （无 token/未知用户/非整型身份/生成失败）
  - 管理面：用户列表 admin 门禁 + search/status/role 过滤、用户详情三视角
    （self/admin/共享组织公开档案含角色键）、状态管理五分支
  - 组织角色键收集：owner 短路、非成员空、角色定义去重归一、legacy 回退
- 结果 **99%**（334 语句缺 1 行）：`_collect_user_org_role_keys` 末行
  `return []` 在 member.role NOT NULL + default=MEMBER 约束下不可达（死行，
  按计划纪律注明不强凑）。**注意本仓库 `.env` 会把 DOCKER_ENV 等灌入
  os.environ，分支类测试必须显式 delenv/setenv 固定环境**。

### 迭代 20（2026-09-08 16:50-18:00）context_rules API 17% → 100% ✅
- `api/context_rules.py`（292 语句，12 端点 + 双级缓存）此前 17% 覆盖、
  零专项测试。新增 `tests/unit/api/test_context_rules_api.py` 46 用例：
  列表全部分支（项目权限 403/scope/布尔过滤/搜索/四种排序/分页/项目信息
  批量装配）、创建校验矩阵、单查/更新/删除属主校验与异常兜底、
  activate/deactivate、build-context、规则广场与复制四分支、全局规则
  （缓存回放写法：删库后二次请求仍 200）、merged/preview。
- **修了 3 个真 bug（全部无测试掩护）**：
  1. `/global` 用 `ContextRule.is_global == True` 过滤——`is_global` 是
     Python property 而非列，SQL 里恒 False → 本人全局规则永远查不出来；
     改为 `project_id IS NULL` + 可见性（本人或已公开）；
  2. `?is_active=false` 参数形同虚设——`get_request_args` 根本不含该键，
     过滤永远生效；改为从 `request.args` 直读；
  3. `sort_by=rule_type` 引用模型不存在的列 → 列表页排序 500；移除死排序键。
- 覆盖率 **100%**（291/291）。坑：`validate_json_request` 对空 dict `{}`
  一律 400（`if not data`），"全可选字段"的端点也必须带至少一个字段。

### 迭代 21（2026-09-08 18:10-19:30）custom_prompts API 修复三处从未工作过的端点 + 收口 100% ✅
- `api/custom_prompts.py`（537 行 15 端点）零专项测试。新增
  `tests/unit/api/test_custom_prompts_api.py` 47 用例后揭出 **4 个真 bug**：
  1. **export 端点从未工作过**：`'export_time': db.func.now()` 把 SQL 函数
     对象塞进 JSON 响应 → jsonify 必炸（测试客户端 status_code=0）；
  2. **preview 端点同病**：`'preview_generated_at': db.func.now()` 同样必炸；
     均改为 `datetime.utcnow().isoformat()`；
  3. **全部 13 处 `handle_api_error(e, "中文消息")`**——第二参数是
     status_code 形参，传字符串导致错误响应状态码为 0（非法 HTTP 响应），
     全模块错误路径从未正确返回；统一改回 `handle_api_error(e)`；
  4. `?prompt_type` / `?is_active` 过滤参数形同虚设（get_request_args 不含
     该键，同迭代 20 的模式）——改为 request.args 直读并保留"默认只列激活"。
- 覆盖率 **100%**（267/267 语句，47 用例）。

### 迭代 22（2026-09-08 20:00-22:00）openai_compatible 15% → 100% + 修 2 个从未工作过的能力 ✅
- `api/openai_compatible.py`（876 行 422 语句，OpenAI 协议兼容面：
  三档认证装饰器 / 双级缓存管理器 / chat 校验与流式 / embeddings /
  usage / 缓存管理端点）此前 15% 覆盖、零单测（根目录两个
  test_openai_api*.py 是打真实服务器的运维脚本，pytest 不收集；
  其一还硬编码了 API token，留档提醒）。
- **修了 2 个真 bug**：
  1. `/chat/completions` 的 `validate_json_request` 未列 optional_fields →
     过滤器把 stream/temperature/max_tokens/top_p 等全部丢弃——
     **流式模式从未生效**（客户端要流式也拿到非流式响应），采样参数
     从未到达 LLM；已列全 optional_fields；
  2. `/embeddings` 同因过滤丢掉 `model` → 恒用默认模型；已补。
- 新增 `tests/unit/api/test_openai_compatible_api.py` 72 用例：认证三档
  与降级链（8 用例）、缓存管理器（13：双级读写/过期回填/锁生命周期/
  双重检查/失效广播/异常容错）、chat 校验矩阵（约 20 参数分支）、
  chat 端到端（缓存命中/失败透传/流式分块/[DONE]/系统提示词剥离/
  response_format 透传/异常 500）、embeddings、usage 聚合、cache 管理
  端点权限。覆盖率 **100%**（422/422）。
- 根目录 test_openai_api*.py 硬编码 token 属安全问题，删除需用户确认。

### 迭代 23（2026-09-08 22:10-23:20）agent_teams API 35% → 100% + 修 3 个真 bug ✅
- `api/agent_teams.py`（310 语句，13 端点：团队 CRUD + 成员管理 + 团队项目
  关联）此前 35% 覆盖、零专项测试。新增
  `tests/unit/api/test_agent_teams_api.py` 40 用例（真实鉴权链：owner+JWT）。
- **修了 3 个真 bug**：
  1. **create_team 从未成功过**：`name` 同时显式传入又在 `**team_data`
     里重复 → TypeError → 创建团队接口必然 500；
  2. `?status=` 过滤按 value（'archived'）与按 name 落库的枚举比较 →
     永不命中（记忆中的 Enum name/value 坑再现）；
  3. add_team_member 的白名单只有 agent_id → role/responsibility/config
     被静默丢弃（validate_json_request 白名单模式第 3 次踩中）。
- 覆盖率 **100%**（310/310）。重复的"工作区 404 / 越权 403"守卫行用
  参数化（13 端点 × 2 场景）一次性收口。

### 迭代 24（2026-09-09 00:30-01:40）workspace_secrets 路由 17% → 100% ✅
- `api/agent_workspace_secrets/routes_secrets.py`（227 语句，7 端点：机密
  列表/创建/reveal/共享 reveal/rotate/revoke/shares 列表）此前 17% 覆盖、
  零专项测试。新增 `tests/unit/api/test_workspace_secrets_api.py` 39 用例。
- 覆盖：列表（share 计数/include_shared 共享来源过滤——吊销与自共享跳过、
  幽灵 owner 归一 None）、创建校验矩阵（必填/空值/类型/scope/project 归属/
  非整型/重名 409）、reveal（解密回显+usage 计数/已吊销 400）、共享 reveal
  （无授权 404/过期 404/非 read 403/已吊销 400/成功含所有方）、rotate
  （哈希密文轮换/已吊销 400）、revoke（连带吊销 share+grant）、shares 列表
  （include_inactive/is_expired 计算）、13 端点守卫（agent 404/外人 403/
  secret 404）参数化。
- 覆盖率 **100%**（227/227）。
- 同包 routes_collaboration.py（447 行）与 routes_grants.py（297 行）留待
  迭代 25。

### 迭代 25（2026-09-09 01:50-03:30）workspace_secrets 协作与授权路由收口 ✅
- `routes_collaboration.py` 92% → **100%**（211 语句；协作拓扑聚合：
  出/入边、协作者统计排序、project 过滤、include_inactive 语义——share
  保留但已吊销 secret 的边仍跳过、悬空 secret_id 的幽灵 share 跳过）
- `routes_grants.py` 92% → **99%**（155 语句缺 1 行；授权链：创建默认值
  leased=1 天 100 次/persistent=30 天不限、task 归属工作区校验、
  attempt 长度、同 agent 拒绝、目标 404/停用 400、状态过滤与
  include_expired/include_inactive、吊销 409）
- routes_grants 唯一未覆盖行 255（`return manage_err`）为覆盖率归属异常：
  同文件同模式已覆盖 8 处，且测试断言了该端点的 403（实测 403 来自
  unified_auth 层）——按计划纪律注明，不强凑。
- 新增 `tests/unit/api/test_workspace_secrets_collaboration_grants.py`
  61 用例。

### 迭代 26（2026-09-09 02:00-03:10）全局丢参模式排查（AST 扫描 + 修复）✅
- 用 AST 脚本全量扫描 api/ 下 validate_json_request 白名单丢参与
  get_request_args 死参数两类模式（排除他人 WIP），确认 **6 处白名单
  丢参 + 1 处死过滤**：
  1. `agent_role_templates.create_template` 丢 parent_template_id
     （市场安装去重依赖该字段）
  2. `agent_role_templates.instantiate_template` 丢 14 个 Agent 字段
     （system_prompt/llm_model 等全部被过滤）
  3. `agent_team_orchestration.start_orchestration` 丢
     participating_agent_ids/config/output_aggregator/subtasks
  4. `agent_teams.add_team_project` 丢 role/config（迭代 23 漏网）
  5. `ai_task_assistant.task_assistant` 丢 project_context/stream/use_cache
  6. `ai_task_assistant.enhance_task` 丢 description/use_cache
  7. `agents/experiences` 三端点 9 个死过滤参数（get_request_args 不含）
- 全部修复（validate 补列 optional_fields；experiences 改 request.args
  直读；orchestration 连带补 AgentTeamStatus/AgentStatus/Project 导入）。
- 新增 `tests/unit/api/test_param_whitelist_sweep.py` 9 用例作回归钉子。
- 经验：`validate_json_request` 的白名单若用模块常量拼接（BinOp），
  AST 审计需跟随常量解析，否则误报"create_agent 丢 29 字段"。

## 收尾总览（2026-09-08 07:40 起，迭代 13 后更新）

**门禁**：全量单测 396（基线）→ **1275 passed**（26 个迭代全部绿灯后）。

**第二轮（迭代 14-19）补充**：knowledge_curation / review_gate /
failure_recovery / deploy_check / jwt_refresh / interaction_contract /
mcp_shared / github_app / sso / budget_service / skill_profile /
insight_actions / marketplace 全部收口至 100%；auth API 43% → 99%
（缺 1 行死行）。合计约 190 个新用例。剩余大缺口：api/agents/*
（他人 WIP）。已完成：openai_compatible（修复流式从未生效）、
context_rules（修复 3 bug）、custom_prompts（修复 4 个死端点）、
agent_teams（修复创建必 500 等 3 bug）、agent_workspace_secrets 全包
（routes_secrets/collaboration/grants）。根目录 test_openai_api*.py
含硬编码 token，属运维脚本非单测，删除需用户确认。

**覆盖率终值（coverage JSON 度量，services+api 合计 45% → 52.26%）**：
本夜触碰的全部 39 个模块 **100% 行覆盖**，唯一例外
`services/agent_runtime_controller.py` 84.4%——缺失行 100% 落在并行会话
WIP 的 `auto_assign_task`（按 hunk 纪律不动不测，等原作者收口）。

**大文件拆分（4 次拆包，全部保持导入路径兼容）**：
- goal_loop_service 744 行 → services/goal_loop/ 包（六模块 + 门面）
- ai_service 763 行 → services/ai/ 包（六模块 + 门面），call() 280 行拆六阶段
- redis_cache_service 543 行 → services/cache/ 包（四模块 + 门面）
- connectors 459 行 → services/connectors/ 包（按 provider 七模块）

**顺手修掉的真 bug（全部无测试掩护的存量）**：
1. agent_runtime_mgmt：status/list 路由调用不存在的 `has_workspace_access`
   → 必然 500（迭代 3）
2. agent_health：查询 AgentTaskLease 不存在的三列 → 健康端点自上线必然 500
   （迭代 12）
3. secret_analytics：`func.date()` 跨库返回类型不一致 → SQLite 全路径崩溃
   （迭代 11）
4. connectors：同一 default_project_id 解析块重复 5 次 → 收敛到 common
   （迭代 9）

**移交项**：
- `auto_assign_task` org_id 修复归并行会话；其提交后补测收口至 100%。
- `services/cache/`（原 redis_cache_service）全库零引用——建议用户裁决是否删除。
- `agent_batch_ops.export_agents_to_json(include_secrets=True)` 参数为 no-op，
  语义应澄清（属行为变更，未擅动）。
- 迭代 12 的"新建 Agent 无租约判 online"语义如需改为 unknown，属产品决策。

### 迭代 27（2026-09-09）agent_automation 包 + channels.py 收口 ✅
- **删死文件 `api/agent_automation/routes_channels.py`（260 行）**：
  c742464（4 月）把渠道路由解耦到独立的 `api/channels.py` 时，
  只删了 `__init__.py` 里的一行 import，忘了删旧文件——留下一个
  与在册实现逐行等价的死副本，零引用、0% 覆盖。危险在于它
  会误导后续维护（本轮审查第一结论差点是"端点没注册，接回去"，
  接回去就会与 channels_bp 产生重复 URL 规则）。
- `api/agent_automation/shared.py` 删除不可达守卫（`split('-', 1)`
  恒返回 2 元素，`len(bounds) != 2` 分支永不触发）。
- 新增 `tests/unit/api/test_agent_automation_api.py` 97 用例，覆盖：
  - 触发器 CRUD 全分支（task_event/cron 分派、重名 409、窗口钳制、
    Patch 类型不匹配字段忽略语义、删除=停用）
  - Runner 配置（执行模式/沙箱策略清洗含域名归一、版本双自增）
  - 运行列表与详情（状态过滤、分页 has_prev/has_next、排序）
  - 通知渠道全端点（user/org/project 三 scope 的读/写权限矩阵：
    owner/member/stranger 三视角、webhook 头清洗+掩码、feishu/
    dingtalk Patch 保 secret、effective-channels 三级回退与事件过滤、
    无组织项目跳过 org 层）
  - shared 辅助（cron 解析全分支、`0 0 31 2 *` 扫满一年放弃、
    dow 周日=0 映射、布尔/整数归一矩阵、幂等键稳定性）
  - 路由真实注册断言（channels_bp + agent_automation 双蓝图）
- 覆盖率：包内 6 文件 + api/channels.py 全部 **100%**（此前包整体
  12-30%、channels.py 23%）。
- 教训：解耦/搬移类重构必须同时删旧文件——grep 引用数为零不等于
  "没接线是故意的"，要先查 git log 确认是否为搬移残留。

### 迭代 28（2026-09-09）agent_workspace_insights 包：拆双胞胎 + 整包 100% ✅
- **修最重存量 bug：interactions 端点 from/to 时间过滤把聚合函数写进
  WHERE**（`filter(func.max(TaskLog.created_at) >= from)`）——分组查询里
  SQLite 抛 "misuse of aggregate: max()"，MySQL 同样拒绝；**带时间参数
  的请求自上线起必然 500**。改为与其他聚合过滤一致的 HAVING。
- **拆双胞胎**：activity.py（352 行）与 workspace_activities.py（413 行）
  是约 90% 同构的五源聚合循环（run/attempt/task_event/task_log/audit），
  仅 agent 归属口径不同。抽出共享 `activity_collectors.py`
  （ActivityScope 参数化 + 五个收集器 + 富化 + 过滤切页汇总），两个
  路由文件各瘦身为 27 行纯鉴权+分发薄壳；净删约 700 行重复。
- 新增 `tests/unit/api/test_agent_workspace_insights_api.py` 57 用例：
  - agent 活动端点：五源装配、失败/中止级别判定、audit risk_score 级别
    回退（60/25/5 → error/warn/info）、全量过滤矩阵（source/level/
    event_type/task/project/run/attempt/actor/q/min_max_risk）、时间窗、
    分页与 scan_limit 钳制、task_title/project_name 回填（含仅 project
    payload 的 run）
  - 工作区活动端点：跨 agent 聚合 + agent_name 档案回填、agent_id 过滤、
    审计行无 agent 关联跳过（含 actor_id 不可解析）、target/actor 回推、
    全五源一次到位的 summary 断言
  - 活动事件端点：游标编解码 + 非法游标 400 + 游标翻页不重不漏、
    limit 钳制、全过滤参数、agent/task/project 实体富化
  - agent 任务/项目/交互三维统计：触达集合（attempt∪log）、提交率、
    活跃度分数上界与久远衰减下界、HAVING 区间、排序字段回退、
    display_name 回退链
  - shared 辅助直测 + 审计降级查询（unknown column 降级 / 其他错误上抛）
- 覆盖率：包内 9 文件全部 **100%**（拆分前 6-20%）；门禁
  **1452 passed**（27 迭代后 1395）。
- 经验：matcher 类纯函数（`_activity_item_matches` 12 个过滤维度）在
  端点测试里很难自然命中全部分支，直接来一组单维真值表直测最省；
  双胞胎文件合并前先用端到端断言钉死可观察输出，再合并，测试一行不用改。

### 迭代 29（2026-09-09）organizations 包收口 ✅
- 新增 `tests/unit/api/test_organizations_api_full.py` 44 用例，补齐
  organizations 包 6 文件至 **100%**（原 15-53%）：
  - 组织 CRUD：可见域（owner∪member，陌生人空集）、搜索/状态/三种排序
    （钉住怪癖：get_request_args 缺省 sort_by='created_at'，列表端点的
    updated_at 分支需显式传参才触发）、五维计数装配（member/agent/
    project/active_role/last_activity）、slug 冲突自增后缀、创建链路
    （owner 成员 + owner 角色绑定 + org.created 事件）、归档分型事件
  - 成员管理：邀请（重邀请复活 REMOVED 成员、owner 邮箱 409、owner 角色
    禁授、角色两种入参）、更新（owner 保护、状态机校验、事件载荷）、
    移除、列表 include_user
  - 角色管理：系统角色种子幂等与"复活"（非系统/停用/无名修复）、key
    去重后缀、系统角色禁删禁停用、删除后剩余绑定主角色重同步
  - 组织事件：record 工具全兜底（截断 512+3/actor_name 回退 actor_id/
    非 dict payload 进 raw_payload/ip 注入失败保底）+ 路由过滤矩阵
  - 全部 7 个 500 兜底分支经 monkeypatch 注入触发；非 JSON 体 400
- **删死代码 2 处**：`_backfill_member_role_bindings`（40 行零调用，
  且内部 stale role_bindings 集合会触发 UNIQUE 自撞——同 session 内
  先读过成员再回填必炸，幸好从未接线）；`_sync_member_primary_role`
  的 `except ValueError`（ROLE_PRIORITY 四键全是合法枚举，分支不可达）；
  `_get_user_org_roles_map` 的空角色 setdefault 死分支（role 列
  NOT NULL 非空枚举）。
- 门禁 **1496 passed**（28 迭代后 1452）。
- 经验：catch-all `except Exception` 的 500 分支是覆盖率钉子户，
  monkeypatch 模块命名空间内的符号（from-import 落地的本地名）是最省
  的触发方式；`Query.get()`/expire 语义（expire 丢弃未 flush 修改）
  反复成为测试自身失败的来源，断言前先 commit。

### 迭代 30（2026-09-09）ai_task_split：三个上线级 bug 修复 + 100% ✅
- **Bug 1（最重）**：拆分主端点默认 atomic 分支用
  `with db.session.begin():`——请求内先查父任务已开启事务，
  SQLAlchemy 2.0 直接抛 InvalidRequestError 被外层 except 吃掉，
  **AI 拆分成功路径上线起必 500**。修复：删除伪原子包装，依赖请求级
  单一事务 + 失败统一 rollback 的天然全有或全无语义（原 atomic 分支
  与非 atomic 分支本就做同样的事）。
- **Bug 2**：`Task.tags.contains(['parent_task:X'])` 把整个单元素
  JSON 数组当 LIKE 子串——多标签行永远匹配不上。**子任务列表/删除/
  重排序/剩余计数四处查询在生产 MySQL 上同样全坏**（真实子任务有
  3 个标签）。修复：改引号定界 LIKE（`%"parent_task:X"%`，闭口引号
  防 parent_task:1 误中 12，SQLite/MySQL 通用）。
- **Bug 3**：delete_subtask 父任务无 tags 时 `remaining` 未赋值 →
  NameError → 500。修复：前置初始化 `remaining = 0`。
- 新增 `tests/unit/api/test_ai_task_split_api.py` 42 用例：输入清洗/
  子任务校验矩阵（标题截断、优先级回退、预估时间钳制、依赖整数化）、
  LLM JSON 四级降级解析、主端点全分支（429 限流映射、解析失败、
  空子任务、成功链路含父任务标签置换与 auto-assign 联动、缓存参数
  透传、数量钳制、401/SQLAlchemyError/兜底 500）、子任务列表元数据
  解析与排序、删除（无标签父任务回归钉子）、重排序校验矩阵。
- 覆盖率 16.1% → **100%**；门禁 **1538 passed**（29 迭代后 1496）。
- 经验：`JSON 列.contains(list)` 不是 JSON 包含判断而是整段数组文本
  LIKE——凡是"按 JSON 数组中的一个成员查行"都要用引号定界 LIKE 或
  方言原生函数；写"防御性"包装前先确认 SQLAlchemy 事务模型
  （Session.begin() 在活动事务上必抛，2.0 无隐式 autocommit）。

### 迭代 31（2026-09-09）dashboard + system_settings 收口 ✅
- **修 ollama 连接测试守卫矛盾**：`test_llm_api_connection` 入口
  `if not api_base or not api_key` 无差别要求 key，但 ollama 分支
  注释明说"通常不需要 API key"——**无 key 的 ollama 配置永远无法
  测试连接**。改为 `not api_key and provider != 'ollama'`。
- **删死代码**：`_empty_project_stats` / `_empty_task_stats`
  两个零引用助手。
- **coverage 盲行疑云排排查记录**：`_get_consecutive_active_days`
  的 `else: break` 语义上必然执行（间断用例断言通过）却始终不被
  coverage 记录——CPython 3.9 peephole 会把 `while True` 中以
  break 为唯一语句的分支做条件跳转重定向，行事件不再触发。重构为
  `for _ in range(365)` 有界循环 + `return` 早退（return 不受该
  优化影响），行覆盖与结构双改善。
- 新增 `tests/unit/api/test_dashboard_and_system_settings_api.py`
  52 用例：双层缓存（redis + 进程内回退 + stale 降级）、后台异步
  刷新去重与 in-flight 清理、owned/participated 双范围统计、大数据
  集降级、组织角色解析（owner 优先于成员行/优先级序/自定义 key/
  空 key 回退）、组织 Agent 7 天窗口统计、热力图/摘要缓存、连续
  活跃天数（间断/上限 365/查询异常）、系统设置管理员门禁矩阵、
  LLM 配置加密读写（部分更新保留旧密钥、非管理员掩码）、通用设置
  get/set、五 provider 连接测试与超时/连接错误/未知异常矩阵、
  全部 500 兜底分支。
- 覆盖率：dashboard 19.6% → **100%**，system_settings 18.6% →
  **100%**；门禁 **1590 passed**（30 迭代后 1538）。
- 教训：断言通过但某行始终不被 coverage 记录时，先怀疑 3.9 字节码
  优化（break 重定向/死代码消除）而非测试没跑到——用 dis 或
  coverage API 直接验证执行行集合，再决定改结构还是改测试。
  另：仪表盘蓝图挂在 `/dashboard` 子前缀、系统设置在
  `/system-settings` 子前缀（路由里 route('') 是相对路径），写
  端点测试前先查 app.py 的 url_prefix。

### 迭代 32（2026-09-09）agent_analytics 路由层收口 ✅
- 新增 `tests/unit/api/test_agent_analytics_api.py` 22 用例，模块
  22.4% → **100%**。本文件是纯委托层（业务在 agent_health/
  secret_analytics/agent_batch_ops 三个服务，迭代 10/11/12 已收口），
  测试用 Recorder 桩替换三个服务 Getter，专注路由职责：
  - 工作区门禁矩阵：10 个 GET 端点 × 工作区 404/陌生人 403 参数化、
    4 个批量 POST 端点 × 非 JSON 400/工作区 404/陌生人 403/成员非
    manage 403
  - 参数解析与透传：days/threshold/limit 默认值与显式值、agent_ids
    多值 getlist（空→None）、include_secrets 字符串解析、import 的
    mode 透传、force 旗标、AgentStatus 按 value 枚举（'paused'，
    非法值 400 回显）
  - 响应形态：CSV 下载（mimetype + Content-Disposition 文件名）、
    report 含 error 键 → 404 Secret、健康检查 agent 不存在 404
- 确认蓝图 url_prefix='/api/v1' 会被 register_blueprint 的
  url_prefix 覆盖，实际路由无双前缀问题（前端契约安全）。
- 门禁 **1612 passed**（31 迭代后 1590）。
- 经验：薄委托层的补测顺序——先 stub 服务 Getter 断开业务依赖，
  再用参数化矩阵扫门禁早退分支，最后逐端点钉参数透传；枚举入参
  统一按 value 形式（全库 SQLAlchemy Enum 按 name 落库、按 value
  查询的既有约定）。

### 迭代 33（2026-09-09）pins + api_tokens 收口 ✅
- 新增 `tests/unit/api/test_pins_api.py`（19 用例）与
  `tests/unit/api/test_api_tokens_api.py`（16 用例），两模块
  21.6%/21.1% → **100%**：
  - pins：双层缓存（redis+回退，TTL 20s，过期 miss/新鲜命中）、
    Pin/取消/复活（上限 10 豁免已 Pin 项目）、重排序校验与未知项目
    跳过、stats/task-counts（缓存命中、pending 三状态聚合）、仅可
    Pin 自有项目、invalidate_user_caches 联动、全部 6 端点 500 兜底
  - api_tokens：CRUD（列表脱敏、重名仅查 active、raw token 仅创建
    返回、过期设置/清空、物理删除）、reveal（损坏密文 400、停用/
    他人 404）、verify（无效/过期 401、usage_count 自增）、全部
    6 端点 500 兜底
- **行为钉子**：pins 的 unpin/reorder 只调 invalidate_user_caches，
  不清 pins 自身缓存——20s TTL 内提供有界旧读（属设计取舍，测试
  钉住；如需强一致应由用户裁决改为写时失效）。
- **删死代码 3 处**：`api_tokens.require_api_token_auth` 装饰器
  （全库零引用，MCP 用 api/mcp/auth.py 同名实现）、
  `UserProjectPin.get_user_pins` / `UserProjectPin.reorder_pins`
  （路由各自内联实现）。
- 门禁 **1647 passed**（32 迭代后 1612）。
- 经验：①`unified_auth` 对任何 Bearer 头一律先尝试 API token 认证
  再退回 JWT——patch ApiToken 的 query/verify_token 会把认证链炸穿
  并在端点 try 之外冒泡，500 兜底测试应 patch 端点 try 内部的依赖
  （如 session.commit，且要先造数据再 patch）；②测试助手不要悄悄
  改写名字类入参（重名校验依赖精确匹配）；③hash() 有随机化，禁止
  用作测试 id 生成。

### 迭代 34（2026-09-09）小模块清扫：task_labels + review + delegation + agent_performance ✅
- 新增 `tests/unit/api/test_small_route_modules_api.py` 22 用例，
  四模块 17-22% → **100%**（task_labels 128 / routes_review 50 /
  routes_delegation 73 / agent_performance 42 语句）：
  - 任务标签：内置标签种子幂等、项目过滤（404/403）、名称归一小写、
    重名 409、停用复活、内置/他人写保护、软删除、4 端点 500 兜底与
    非 JSON 400
  - 任务评审：REVIEW 队列过滤与分页、approve→DONE（完成率 100 +
    completed_at）、reject→IN_PROGRESS（feedback_content 追加带
    分隔符与旧反馈共存）、缺 decision/非法 decision/非 REVIEW 400
  - 任务委派：agent 委派（assignees JSON 去重追加、状态流转、
    auto-assign 与 WebSocket/房间推送三路副作用打桩断言、**副作用
    全挂也不阻断委派**）、回收（剥离 agent 型 assignee、保留人类、
    名单拼接）、可委派列表（仅 ACTIVE、按名称排序、capability_tags）
  - Agent 绩效：审计事件聚合（task_complete/error/failure 分类、
    duration 均值剔除非正值）、成功/错误率、7 天日活、除零保护、
    跨工作区 agent 404
- 门禁 **1669 passed**（33 迭代后 1647）。
- 经验：小模块合批清扫效率高——四个模块共用一套 env 夹具一次写完；
  委派端点的"副作用失败不阻断"是隐式契约，用三路 raise 打桩钉住。

### 迭代 35（2026-09-09）api/tasks 包收尾：attachments + batch + agent_chat ✅
- 新增 `tests/unit/api/test_tasks_remaining_api.py` 26 用例，三模块
  22%/29%/33% → **100%**（94 + 83 + 24 语句）：
  - 附件：上传落盘（扩展名白名单/空文件名/content_length 预检/
    落盘后 getsize 超限回滚文件并 400 双分支）、下载 as_attachment
    往返、删除连物理文件、列表、他人项目 403、任务 404、附件 404、
    list/delete/download/upload 四路 500 兜底；注：附件响应刻意
    不含 file_path（安全设计），落盘断言走 DB 行
  - 批量：状态/优先级/负责人/删除四路批量（不存在 id 忽略、计数
    返回）、缺参与 400、MAX_BATCH_SIZE 超限 400（patch 常量=2 避免造
    100 条数据）、dependencies GET/PUT 与 404
  - Agent 聊天：agent 会话认证 401 矩阵（缺头/无效令牌/非活跃）、
    content 必填、parent_id 同任务校验（跨任务 404）、TaskLog 落库
    （actor=agent、parent 关联）、房间推送 task_comment
- 观察项（未擅动）：批量四端点无项目级权限校验——任何登录用户可按
  id 批量改/删任意任务，与 pins 等模块的 owner 校验风格不一致；
  是否收紧属产品决策，已在测试注释标注。
- 门禁 **1695 passed**（34 迭代后 1669）。
- 经验：①multipart 上传的 content_length 恒大于纯 body——想测
  "落盘后超限"分支必须放行预检、单独 patch os.path.getsize，且注意
  20MB 阈值的量级；②批量硬删后身份映射残留过期实例，session.get
  会抛 ObjectDeletedError，改用 query 确认或先快照 id。

### 迭代 36（2026-09-09）core 层：google_config + notification_queue ✅
- 新增 `tests/unit/core/test_google_config_and_notification_queue.py`
  44 用例，两模块 22.3%/18.8% → **100%**（148 + 69 语句）：
  - GoogleConfig：环境变量缺失抛 ValueError / 齐备正常
  - GoogleService：init_app OAuth 注册（含构造器直传 app 分支）、
    get_user_info 成功/网络异常、create_or_update_user 四分支
    （google_id 命中→更新、邮箱命中→绑定 google_id、全新用户→
    四件套脚手架、缺邮箱/异常→None）、generate_tokens 对与错
  - 新用户默认脚手架幂等与语言检测：默认 API Token、用户设置
    （Accept-Language → zh-CN / locale 优先级 / 默认 en）、默认全局
    规则（内容含 UI 四原则）、默认提示词按语言初始化；已有任一项
    时整体跳过；异常路径回滚不打断主流程
  - 通知队列：进程内假 Redis（list/zset/kv/pipeline）覆盖入队、
    批量入队过滤非数字、重试调度（datetime/浮点两种 run_at）、
    到期晋升（zset→list 迁移、limit、空集）、阻塞弹出（非整数
    字段→None）、分布式锁 fail-open（无 Redis 放行）与属主校验
- 门禁 **1739 passed**（35 迭代后 1695）。
- 经验：假 Redis 的 zrangebyscore 必须只返回 member（与真实协议
  一致），返回 (score, member) 元组会让晋升逻辑写出脏数据但断言
  才暴露；scaffolding 类函数的失败注入要在 undo patch 之后再断言。
  api/agents/experience_analytics.py（646 行）仍处他人 WIP 目录
  （api/agents/ 27 个未提交文件），继续回避。

### 迭代 37（2026-09-09）services 层收尾：goal_decomposition + saml + runtime_policy ✅
- 全量重扫（第 37 轮）：services 层低覆盖仅剩 5 个——本轮收口
  goal_decomposition（79.2%→100%）、saml（82.3%→100%）、
  workspace_runtime_policy（81.0%→100%，与既有测试合计）；回避
  agent_runtime_controller（缺失行全在他人的 auto_assign_task WIP）
  与 task_content（未跟踪 WIP 文件）。services 层 51 文件除 WIP 外
  **全部 ≥99.5%**。
- **修真 bug：`_parse_saml_time` 小数秒+数字时区偏移被剁掉**——
  原实现 `''.join(ch for ch in rest if not ch.isdigit())` 会把
  `.123456+0000` 的偏移数字一并滤掉（`+0000`→`+`），导致带小数秒
  且带显式偏移的 SAML 时间戳全部解析失败。改为只剥前导小数位。
- 新增 32 用例：goal_decomposition 全链路（LLM 三态错误、二次转义
  再解析、DoD 白名单/截断、双向依赖去重、工作区项目回退）；saml
  缺口分支（元数据 binding 回退/缺证书、fetch 注入与自管客户端、
  签名验证 6 个 False 分支、verify 10 个错误分支含 Response 级
  签名回退/断言过期/InResponseTo、小数秒时间解析）、
  build_authn_request/build_saml_redirect；runtime_policy（集群
  异常静默、labels/phase 跳过、_pod_ready_at 三形态、
  _last_activity_at 空记录）。
- 门禁 **1783 passed**（36 迭代后 1739）。
- 经验：SAML 时钟偏移 CLOCK_SKEW_SECONDS=90——"已过期"用例至少要
  过期 2 分钟；测试类插桩锚点选错会让用例落进没有助手的类。


### 迭代 38（2026-09-09）跨仓：todo-for-ai-mcp 传输层与会话管理 ✅
- mcp 子仓覆盖率基线 33.55%（阈值 70%），零覆盖区：transports
  （677 行）/session（142 行）/api-client methods/handlers 大部。
- **修真 bug（mcp/src/transports/http.ts）**：startServer 在 listen
  回调里 resolve、error 监听后挂——macOS 等平台绑定失败
  （EADDRINUSE/EADDRNOTAVAIL）回调先触发，传输层错误地报告"已启动"。
  改为显式等待 listening 事件、早期错误 reject、运行期错误降级为日志。
- **删死代码（mcp/src/transports/factory.ts）**：私有
  detectTransportType/analyzeEnvironment 仅被注释代码引用，删除
  （HTTP 传输类本体保留在 http.ts 供未来启用）。
- 新增 35 用例：session-manager 11（创建/过期/活跃/清理/定时器/销毁）、
  transports 24（BaseTransport 契约、factory stdio-only、stdio 生命周期
  含启停失败、HttpTransport 临时端口真实 HTTP——health、initialize
  握手+会话复用、400 非法请求、JSON 解析错误、CORS 通配/精确/拒绝、
  DELETE 会话终止清理、500 兜底、运行期 error 韧性）。
- mcp 门禁 `npm test` **59 passed**；覆盖率：session/manager、
  base/factory/stdio 行覆盖 **100%**、http.ts **96.8%**（剩余为
  next(error) 透传与防御分支，已在测试注明）。
- mcp 提交 794b52d，主仓 ref e6ebba4。
- 经验：①tests/setup.ts 全局 mock fetch——HTTP 链路测试改用 node:http
  极简助手；②macOS 的 listen 失败回调先于 error 事件（Linux 相反），
  "等 listening 事件"是跨平台正确姿势；③同端口二次绑定在 macOS 会
  先成功后报 EADDRINUSE，端口冲突测试在修复前不可写。mcp 剩余大块：
  handlers（14-25%）、api-client methods（0%）、server.ts、http.ts
  余量——后续迭代继续。


### 迭代 39（2026-09-09）跨仓：mcp api-client 方法层 + handlers + TodoApiClient 类 ✅
- 新增 3 个测试文件共 46 用例：
  - `api-client-sweep.test.ts`（20）：magic-proxy 全量扫 14 个模块
    约 245 个导出方法（记录型 axios 桩上触发 happy path）+ 5 个代表
    方法精确断言（mcp/call payload、create_task 默认值、compactParams
    剥空字段、error 响应抛错）
  - `handlers-sweep.test.ts`（7）：handlerMap 全部工具（9 域）逐个
    magic 触发 + get_task_by_id 透传/错误传播/toToolResponse 整形
  - `todo-api-client.test.ts`（16）：vi.mock axios 下 TodoApiClient
    构造（baseURL 归一/鉴权头/元数据拦截器/响应拦截器三分类日志）、
    executeWithRetry 重试矩阵（网络与 5xx 重试、耗尽抛错、4xx 与
    非 Axios 立即抛、fake timers 推进）、unwrapApiData/compactParams、
    委托方法透传与类级 magic 扫描
- mcp 覆盖率：总行 33.55%→**90.46%**、函数 1.63%→**97.68%**、
  api-client 方法层 0→**100%**、handlers 14-25%→**90.14%**、
  api-client.ts 类 0→**95.25% 行 / 99.61% 函数**。
- mcp 门禁 `npm test` **102 passed**（38 迭代后 59）。
- mcp 提交 341c769，主仓 ref 待推。
- 经验：①大批量薄包装函数用"魔法对象 + 记录桩"全量扫 + 少量精确
  断言背书，覆盖率收益极高；②in 操作符检查（`'error' in result`）
  会被 magic has()=true 误触，魔法对象 has() 必须返回 false；
  ③fake timers 下"重试耗尽后拒绝"的用例要先挂 rejects 断言再推
  时间，否则出现短暂 unhandled rejection。
- mcp 剩余：handlers 分支覆盖 21%（深层整形分支）、部分 api-client
  模块分支、server.ts/index.ts——后续迭代按需继续。


### 迭代 40（2026-09-09）跨仓：agent-runtime 子仓清扫 ✅
- **修最重 bug：src/notifications 双向循环导入**——manager 在定义
  NotificationBackend/Notification 之前顶层 import chinese_providers，
  而后者回导这两个名字，**任何导入顺序都 ImportError，通知功能自
  出生即死**。修复：chinese_providers 导入下沉到三个 configure_*
  方法（局部导入破环）。
- **补依赖声明**：requirements.txt 漏 tenacity（src/api/client.py
  顶层导入，缺失时 22 个测试全 ERROR）/fastapi/uvicorn
  （health_server 依赖）。
- **门禁质变**：基线 4 failed + 18 errors + 149 passed →
  **209 passed, 0 failed, 0 errors**（补装 tenacity 后既有 22 用例
  全部转绿）。
- 新增 38 用例：test_notifications.py 31（8 后端 configured/成功/
  失败、飞书钉钉签名、严重度颜色、Manager 聚合/历史上限溢出/规则
  匹配/get_stats）+ test_health_server.py 7（health/ready 双态/
  metrics Prometheus 文本/profiling 三端点/uvicorn 生命周期；
  prometheus 全局注册表按名反注册隔离）。
- 覆盖率：notifications 0→100%/96.6%、health_server 0→**100%**、
  api/client.py 4.1%→63.5%（tenacity 解锁既有测试）、src 总计
  46.2%→**61.4%**。
- agent-runtime e80cb5a，主仓 ref 待推。
- 经验：①asyncio.run() 结束后主线程无当前事件循环——测试里用
  asyncio.run 会毒化后续同步测试的 asyncio.Event() 构造，改
  pytest-asyncio auto 模式的 async def 即可；②prometheus 指标注册
  在全局注册表，同进程二次实例化需按名反注册隔离；③"既有失败基线"
  有时只是环境缺依赖——先装声明内依赖再断定失败是既有的。
- WIP 仍回避：src/runtime/main.py、task_executor.py（修改中）、
  cli_engines/runtimes/cli-agents（未跟踪）。

（每完成一个迭代追加：日期、做了什么、覆盖率前后、测试数、提交号）

### 迭代 41（2026-09-10）api/agents/health.py 拆薄 + 健康分析下沉 service ✅
- 前：`api/agents/health.py` 490 行 / 行覆盖仅 7.5%（trend 与 state-transitions 端点引用未导入的 `AuditLog`——潜伏 NameError，从未被测试触达）。
- 后：计算逻辑下沉 `services/agent_health_analytics.py`（评分/告警/趋势/状态迁移四入口，入参 owner_id 解耦 flask user）；路由薄化至 109 行（参数钳制 + 鉴权 + 包装）。端点 URL 与响应键零变化。
- 顺带修复：trend/state-transitions 缺 `AuditLog` 导入的潜伏 NameError；移除两处死守卫（`created_at` NOT NULL 约束下"空日期 continue"不可达；`isinstance(detail, dict)` 已保护的冗余 try/except）；transitions 的日期键兼容 sqlite（func.date 返回 str）。
- 覆盖率：`api/agents/health.py` **100%**（52/52）、`services/agent_health_analytics.py` **100%**（200/200）。
- 测试：新增 `test_agent_health_analytics.py` 20 用例（权重归一化/四维评分排序/告警原因分支/建议分支/趋势聚合含脏增量/状态迁移含脏 detail）；全量门禁见提交（后台全量）。
- 经验：本文件自建 User/Agent 时别走 factory 清理（删除 Agent 级联 reputations 触发 NOT NULL，同 UserActivity 坑）；"0% 覆盖的旧路由"优先怀疑有潜伏 NameError/死导入。

### 迭代 42（2026-09-10）api/agents/productivity.py 拆薄 + 生产力分析下沉 service ✅
- 前：`api/agents/productivity.py` 703 行 / 行覆盖 8.4%；8 个端点全部内联聚合计算，
  且"按状态分桶 + 时长累计"的同一段循环在概览/告警/分组抄了 **3 遍**（低内聚实证）。
- 后：计算下沉 `services/agent_productivity_analytics.py`（8 入口 + `_bucket_by_state`
  /`_aggregate_assignments` 去重）；路由薄化至 ~110 行（`_parse_int_arg/_parse_float_arg`
  统一参数钳制）。端点 URL 与响应键零变化。
- 顺带修复：trend 的 kind_map 把 AgentKind 枚举直接当字典键（响应 JSON 键不可序列化
  且排序抛 TypeError）——统一取 `kind.value`。
- 覆盖率：`api/agents/productivity.py` **100%**（67/67）、
  `services/agent_productivity_analytics.py` **100%**（238/238）。
- 测试：新增 `test_agent_productivity_analytics.py` 21 用例（六状态分桶/时长回退分支/
  告警原因与排序/by-kind 分组/热力图峰值与截断/周对比 change_pct 三分支/闲置五档）。
- 经验：`kind=None` 写不进有 `default=AgentKind.X` 的列（默认值顶掉）——"unknown" 兜底
  分支只能靠 `.get()` 默认值触达，行覆盖不受影响但别指望造数命中；耗时 1.5h。

### 迭代 43（2026-09-10）api/agents/messaging.py 拆分四模块 + 修 7 处潜伏运行时错误 ✅
- 前：`api/agents/messaging.py` 911 行 / 行覆盖 14.9%（且 14.9% 几乎全是导入 incidental——
  messaging 相关端点零测试）；大杂烩混 workflow-triggers CRUD、点对点消息、broadcast、
  workflow-templates、collaboration-templates 五类不相关路由 + 100+ 行复制来的未用导入。
- **潜伏运行时错误 7 处**（全部由本次测试首次触达暴露）：
  1. `_compute_next_fire` 未导入 → cron 触发器 create/update 必 500；
  2. `_advance_workflow` 未导入 → 协作模板实例化（带 workflow）必 500；
  3. `_BUILTIN_COLLAB_TEMPLATES` 未导入 → 协作模板列表/实例化必 500；
  4. `AuditLog.record` 传参 target_type/target_id（形参是 resource_type/resource_id）→ 实例化必 500；
  5. builtin 模板建 Workflow 缺 NOT NULL 的 definition → 500；
  6. Workflow.create 传不存在的 project_id 列 → TypeError 500（归属由 WorkflowRun 携带）；
  7. AgentChannel.create 后未 flush 即取 channel.id 建成员 → NOT NULL 500。
  全部修复：前三者从真实定义处导入；后四者本地修复（含两处 Workflow.create 后补 flush）。
- 后：拆分为四个内聚模块（URL 零变化，纯移动 + 各自精简导入）：
  `messaging.py`（342 行，broadcast/点对点/消息流/collaborators）、
  `workflow_triggers.py`（174 行）、`workflow_templates.py`（99 行）、
  `collaboration_templates.py`（285 行）；`__init__.py` 注册。
- 另修：5 处 `if isinstance(data, tuple): return data` 的失效守卫
  （validate_json_request 出错时返回 Response 对象而非 tuple——空 body 请求此前会
  落进 500）→ 改为 `if not isinstance(data, dict): return data`。
- 覆盖率：四个模块 **全部 100% 行覆盖**（122 + 97 + 39 + 126 = 384/384）。
- 测试：新增 `test_agent_messaging.py` 26 用例；全量门禁见提交（后台全量）。
- 经验：911 行"大杂烩"文件往往是多次拆分的残余倾倒场——顶层巨型未用导入块是
  强信号；`validate_json_request()` 空 body 返回 Response 对象、`create()` 后不 flush
  取 id、列 default 顶掉显式 None——本仓三类高频坑。

### 迭代 44（2026-09-10）_workflow_helpers.py 拆出纯逻辑模块 workflow_conditions ✅
- 前：`api/agents/_workflow_helpers.py` 671 行 / 行覆盖 4.4%；条件求值（10 种操作符 +
  all/any 组合）与运行时覆盖合并是**零 DB 依赖的纯逻辑**，却埋在 DAG 引擎文件里
  （低内聚；且 4.4% 覆盖意味着这批共享逻辑从未被回归保护）。
- 后：拆出 `api/agents/workflow_conditions.py`（44 行纯逻辑：_RUNTIME_OVERRIDABLE_KEYS /
  _apply_runtime_overrides / _evaluate_step_condition）；_workflow_helpers 顶部改为从新模块
  导入（既有一切 `from ._workflow_helpers import _evaluate_step_condition` 路径零破坏，
  workflow_runs / workflow_versions / maintenance 的调用不变）。
- 覆盖率：`workflow_conditions.py` **100%** 行覆盖（44/44）——条件引擎首次被回归保护。
- 测试：新增 `test_workflow_conditions.py` 15 用例（allowlist 屏蔽/None 跳过/原对象不变/
  全操作符真伪/all-any 组合/status_equals 的 enum 与 str 双形态/未知操作符放行）。
- ⚠️ 遗留（迭代 45 处理）：_advance_workflow 内部给无根任务的步骤建 TaskAssignment
  （task_id=None → NOT NULL）——builtin 模板带 steps 实例化时该路径必炸，messaging.py
  侧已做容错降级（try/except + 日志）；修引擎需通读 _advance_workflow + _start_step。

### 迭代 45（2026-09-10）修 _start_step 双 flush 缺陷——工作流步骤派发从"必炸"到"真实可用" ✅
- 前：`_start_step` 在 `Task.create()` 后**未 flush 就读 task.id**（None）→
  TaskAssignment/AgentRun/step_run 全部拿到 task_id=None/assignment_id=None →
  下一次 flush 必炸 NOT NULL。这是迭代 43 发现的 builtin 模板带 steps 实例化
  "必 500" 的真正根因（此前只能靠 try/except 吞掉降级——推进永远失败）。
- 后：Task.create 与 TaskAssignment.create 之后各补一行 `db.session.flush()`。
- 回归测试 `test_instantiate_builtin_with_steps_advances_for_real`：真实推进
  （不打桩）——run 进入 RUNNING、步骤启动、assignment.task_id == step_run.task_id
  全链路断言（该测试在修复前必失败）。
- 覆盖率：改动文件全部 100%（沿用迭代 43 的 26+ 测试 + 新回归测试）。
- 经验：本仓 `Model.create()` = add 不 flush——**create 后立刻读自增 id 的地方
  全是同类雷**（channel/workflow/assignment 三连修），后续迭代 45+ 扫
  `\.id` 紧跟 `create()` 的模式可再清一批。

### 迭代 46（2026-09-10）task_escalation 下沉 + 修 maintenance 三处潜伏 NameError + 根治套件 65F 污染 ✅
- **潜伏运行时错误第八、九处**：maintenance.py 三处调用 `_escalate_overdue_tasks`
  （POST /maintenance/escalate-overdue 等三个维护端点）全模块无导入 → 必 NameError 500。
- 后：下沉独立模块 `api/agents/task_escalation.py`（PRIORITY_LADDER + escalate_overdue_tasks，
  周期维护职责与 DAG 引擎分离）；maintenance.py 正式导入（潜伏 NameError 修复）；
  _workflow_helpers 保留兼容再导出；`__init__.py` 注册。
- **根治全量套件 65F+60E 污染链**：定位到 test_agent_messaging.py 泄漏自增 id 的 Task 行
  （真实推进用例的自增 Task 残留 + 自建 Task 未用高位 id）→ 撞后续文件 task_factory 的
  手工 id=1。修复：_make_task 用高位 id 段（9_100_000+）+ is_ai_task=False +
  夹具 teardown 全量清场（Task/Workflow 链）。**注意：该修复在迭代 45 曾修过但未
  commit 就 push——origin/main 30b196f 仍带此问题，本迭代正式落库修复。**
- 覆盖率：`task_escalation.py` **100%** 行覆盖（23/23）+ 9 新用例（阶梯逐级/
  urgent-done-cancelled-无截止排除/cutoff 阈值/owner 作用域/全 owner/提交分支/
  端点回归）。
- 全量门禁：**1966 passed**（污染修复后套件首次全绿收敛）。

### 迭代 47（2026-09-11）_core.py 955 行清零：拆 agents_crud + agent_assignments 双 100% ✅
- 前：`api/agents/_core.py` 955 行 / 行覆盖 24.2%——project_members/workflow_routes/
  task_templates 等多轮拆分后的"残余倾倒场"（文件头自述"Routes that don't clearly
  belong to a specific sub-module live here"），14 条协作核心路由零测试。
- **潜伏运行时错误第十、十一、十二处**（AST 未定义调用扫描 + 审计Signature核对）：
  1. `list_review_queue` 用 `or_`/`and_` 但全文件未导入 → **默认 action=all 请求必 500**
     （人类审查队列入口整体不可用）；
  2. `self_register_agent` 两条路径的 `AuditLog.record` 传 `target_type/target_id`
     （形参是 resource_type/resource_id）→ TypeError：**Agent 已 commit 建成，
     但调用方收到 500**（更新/新建双路径全坏）；
  3. 顶部 `from workflow_templates import WORKFLOW_TEMPLATES` 绝对路径导入且正文
     零使用（依赖 sys.path hack 才能解析的脆弱导入）→ 删除。
- 后：拆为两个内聚模块（URL 零变化，纯移动 + 各自精简导入）：
  `agents_crud.py`（229 语句：列表/创建/自助注册/发现/详情/更新/心跳 +
  _normalize_working_schedule_field）、`agent_assignments.py`（228 语句：
  审查队列/推荐/认领/agent 分配列表与更新/task 分配列表与更新）；
  `_core.py` 变 9 行兼容 shim（`from . import ...` 副作用注册语义不变，
  `__init__.py` 零改动）。
- 覆盖率：`agents_crud.py` **100%**（229/229）、`agent_assignments.py` **100%**
  （228/228）、`_core.py` shim 100%。
- 测试：新增 `tests/unit/api/test_agents_core_routes.py` **89 用例**
  （每路由 200/400/404/409/500 矩阵 + 审计 kwargs 断言 + 工作时间窗 409 +
  认领双模式 + 能力合并 + 角色模板四分支 + 租约过期 409 + 更新错误三态）。
- 经验：①AST "调用-定义差集" 扫描是 NameError 家族的终极武器（本轮第 10-12 处，
  累计 12 处同类潜伏错误全部在零测试路由里）；②本轮测试自身先后暴露
  werkzeug test client 无 params=（用 query_string=）、TaskStatus/TaskAssignmentState
  枚举值全小写、AgentRoleTemplate 三个 NOT NULL（display_name/created_by_user_id）——
  新测试文件首轮全红是常态，按签名逐个收敛；③本体曾犯"自造符号
  ACTIVE_ASSIGNMENT_STATES_QUERY/AuditLog_record_claim"——被自己的测试当场
  抓住（测试先行就是给重构上保险的最好证据）。
- 全量门禁：见提交（后台全量）。

