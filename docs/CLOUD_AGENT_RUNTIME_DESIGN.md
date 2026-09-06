# 云端 Agent 运行时与多 Agent 协作 — 现状、差距与分阶段设计

状态：设计稿 v1（2026-09-06）
范围：api-server 的云端执行面（K8s 控制器、派发协议、编排联动）+ agent-runtime 的沙箱镜像。
定位：这是**周级**工程，本文把它拆成可独立验收的阶段；每个阶段合并时必须有测试与真实验证证据。
本文回答三个问题：已经有什么、还缺什么、每一阶段交付什么。

---

## 1. 现状盘点（截至本文撰写时）

### 1.1 已有且可用

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| K8s Pod 控制器 | api-server `services/agent_runtime_controller.py` | spawn/terminate/status/list；gVisor runtimeClass；non-root + 只读 rootfs + drop ALL caps；三档资源（minimal/standard/performance）；liveness/readiness 探针；每 Agent 一 Pod（label `agent-id`/`workspace-id`） |
| 运行时管理 API | api-server `api/agent_runtime_mgmt.py` | spawn/terminate/status 端点；AgentKey 自动生成、加密落库、`reveal()` 注入 |
| 拉取协议 | api-server `api/agent_runtime_pull.py` + agent-runtime `src/runtime/platform_client.py` | HTTP pull + 租约（AgentTaskAttempt/AgentTaskLease）+ 心跳续期 + 幂等提交 + DoD 证据 |
| Runtime 执行器 | agent-runtime `src/runtime/` | OpenClaw 与 CLI 双模式；CLI 引擎 claude/codex/opencode/custom；任务级隔离工作区（repo_workspace）；DoD 验证器；健康/指标端口 |
| 沙箱镜像 | agent-runtime `runtimes/{openai,claude,gemini,ollama,cli-agents}/Dockerfile` + docker-compose | 本地 Docker 三步接入；cli-agents 镜像预装三 CLI |
| 编排引擎（本地验证） | api-server `services/goal_loop_service.py` | 指挥者拆解评审、计划步骤岗位路由执行者、时长/轮数/受阻三护栏、看门狗自愈 |
| 派发通道 | WebSocket push + HTTP pull 双通道；attempt/lease 租约 | auto_assign 兜底 + 编排定向派发 |

### 1.2 关键差距（按对"多 Agent 云端协作"的影响排序）

| # | 差距 | 影响 | 阶段 |
| --- | --- | --- | --- |
| G1 | **AGENT_KEY 明文注入 Pod env**（`_build_env_vars` 直接 `value=agent_key`，代码注释自认不安全） | 凭据泄漏面：Pod spec/日志/etcd 可见明文 | P1 |
| G2 | **Agent Pod 之间零共享**：卷只有 emptyDir（tmp/cache），协作 Agent 的产物只能走平台任务文本往返 | 多 Agent 干同一目标时无法共享 repo/工作产物，协作名存实亡 | P1 |
| G3 | **编排不感知云端执行**：GoalLoop 路由到某 Agent 时，若其 Pod 未运行，任务派发后无人领取（本地 external_pull 模式才有真人接） | "云端一群 Agent 协作"缺了最关键的一环：按需把执行者拉起来 | P1 |
| G4 | **镜像映射断档**：`_get_runtime_type` 只看 llm_provider；CLI 引擎 Agent（claude/codex/opencode）会拿到错误的镜像，且 `CLI_AGENT_ENGINE` 不随 Pod 注入 | 云端 CLI Agent 根本起不来或引擎不对 | P1 |
| G5 | **无工作区级并发/配额**：一群 Pod 无上限，成本失控无护栏 | 多 Agent 规模化的前提 | P2 |
| G6 | **无生命周期回收**：空闲 Pod 永续运行，无 idle timeout / 伸缩 | 成本 | P2 |
| G7 | **无集群级 E2E**：控制器只有 fake 单测；没有 kind/k3d 的真集群验证与部署清单 | 正确性证据缺失 | P2 |
| G8 | **无观测面**：Pod 事件/重启/OOM 不回流平台审计；无每任务成本记录 | 运维黑盒 | P3 |

### 1.3 为什么这是一个周级而不是小时级工程

- 每个缺口都横跨"控制器 + 镜像 + 协议 + 权限"四层，且都要在真集群上验证（gVisor、PVC 的 ReadWriteMany 存储类、Secret 权限都是集群能力，fake 测不出来）。
- 凭据与配额改的是安全边界，动错就是事故；需要分阶段上线（先 Secret 化、再配额、再回收），不能一把梭。
- 多 Agent 协作的正确形态（共享卷 vs 对象存储 vs 平台中转、Pod 粒度 vs 池化）依赖实际负载观测，需要 P1 落地后按证据演进。

---

## 2. 目标架构（北极星）

```
                       ┌────────────────────────────────────────────┐
                       │ api-server（控制面）                        │
                       │  GoalLoop 编排（指挥者/路由/护栏/看门狗）    │
                       │  RuntimeController（spawn/回收/配额）       │
                       │  派发协议（pull + lease + 幂等提交）        │
                       └──────┬─────────────────────┬───────────────┘
                              │ ensure-runtime      │ pull/push 任务
                 ┌────────────▼─────────┐   ┌───────▼───────────────┐
                 │ K8s Namespace        │   │ Runtime Pod（每 Agent）│
                 │  Secret: runtime-keys│◄──┤  AGENT_KEY(SecretRef) │
                 │  PVC: ws-<id>-shared │──►│  /workspace/shared(RWX)│
                 │  Pod: agent-<id>-xxx │   │  CLI 引擎 + DoD 验证   │
                 └──────────────────────┘   └────────────────────────┘
```

原则：
1. **控制面只做编排与记账，数据面只做执行**——Agent 之间不直连，协作通过共享卷（文件产物）+ 平台任务（意图与结论）两个通道。
2. **一切凭据走 Secret，一切容量走配额**。
3. **每个阶段独立可验收**：fake 单测保逻辑，真集群 E2E 保集成（P2 起）。

---

## 3. 阶段计划

### Phase 1 — 云端协作的最小闭环（本轮）
目标：让"指挥者在平台拆解、多个云端 Agent Pod 各司其职、共享工作区、凭据安全"跑通。

- **F1 Secret 化凭据**：spawn 前确保 per-workspace Secret `todo4ai-runtime-keys` 存在（key = `agent-<id>`，值为 AgentKey 明文）；Pod env `AGENT_KEY` 改 `secretKeyRef`。消灭明文。
- **F2 共享工作区卷**：`ensure_workspace_shared_pvc(workspace_id)`（PVC `todo4ai-ws-<id>-shared`，ReadWriteMany，storageClass 可配）；`_build_pod` 按 `sandbox_policy.shared_workspace` 挂载到 `/workspace/shared`。协作 Agent 对同一工作区共享文件产物。
- **F3 编排↔云端联动**：GoalLoop 物化任务派发前，对 `execution_mode='managed_runner'` 的执行者调用 `ensure_agent_pod`（幂等：Running/Pending 即跳过）；工作区运行 Pod 数上限 `MAX_PODS_PER_WORKSPACE`（默认 10）超限记审计不 spawn（任务留队列等重试/看门狗）；联动失败不阻塞派发（降级 external_pull 并记原因）。
- **F4 CLI 引擎镜像映射**：`runtime_type` 支持 `claude/codex/opencode`（→ runtimes/cli-agents 构建的镜像）；Pod 注入 `CLI_AGENT_ENGINE`（取 `sandbox_policy.cli_engine`）。
- 验收：新增单测覆盖以上四点（fake k8s）；`pytest tests/unit` 全绿；本地 spawn 路径 dry-run 校验（无集群时以单测为准，集群验证挂 P2 E2E）。

### Phase 2 — 配额、回收与真集群验证
- 工作区配额面（max pods / 月度成本估算 API）+ 前端可视化；
- 空闲回收（idle timeout → scale-to-zero，事件回流审计）；
- kind/k3d 部署清单 + 真集群 E2E（spawn→pull→commit→terminate 全链路）。

### Phase 3 — 观测与成本
- Pod 事件（OOMKilled/CrashLoop/驱逐）回流 agent_audit_events；
- 每 Pod×每任务成本记账（资源×时长），预算系统（Budget）接入 Pod 生命周期。

### Phase 4 — 规模化形态演进
- Pod 池化（预热池 + 归还）替代按 Agent 独占；
- 多集群/多区域调度；对象存储产物通道（大文件不走 PVC）。
- 触发条件：P2 的真实负载证据，不预先设计。

---

## 4. 变更记录
- 2026-09-07 v2：**Phase 2 交付**——工作区运行时配额（workspace_runtime_settings 表 + GET/PUT API + 控制器接入）、空闲 Pod 自动回收（recycle_idle_pods 挂入看门狗调度）、kind 真集群 E2E（deploy/k8s/ 部署清单 + scripts/e2e_cloud_runtime.py：spawn→pull→commit→空闲回收→terminate 九步断言全过）。真集群 E2E 当场抓出并修复三个问题：RuntimeClass "gvisor" 硬编码（kind 上直接 403）、imagePullPolicy Always 破坏本地镜像、spawn 409 分支 ApiResponse.error 位置参数 TypeError。
- 2026-09-06 v1：初稿；Phase 1（F1-F4）随本文同批落地。
