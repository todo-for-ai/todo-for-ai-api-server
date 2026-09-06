# 云端 Agent 运行时 — 本地真集群验证环境（kind / colima-k3s）

## 路线 A：kind（需要 docker.io 可直连， kindest/node 镜像 ~1GB）
```bash
kind create cluster --name todo4ai --config deploy/k8s/kind-config.yaml
kubectl apply -f deploy/k8s/00-namespace.yaml -f deploy/k8s/01-rbac.yaml
```

## 路线 B（本机实测可用）：colima + k3s
docker.io 的 kindest/node 在部分网络拉不动；colima 的 VM 走 GitHub 下载 k3s：
```bash
colima start --kubernetes            # 首次数分钟，VM 有缓存时更快
kubectl config use-context colima    # colima 自动写入 kubeconfig
kubectl get nodes                    # k3s 单节点 Ready
```

## E2E worker 镜像（两个集群通用）
```bash
# 构建（Desktop/colima 任一 daemon 均可）
docker build -f deploy/k8s/e2e/Dockerfile.e2e-worker \
  -t todo4ai/agent-runtime:latest ../agent-runtime

# 跨 daemon 导入：Docker Desktop 构建 → colima k3s 使用
docker save todo4ai/agent-runtime:latest -o /tmp/e2e-worker.tar
colima cp /tmp/e2e-worker.tar default:/tmp/e2e-worker.tar   # colima >= 0.6
colima ssh default -- sudo k3s ctr images import /tmp/e2e-worker.tar
# kind 集群则用：kind load docker-image todo4ai/agent-runtime:latest --name todo4ai
```

## 真集群 E2E
```bash
kubectl apply -f deploy/k8s/00-namespace.yaml     # todo4ai-agents 命名空间
todo-for-ai-api-server/.venv/bin/python \
  todo-for-ai-api-server/scripts/e2e_cloud_runtime.py
```
链路断言：spawn（AGENT_KEY 走 SecretKeyRef，无明文）→ Pod Running →
平台建 AI 任务 → Pod pull 协议领取 → custom 引擎执行 → commit → 任务 done →
空闲回收删除 Pod → terminate API 兜底清理。

## 清理
```bash
kind delete cluster --name todo4ai   # 路线 A
colima kubernetes delete / colima stop  # 路线 B
```
