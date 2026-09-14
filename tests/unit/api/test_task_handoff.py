"""依赖交接（task handoff）：上游产出注入 pull payload + 终态解锁事件。

多 Agent 按 blocked_by 接力时的两个交接动作：
- 下游任务被领取时，payload.upstream 自动携带上游任务（已终态阻塞者）
  的 shared_context，产出无需下游显式拉取；
- 任务 commit 到终态后，刚解锁的下游任务时间线出现 dependency.unlocked
  事件，commit 响应回传 unlocked_downstream。
认证与隔离方式与 test_dependency_gate.py 一致。
"""

import uuid

import pytest

from app import create_app

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    from models import db
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def runtime_ctx(_isolated_app, client):
    from models import Agent, AgentKey, AgentStatus, Organization, Project, User, db

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id)
    db.session.add(project)
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        owner_id=user.id,
        creator_user_id=user.id,
        runner_enabled=True,
        status=AgentStatus.ACTIVE,
    )
    db.session.add(agent)
    db.session.commit()

    key_row, raw_key = AgentKey.generate_key(
        name=f"Runtime Key {uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        agent_id=agent.id,
        created_by_user_id=user.id,
    )
    db.session.add(key_row)
    db.session.commit()

    auth_resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
    assert auth_resp.status_code == 200
    token = auth_resp.get_json()["data"]["access_token"]

    return {
        "user": user, "org": org, "agent": agent, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _make_task(ctx, status="todo", blocked_by=None):
    from models import Task, TaskStatus, db
    task = Task(
        title=f"t_{uuid.uuid4().hex[:6]}",
        content='{"prompt":"x"}',
        project_id=ctx["project"].id,
        owner_id=ctx["org"].id,
        is_ai_task=True,
        status=TaskStatus(status),
        blocked_by_task_ids=blocked_by or [],
        dod=[],
    )
    db.session.add(task)
    db.session.commit()
    return task


def _put_shared_context(ctx, task, entries):
    from models import SharedContext, db
    for key, value in entries.items():
        db.session.add(SharedContext(
            task_id=task.id, key=key, value=value,
            author_agent_id=ctx["agent"].id,
        ))
    db.session.commit()


def _pull(client, ctx, max_tasks=1):
    resp = client.post(f"{BASE_URL}/agent/tasks/pull",
                       json={"max_tasks": max_tasks}, headers=ctx["headers"])
    assert resp.status_code == 200
    return resp.get_json()["data"]


def _commit(client, ctx, pulled):
    resp = client.post(
        f"{BASE_URL}/agent/tasks/{pulled['task_id']}/commit",
        json={
            "attempt_id": pulled["attempt_id"],
            "lease_id": pulled["lease_id"],
            "status": "succeeded",
            "result": {"output": "done"},
        },
        headers={**ctx["headers"], "Idempotency-Key": pulled["attempt_id"]},
    )
    assert resp.status_code == 200
    return resp.get_json()["data"]


# ── 上游产出注入 pull payload ─────────────────────────────────────────


class TestUpstreamContextInjection:
    def test_upstream_shared_context_injected(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="done")
        _put_shared_context(runtime_ctx, blocker, {
            "research_summary": "结论：用方案B",
            "code_plan": "1. 建表 2. 写接口",
        })
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])

        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]
        upstream = data["tasks"][0]["upstream"]
        assert upstream == [{
            "task_id": blocker.id,
            "title": blocker.title,
            "shared_context": {
                "research_summary": "结论：用方案B",
                "code_plan": "1. 建表 2. 写接口",
            },
        }]

    def test_no_upstream_output_no_field(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="done")
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])

        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]
        assert "upstream" not in data["tasks"][0]

    def test_free_task_has_no_upstream_field(self, client, runtime_ctx):
        _make_task(runtime_ctx, status="todo")
        data = _pull(client, runtime_ctx)
        assert len(data["tasks"]) == 1
        assert "upstream" not in data["tasks"][0]

    def test_value_truncated_and_keys_capped(self, client, runtime_ctx):
        from services.task_handoff import (
            _UPSTREAM_KEYS_PER_TASK,
            _UPSTREAM_VALUE_MAX_CHARS,
        )

        blocker = _make_task(runtime_ctx, status="done")
        _put_shared_context(
            runtime_ctx, blocker,
            {f"k{i}": "v" * (_UPSTREAM_VALUE_MAX_CHARS + 500)
             for i in range(_UPSTREAM_KEYS_PER_TASK + 5)},
        )
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])

        data = _pull(client, runtime_ctx)
        upstream = data["tasks"][0]["upstream"]
        context = upstream[0]["shared_context"]
        assert len(context) == _UPSTREAM_KEYS_PER_TASK
        assert all(len(v) == _UPSTREAM_VALUE_MAX_CHARS for v in context.values())


# ── 终态解锁事件（dependency.unlocked）─────────────────────────────────


class TestUnlockNotification:
    def test_commit_done_unlocks_downstream_event(self, client, runtime_ctx):
        from models import TaskEvent, db

        blocker = _make_task(runtime_ctx, status="todo")
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])

        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [blocker.id]

        commit_data = _commit(client, runtime_ctx, data["tasks"][0])
        assert commit_data["unlocked_downstream"] == [dependent.id]

        events = TaskEvent.query.filter_by(
            task_id=dependent.id, event_type="dependency.unlocked").all()
        assert len(events) == 1
        assert events[0].payload["unblocked_by_task_id"] == blocker.id
        assert events[0].payload["unblocked_by_status"] == "done"

    def test_commit_keeps_dependent_held_by_other_blocker(self, client, runtime_ctx):
        from models import TaskEvent

        other_blocker = _make_task(runtime_ctx, status="todo")
        blocker = _make_task(runtime_ctx, status="todo")
        _make_task(runtime_ctx, status="todo",
                   blocked_by=[blocker.id, other_blocker.id])

        data = _pull(client, runtime_ctx)
        # 最新优先：blocker 更新，先被领取
        assert [t["task_id"] for t in data["tasks"]] == [blocker.id]

        commit_data = _commit(client, runtime_ctx, data["tasks"][0])
        # 另一前置仍在 todo，下游未解锁，无事件
        assert commit_data["unlocked_downstream"] == []
        assert TaskEvent.query.filter_by(event_type="dependency.unlocked").count() == 0

    def test_no_dependent_commit_reports_empty(self, client, runtime_ctx):
        task = _make_task(runtime_ctx, status="todo")
        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [task.id]
        commit_data = _commit(client, runtime_ctx, data["tasks"][0])
        assert commit_data["unlocked_downstream"] == []
