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
（每完成一个迭代追加：日期、做了什么、覆盖率前后、测试数、提交号）
