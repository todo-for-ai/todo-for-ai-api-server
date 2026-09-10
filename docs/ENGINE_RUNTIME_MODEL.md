# 引擎 × 执行环境：两层模型设计（Engine vs Execution Environment）

状态：已实现（本文与代码同源）
范围：api-server 的 Agent 执行面 —— `services/runtime_env/`
关联：`docs/RUNTIME_ENV_PROVIDERS.md`（环境后端操作手册）、
`docs/CLOUD_AGENT_RUNTIME_DESIGN.md`（云端协作分阶段设计）

---

## 1. 两条正交的轴

Agent 的执行由两个互相独立的问题决定：

```
                 执行环境轴（跑在哪）                引擎轴（跑什么）
                 RuntimeProvider 后端               Engine 注册表
                ┌──────────────────────┐          ┌──────────────────────┐
                │ k8s        （Pod）    │          │ claude   Claude Code │
                │ docker     （容器）   │          │ codex    Codex CLI   │
                │ compose    （compose）│   ✕     │ opencode OpenCode    │
                │ baremetal  （进程）   │          │ openai / anthropic   │
                │ remote     （反连）   │          │ google / ollama …    │
                └──────────────────────┘          └──────────────────────┘
```

- **执行环境（Execution Environment）**：Agent 运行时的宿主。负责生命周期——
  创建、终止、状态、工作区配额、空闲回收。平台侧接口是
  `services/runtime_env/base.py::RuntimeProvider`（spawn / terminate /
  get_runtime_status / list_runtimes + ensure_runtime 模板方法）。
- **引擎（Engine）**：跑在运行时**内部**、真正执行任务的程序。负责任务级行为——
  调用哪个 CLI / SDK、用什么模型。daemon 侧实现在 agent-runtime
  `src/runtime/cli_engines.py`；平台侧的单一事实来源是
  `services/runtime_env/engines.py::EngineSpec` 注册表。

**引擎永远跑在执行环境里**：云端 Pod 和用户本机反连的 daemon 是同一份
agent-runtime 代码，内部按注入的 `CLI_AGENT_ENGINE` 调用 claude/codex/…
引擎。任何环境 × 任何引擎都是合法组合（矩阵而非层级）。

## 2. 引擎轴：注册表（engines.py）

```python
EngineSpec(key, label, kind, image, cli_flag, provider_aliases)
```

- `key`：归一化引擎键，即 Pod label `runtime-type` / 状态 dict `runtime_type`
  （openai / anthropic / google / ollama / claude / codex / opencode / custom）；
- `image`：运行时镜像，**所有环境后端共用**（cli 系引擎共用
  `todo4ai/agent-cli-agents:latest`）；
- 解析顺序（`resolve_engine(agent)`）：沙箱策略 `sandbox_policy.cli_engine`
  显式声明 > `llm_provider` 别名（anthropic|claude→anthropic、google|gemini→google、
  ollama|local→ollama）> 兜底 custom；
- `engine_task_env(agent)`：引擎/任务环境变量（LLM_*、SANDBOX_*、
  超时/心跳/并发、按需 CLI_AGENT_ENGINE），环境无关；
  凭据与回连地址由各环境后端自行注入（K8s 走 SecretKeyRef，docker 系走 `-e`）。

历史上引擎→镜像的映射在 manifests（K8s）与 docker_provider 各写一份，
现全部收敛到注册表；`manifests.RUNTIME_IMAGES` / `manifests.runtime_type()`
保留为向后兼容的派生导出。**新增引擎 = 加一个 EngineSpec，五个环境即刻可用。**

## 3. 执行环境轴：五个后端与按 Agent 解析

`RUNTIME_PROVIDER`（部署级默认后端）+ `get_runtime_provider_for_agent(agent)`
（按 Agent 执行模式解析）：

| Agent.execution_mode | 归属后端 | 生命周期由谁驱动 |
| --- | --- | --- |
| `managed_runner` | 部署级后端（k8s/docker/compose/baremetal） | 平台 spawn / terminate / 回收 |
| `external_pull`（默认，反连） | `remote` | daemon 自管，平台维护注册与状态视图 |

remote 后端把「远程连上来」归一到同一接口，语义映射：

| RuntimeProvider 方法 | remote 语义 |
| --- | --- |
| spawn | 注册：不创建进程，返回 Pending（等 daemon 反连） |
| terminate | 向在线 daemon 下发 `shutdown` 命令；离线 → False |
| get_runtime_status | 在线 Running / 已启用待反连 Pending / 其余 Unknown |
| list_runtimes | 全部非 managed_runner Agent |

在线判定：进程内 WS 连接注册表（`api/agent_runtime_websocket.py::
is_agent_connected`，断连即时感知）或 `last_seen_at` 心跳新鲜度
（`REMOTE_ONLINE_WINDOW_SECONDS`，默认 90s，多 worker 部署兜底）。

## 4. 数据流（统一后）

```
任务 → auto_assign（后端无关：窗口/预算/容量三门）→ 派发（WS push + pull 兜底）
     ↘ GoalLoop ensure_cloud_executor → get_runtime_provider_for_agent
                                          ├─ managed_runner → k8s/docker/... ensure_runtime（按需拉起）
                                          └─ 反连 → remote ensure_runtime（在线=已岗）
运行时管理 API（management）→ 状态/终止按 Agent 解析后端；列表聚合托管+反连两个平面
```

## 5. daemon 元数据上报（remote 状态的数据源）

daemon 在 WS 连接（auth payload）与心跳（`meta` 字段）可携带
`host / engine / version / os`，平台合并进 `agent.config['runtime_meta']`
（仅变化时写库），remote 状态的 `address` / `engine` 即来源于此。
不上报也不影响连接与派发——元数据是增强项，不是依赖项。

## 6. 扩展清单（对应「可扩展性」目标）

| 想接入 | 要做什么 |
| --- | --- |
| 新引擎（如 windsurf/aider） | `ENGINE_SPECS` 加 EngineSpec（+ agent-runtime 引擎实现） |
| Podman | docker 兼容 socket：`DOCKER_HOST` 指向 podman 即可复用 docker 后端（待实测）；或加后端类 |
| AWS ECS / Nomad | 新增一个 RuntimeProvider 实现（4 方法 + 守归一化契约） |
| 新的远程形态 | remote 后端已覆盖反连模型；变种只需调其语义映射 |

新增后端守则见 `docs/RUNTIME_ENV_PROVIDERS.md` 的接口契约一节。
