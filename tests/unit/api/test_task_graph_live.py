"""任务图实时刷新：task_graph_changed 推送挂点回归。

任务状态/依赖翻转改变 DAG 就绪态，后端在四个 choke point 推送
`task_graph_changed` 到 /user/ws 的项目房间（前端 TaskGraphTab 订阅后
自动重取图）：
- PUT /tasks/<id>/dependencies（依赖边编辑）
- POST /tasks/batch/update-status（批量状态，按项目分组）
- PUT /tasks/<id>（人工状态变更）
- POST /agent/tasks/<id>/commit（Agent 提交，多 Agent 执行的主推进时刻）
隔离方式与 test_task_graph.py / test_task_coauthoring.py 一致。
"""

import hashlib
import uuid
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app import create_app
from models import (
    Agent,
    AgentSession,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskLease,
    Organization,
    Project,
    Task,
    TaskStatus,
    User,
    db,
)

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    # 预热 agent_common 的表检查缓存（commit 链路 write_agent_audit 需要）
    from api.agent_common import _agent_activity_events_table_exists
    _agent_activity_events_table_exists()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(client):
    owner = User(username=f"tg_{uuid.uuid4().hex[:8]}",
                 email=f"tg_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(owner)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
    db.session.add(project)
    db.session.commit()
    return {
        "owner": owner,
        "project": project,
        "headers": {
            "Authorization": f"Bearer {create_access_token(identity=str(owner.id))}"
        },
    }


def _mk_task(env, status="todo", blocked_by=None):
    task = Task(
        title=f"t_{uuid.uuid4().hex[:6]}",
        content='{"prompt":"x"}',
        project_id=env["project"].id,
        owner_id=env["owner"].id,
        is_ai_task=True,
        status=TaskStatus(status),
        blocked_by_task_ids=blocked_by or [],
        dod=[],
    )
    db.session.add(task)
    db.session.commit()
    return task


@pytest.fixture
def agent_context(_isolated_app):
    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        creator_user_id=user.id,
        status="ACTIVE",
        runner_enabled=True,
    )
    db.session.add(agent)
    db.session.flush()
    raw_token = f"sess_{uuid.uuid4().hex}"
    db.session.add(AgentSession(
        agent_id=agent.id,
        workspace_id=org.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        token_prefix=raw_token[:16],
        expires_at=datetime.utcnow() + timedelta(hours=1),
        is_active=True,
    ))
    db.session.commit()
    return {"user": user, "org": org, "agent": agent,
            "headers": {"Authorization": f"Bearer {raw_token}"}}


@pytest.fixture
def leased_task(agent_context):
    ctx = agent_context
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=ctx["user"].id,
                      organization_id=ctx["org"].id)
    db.session.add(project)
    db.session.flush()
    task = Task(
        title="图刷新提交任务",
        content="# 需求\n\n实现登录页",
        project_id=project.id,
        owner_id=ctx["user"].id,
        is_ai_task=True,
        status="IN_PROGRESS",
    )
    db.session.add(task)
    db.session.flush()
    attempt_id = f"att_{uuid.uuid4().hex[:8]}"
    lease_id = f"lea_{uuid.uuid4().hex[:8]}"
    db.session.add(AgentTaskAttempt(
        attempt_id=attempt_id,
        task_id=task.id,
        agent_id=ctx["agent"].id,
        workspace_id=ctx["org"].id,
        state=AgentTaskAttemptState.ACTIVE,
        lease_id=lease_id,
        started_at=datetime.utcnow(),
        created_by="test",
    ))
    db.session.add(AgentTaskLease(
        lease_id=lease_id,
        task_id=task.id,
        attempt_id=attempt_id,
        agent_id=ctx["agent"].id,
        workspace_id=ctx["org"].id,
        expires_at=datetime.utcnow() + timedelta(seconds=120),
        active=True,
        created_by="test",
    ))
    db.session.commit()
    return {"task": task, "attempt_id": attempt_id, "lease_id": lease_id}


class TestDependencyEditNotify:
    def test_put_dependencies_notifies(self, client, env, monkeypatch):
        calls = []
        import api.tasks.routes_batch as mod
        monkeypatch.setattr(mod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        a = _mk_task(env)
        b = _mk_task(env)
        resp = client.put(f"{BASE_URL}/tasks/{a.id}/dependencies",
                          headers=env["headers"],
                          json={"blocking_task_ids": [], "blocked_by_task_ids": [b.id]})
        assert resp.status_code == 200
        assert calls == [(env["project"].id, [a.id], "dependencies_changed")]

    def test_rejected_cycle_does_not_notify(self, client, env, monkeypatch):
        calls = []
        import api.tasks.routes_batch as mod
        monkeypatch.setattr(mod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        a = _mk_task(env)
        b = _mk_task(env, blocked_by=[a.id])  # B 依赖 A（边 A→B）
        resp = client.put(f"{BASE_URL}/tasks/{a.id}/dependencies",
                          headers=env["headers"],
                          json={"blocking_task_ids": [], "blocked_by_task_ids": [b.id]})
        assert resp.status_code == 400
        assert calls == []

    def test_batch_status_groups_by_project(self, client, env, monkeypatch):
        calls = []
        import api.tasks.routes_batch as mod
        monkeypatch.setattr(mod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        t1 = _mk_task(env)
        t2 = _mk_task(env)
        # value 形式（'done'）：此前批量接口裸赋值会 Enum KeyError 500
        resp = client.post(f"{BASE_URL}/tasks/batch/update-status",
                           headers=env["headers"],
                           json={"task_ids": [t1.id, t2.id], "status": "done"})
        assert resp.status_code == 200
        assert calls == [(env["project"].id, [t1.id, t2.id], "batch_status_changed")]

    def test_batch_status_invalid_value_rejected(self, client, env, monkeypatch):
        calls = []
        import api.tasks.routes_batch as mod
        monkeypatch.setattr(mod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        t1 = _mk_task(env)
        resp = client.post(f"{BASE_URL}/tasks/batch/update-status",
                           headers=env["headers"],
                           json={"task_ids": [t1.id], "status": "warp"})
        assert resp.status_code == 400
        assert calls == []


class TestHumanUpdateNotify:
    def test_status_change_notifies(self, client, env, monkeypatch):
        calls = []
        import api.tasks.routes_tasks as mod
        monkeypatch.setattr(mod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        task = _mk_task(env)
        resp = client.put(f"{BASE_URL}/tasks/{task.id}",
                          headers=env["headers"], json={"status": "done"})
        assert resp.status_code == 200
        assert calls == [(env["project"].id, [task.id], "status_changed")]

    def test_non_status_change_does_not_notify(self, client, env, monkeypatch):
        calls = []
        import api.tasks.routes_tasks as mod
        monkeypatch.setattr(mod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        task = _mk_task(env)
        resp = client.put(f"{BASE_URL}/tasks/{task.id}",
                          headers=env["headers"], json={"title": "只改标题"})
        assert resp.status_code == 200
        assert calls == []


class TestAgentCommitNotify:
    def test_commit_notifies_with_final_status(self, client, env, agent_context, leased_task, monkeypatch):
        calls = []
        import api.user_websocket as wsmod
        monkeypatch.setattr(wsmod, "notify_task_graph_changed",
                            lambda pid, ids, reason: calls.append((pid, ids, reason)))
        task = leased_task["task"]
        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/commit",
            json={
                "attempt_id": leased_task["attempt_id"],
                "lease_id": leased_task["lease_id"],
                "status": "succeeded",
                "result": {"output": "完成", "processed_by": "claude", "metadata": {}},
            },
            headers=agent_context["headers"],
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        pid, ids, reason = calls[0]
        assert pid == task.project_id
        assert ids == [task.id]
        assert reason == "agent_commit:succeeded"
