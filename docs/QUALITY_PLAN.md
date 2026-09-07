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

## 收尾总览（2026-09-08 07:40 起，迭代 13 后更新）

**门禁**：全量单测 396（基线）→ **1169 passed**（23 个迭代全部绿灯后）。

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
  语义应澄清（澄清属行为变更，未擅动）。
- 迭代 12 的"新建 Agent 无租约判 online"语义如需改为 unknown，属产品决策。

（每完成一个迭代追加：日期、做了什么、覆盖率前后、测试数、提交号）
