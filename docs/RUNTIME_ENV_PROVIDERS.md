# Agent 运行时环境后端（RuntimeProvider）

Agent 运行时的执行环境已抽象为可插拔后端。调用方（运行时管理 API、GoalLoop
编排联动、空闲回收看门狗）只依赖 `services/runtime_env/base.py` 的
`RuntimeProvider` 接口，不感知后端差异。

引擎（Engine，跑什么）与执行环境（跑在哪）是两条正交的轴——引擎注册表、
解析规则与两层模型全文见 `docs/ENGINE_RUNTIME_MODEL.md`。

## 切换后端

`RUNTIME_PROVIDER` 环境变量（`core/config.py`，默认 `k8s`）：

| 后端 | 场景 | 关键配置 |
| --- | --- | --- |
| `k8s` | 云端/集群，多 Agent 协作主力 | 既有 K8s 控制器行为（Secret 注入、共享 PVC、gVisor 可选、Pod 配额与空闲回收）；需要 `kubernetes` 包与集群凭据 |
| `docker` | 单机 Docker | `DOCKER_RUNTIME_IMAGE`（默认 todo4ai/agent-runtime:latest）、`DOCKER_RUNTIME_NETWORK`、`DOCKER_API_BASE_URL`（容器回连平台地址，如 http://host.docker.internal:50110/todo-for-ai/api/v1） |
| `compose` | 单机 + 要求编排定义可审计/可手工接管 | 继承 docker 的全部配置；compose 文件落在 `COMPOSE_RUNTIME_DIR`（默认 /tmp/todo4ai-compose），每 Agent 一份、可手工修改后 `docker compose up` 接管 |
| `baremetal` | 无容器环境的可信物理机/裸虚机 | `BAREMETAL_RUNTIME_COMMAND`（如 "python -m runtime.main"）与 `BAREMETAL_RUNTIME_CWD` 必配，未配置时拒绝 spawn；`BAREMETAL_STATE_DIR` 存进程注册表。⚠️ 无隔离，仅限单租户可信环境 |
| `remote` | 远程反连：agent-runtime daemon 跑在用户自己的机器上 | 平台不创建进程；`REMOTE_ONLINE_WINDOW_SECONDS`（心跳新鲜窗，默认 90s）。按 Agent 执行模式自动生效（`execution_mode != 'managed_runner'`），无需全局切换 |

## 按 Agent 解析

`get_runtime_provider_for_agent(agent)`：`managed_runner` → 部署级全局后端；
其余（反连）→ `remote`。运行时管理 API 的状态/终止与 GoalLoop 编排联动
（`ensure_cloud_executor`）均按此解析；实例列表聚合托管与反连两个平面。
`auto_assign_task` 派发始终与后端无关。

## 接口契约（归一化）

- 状态 dict：`runtime_id / agent_id / workspace_id / runtime_type / phase /
  address / started_at / name`；`phase ∈ Running|Pending|Succeeded|Failed|Unknown`；
  「在岗」= `phase ∈ (Running, Pending)`；
- `ensure_runtime(agent, agent_key, sandbox_profile)` →
  `{'status': 'already_running'|'created'|'workspace_pod_limit', 'runtime': ...}`；
- 工作区实例上限（`max_pods` 设置）由基类模板方法统一执行，各后端行为一致
  （remote 后端 `runtime_type` 为引擎键，附加 `environment='remote'`/
  `online`/`last_seen_at`/`engine` 扩展字段）。

## 兼容性

- `AgentRuntimeController` 即 k8s 后端实现（`get_agent_controller()` 继续可用），
  派发逻辑 `auto_assign_task` 与后端无关、不受影响；
- `kubernetes` 包改为惰性导入——docker/compose/baremetal/remote 部署无需安装它；
- 回收看门狗 `recycle_idle_pods(provider)` 对任意后端生效（remote 无进程可回收，
  天然为空操作）；
- 引擎→镜像映射收敛到 `services/runtime_env/engines.py` 注册表；
  `manifests.RUNTIME_IMAGES` / `runtime_type()` 保留为派生导出（逐键一致）。

