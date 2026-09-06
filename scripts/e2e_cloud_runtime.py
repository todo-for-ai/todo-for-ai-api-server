#!/usr/bin/env python3
"""云端 Agent 运行时真集群 E2E（Phase 2 验收）

前置（见 deploy/k8s/README.md）：
- kind 集群 todo4ai 已创建（deploy/k8s/kind-config.yaml），kubeconfig 指向它
- 命名空间 todo4ai-agents 已 apply（00-namespace.yaml / 01-rbac.yaml）
- E2E worker 镜像已构建并 load 进集群：
    docker build -f deploy/k8s/e2e/Dockerfile.e2e-worker \
      -t todo4ai/agent-runtime:latest ../agent-runtime
    kind load docker-image todo4ai/agent-runtime:latest --name todo4ai
- 本地后端 :50110 运行中（连接同一 MySQL），本机 kubeconfig 即 kind

链路（全真实 K8s + 全真实 HTTP）：
  1. 建工作区/项目/Agent（managed_runner + cli_engine=custom）+ AgentKey
  2. spawn API → 真实创建 Pod（AGENT_KEY 走 Secret）
  3. Pod Running 后创建 AI 任务 → Pod pull 协议领取 → custom 引擎执行 → commit
  4. 断言任务 done 且结果回写
  5. 空闲回收：回拨 attempt 时间 → recycle_idle_pods → Pod 被删除
  6. terminate API 兜底清理
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta

NAMESPACE = "todo4ai-agents"
API = "http://127.0.0.1:50110/todo-for-ai/api/v1"


def http(method, path, body=None, token=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


def wait_for(predicate, what, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(3)
    print(f"FAIL 等待超时: {what}")
    return False


def kubectl(args):
    import subprocess
    return subprocess.run(["kubectl"] + args, capture_output=True, text=True)


def main():
    ok = True

    # ── 环境自检 ──
    assert kubectl(["config", "current-context"]).stdout.strip().startswith("kind-"), \
        "kubectl 当前 context 不是 kind（见 deploy/k8s/README.md）"
    assert kubectl(["get", "namespace", NAMESPACE]).returncode == 0, \
        f"命名空间 {NAMESPACE} 不存在（kubectl apply -f deploy/k8s/00-namespace.yaml）"

    from app import create_app
    from core.config import Config
    from models import db, Agent, AgentTaskAttempt, Project, Task, TaskStatus, User
    from models.agent_key import AgentKey
    from models import Organization

    app = create_app()
    with app.app_context():
        # Pod 内回源地址：kind extraHosts 已把 host.docker.internal 指向宿主机
        Config.API_BASE_URL = "http://host.docker.internal:50110/todo-for-ai/api/v1"
        Config.K8S_AGENT_NAMESPACE = NAMESPACE

        # ── 1. 造工作区/项目/Agent/密钥 ──
        user = User(username="cloud_e2e_admin", email="cloud_e2e@local.test")
        existing = User.query.filter_by(username="cloud_e2e_admin").first()
        if existing:
            user = existing
        else:
            db.session.add(user)
            db.session.flush()
        org = Organization.query.filter_by(name="cloud-e2e-ws").first()
        if not org:
            org = Organization(name="cloud-e2e-ws", slug=f"cloud-e2e-{int(time.time())}",
                               owner_id=user.id)
            db.session.add(org)
        project = Project.query.filter_by(name="cloud-e2e-project").first()
        if not project:
            project = Project(name="cloud-e2e-project", owner_id=user.id,
                              organization_id=org.id)
            db.session.add(project)
        agent = Agent.query.filter_by(name="cloud-e2e-agent").first()
        if not agent:
            agent = Agent(
                name="cloud-e2e-agent",
                workspace_id=org.id,
                owner_id=user.id,
                creator_user_id=user.id,
                status="ACTIVE",
                runner_enabled=True,
                execution_mode="managed_runner",
                sandbox_profile="minimal",
                sandbox_policy={"cli_engine": "custom", "network_mode": "whitelist"},
            )
            db.session.add(agent)
        db.session.commit()

        key_row = AgentKey.query.filter_by(agent_id=agent.id, is_active=True).first()
        if not key_row:
            key_row, _ = AgentKey.generate_key(
                name="cloud-e2e-key", workspace_id=org.id,
                agent_id=agent.id, created_by_user_id=user.id,
            )
            db.session.add(key_row)
            db.session.commit()
        token_rows = None
        print(f"1) 工作区 org={org.id} project={project.id} agent={agent.id} 就绪")

        # 平台 JWT（走真实 HTTP 用）
        from flask_jwt_extended import create_access_token
        jwt = create_access_token(identity=str(user.id))

        # ── 2. spawn（真实 HTTP → 后端进程 → 真实 K8s Pod）──
        def spawn_runtime():
            resp = http(
                "POST", f"/workspaces/{org.id}/agents/{agent.id}/runtime/spawn",
                {"sandbox_profile": "minimal"}, token=jwt,
            )
            if resp[0] == 409:
                # 已有在岗 Pod（上次运行的遗留）→ 先终止再建
                http("POST", f"/workspaces/{org.id}/agents/{agent.id}/runtime/terminate",
                     token=jwt)
                time.sleep(3)
                resp = http(
                    "POST", f"/workspaces/{org.id}/agents/{agent.id}/runtime/spawn",
                    {"sandbox_profile": "minimal"}, token=jwt,
                )
            return resp

        status, body = spawn_runtime()
        assert status == 200, f"spawn 失败: {status} {body}"
        pod_name = body["data"]["pod"]["pod_name"]
        print(f"2) Pod 已创建: {pod_name}")

        assert wait_for(
            lambda: kubectl(["get", "pod", pod_name, "-n", NAMESPACE,
                             "--no-headers"]).returncode == 0,
            "pod 出现", 120,
        ), "Pod 未出现"
        assert wait_for(
            lambda: kubectl(["get", "pod", pod_name, "-n", NAMESPACE, "-o",
                             "jsonpath={.status.phase}"]).stdout.strip() == "Running",
            "pod Running", 240,
        ), "Pod 未进入 Running"
        print("3) Pod Running（AGENT_KEY 已从 Secret 注入）")

        # AGENT_KEY 不落明文：Pod spec 中 AGENT_KEY 必须来自 secretKeyRef
        pod_json = json.loads(kubectl(
            ["get", "pod", pod_name, "-n", NAMESPACE, "-o", "json"]).stdout
        )
        env_map = {e["name"]: e for e in
                   pod_json["spec"]["containers"][0]["env"]}
        assert "value" not in env_map["AGENT_KEY"], "AGENT_KEY 明文注入未消除！"
        assert env_map["AGENT_KEY"]["valueFrom"]["secretKeyRef"]["key"] == f"agent-{agent.id}"
        print("4) AGENT_KEY 来自 secretKeyRef，无明文 ✓")

        # ── 3. 建 AI 任务 → 等 Pod 领取并完成 ──
        task = Task(
            title="云端 E2E：由 kind Pod 执行",
            content="echo 云端协作验收",
            project_id=project.id,
            owner_id=user.id,
            is_ai_task=True,
            status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id
        print(f"5) AI 任务已建 task={task_id}，等待 Pod pull→execute→commit ...")

        # 轮询走真实 HTTP（进程内 ORM 在 MySQL REPEATABLE_READ 下看不到
        # 其他进程/Pod 的提交，快照会过期失效）
        def task_done():
            st, b = http("GET", f"/tasks/{task_id}", token=jwt)
            return st == 200 and (b.get("data") or {}).get("status") == "done"

        assert wait_for(task_done, "任务被 Pod 完成", 240), "任务未被完成"
        db.session.expire(task)
        db.session.expire(task)
        print(f"6) 任务已 done（云端全链路 spawn→pull→commit 打通）✓")

        # ── 4. 空闲回收 ──
        from services.workspace_runtime_policy import (
            recycle_idle_pods, set_workspace_runtime_setting,
        )
        from services.agent_runtime_controller import get_agent_controller

        set_workspace_runtime_setting(org.id, max_pods=5, idle_timeout_minutes=30)
        # 回拨 activity 时间使 Pod 变"空闲"（该 Agent 全部 attempt 收尾，
        # 含此前被强删 Pod 遗留的陈旧 ACTIVE 行）
        from models import AgentTaskAttemptState
        now = datetime.utcnow()
        AgentTaskAttempt.query.filter_by(agent_id=agent.id).update({
            'state': AgentTaskAttemptState.COMMITTED,
            'started_at': now - timedelta(hours=2),
            'ended_at': now - timedelta(hours=2),
        })
        db.session.commit()

        controller = get_agent_controller()
        result = recycle_idle_pods(controller)
        assert result["recycled"] >= 1, f"空闲回收未生效: {result}"
        print(f"7) 空闲回收生效: {result}")

        assert wait_for(
            lambda: kubectl(["get", "pod", pod_name, "-n", NAMESPACE]).returncode != 0,
            "Pod 被回收删除", 120,
        ), "回收后 Pod 仍存在"
        print("8) 空闲 Pod 已从集群删除 ✓")

        # ── 5. terminate API 兜底（重新 spawn 一次再终止，覆盖端点）──
        status, body = spawn_runtime()
        assert status == 200, body
        pod2 = body["data"]["pod"]["pod_name"]
        status, body = http(
            "POST", f"/workspaces/{org.id}/agents/{agent.id}/runtime/terminate",
            token=jwt,
        )
        assert status == 200, body
        assert wait_for(
            lambda: kubectl(["get", "pod", pod2, "-n", NAMESPACE]).returncode != 0,
            "terminate 删除 Pod", 120,
        ), "terminate 后 Pod 仍存在"
        print("9) terminate API 清理 Pod ✓")

    print("\n🎉 云端运行时真集群 E2E 全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
