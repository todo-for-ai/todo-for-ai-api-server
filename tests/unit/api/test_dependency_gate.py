"""依赖感知派发（dependency gate）：pull 路径按 blocked_by 任务图顺序派发。

epic 展开/批量编辑写入的 blocked_by 依赖此前只有写入与展示，派发完全不消费——
多 Agent 并发拉取时会提前领到前置未完成的任务，任务图执行顺序失效。
本文件覆盖：终态解除语义（DONE/CANCELLED）、失效引用与脏数据容忍、
依赖跳过可观测性（dependency_gate）、绕行派发与多轮派发、解除后恢复。
认证与隔离方式与 test_pull_branches.py 一致：独立 in-memory SQLite +
introspect 换取会话令牌。
"""

import uuid
from datetime import datetime, timedelta

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
    """introspect 换取会话令牌 + 基础数据（owner/org/agent/project，手工建行）。"""
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
        "raw_key": raw_key,
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


def _lease_task(ctx, task):
    """给任务挂活跃租约，模拟另一个 Agent 正在执行（典型 epic 并行场景）。"""
    from models import AgentTaskLease, db
    db.session.add(AgentTaskLease(
        lease_id=f"lea_{uuid.uuid4().hex[:6]}",
        task_id=task.id,
        attempt_id=f"att_{uuid.uuid4().hex[:6]}",
        agent_id=ctx["agent"].id,
        workspace_id=ctx["org"].id,
        expires_at=datetime.utcnow() + timedelta(hours=1),
        active=True,
        created_by="test",
    ))
    db.session.commit()


def _pull(client, ctx, max_tasks=1):
    resp = client.post(f"{BASE_URL}/agent/tasks/pull",
                       json={"max_tasks": max_tasks}, headers=ctx["headers"])
    assert resp.status_code == 200
    return resp.get_json()["data"]


# ── 依赖门语义 ────────────────────────────────────────────────────────


class TestDependencyGateSemantics:
    def test_todo_blocker_holds_dependent(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="todo")
        _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        _lease_task(runtime_ctx, blocker)  # 前置任务正被其他 Agent 执行
        data = _pull(client, runtime_ctx)
        assert data["tasks"] == []
        assert data["dependency_gate"]["blocked"] is True
        assert data["dependency_gate"]["skipped_blocked"] == 1

    def test_in_progress_blocker_still_holds(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="in_progress")
        _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        _lease_task(runtime_ctx, blocker)
        data = _pull(client, runtime_ctx)
        assert data["tasks"] == []
        assert data["dependency_gate"]["skipped_blocked"] == 1

    def test_done_blocker_releases_dependent(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="done")
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]
        assert "dependency_gate" not in data

    def test_cancelled_blocker_releases_dependent(self, client, runtime_ctx):
        # 排序约束语义：阻塞者到终态即解除；取消后是否连带取消下游由规划者裁决
        blocker = _make_task(runtime_ctx, status="cancelled")
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]
        assert "dependency_gate" not in data

    def test_partial_done_multi_blocker_still_holds(self, client, runtime_ctx):
        done_blocker = _make_task(runtime_ctx, status="done")
        open_blocker = _make_task(runtime_ctx, status="todo")
        _make_task(runtime_ctx, status="todo",
                   blocked_by=[done_blocker.id, open_blocker.id])
        _lease_task(runtime_ctx, open_blocker)
        data = _pull(client, runtime_ctx)
        assert data["tasks"] == []
        assert data["dependency_gate"]["skipped_blocked"] == 1

    def test_ghost_blocker_reference_releases(self, client, runtime_ctx):
        # 指向已删除任务的失效引用不卡死派发
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[987654321])
        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]

    def test_malformed_blocker_entries_tolerated(self, client, runtime_ctx):
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=["abc", None, ""])
        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]


# ── 绕行派发与多轮派发 ────────────────────────────────────────────────


class TestDispatchRoutingAroundBlockers:
    def test_dispatches_blocker_when_dependent_blocked(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="todo")
        _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        data = _pull(client, runtime_ctx)
        # 最新优先（id 降序）：dependent 被依赖门跳过 → 派发其无依赖的前置任务
        assert [t["task_id"] for t in data["tasks"]] == [blocker.id]
        assert data["dependency_gate"]["skipped_blocked"] == 1

    def test_multi_round_never_dispatches_blocked_task(self, client, runtime_ctx):
        blocker = _make_task(runtime_ctx, status="todo")
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        free = _make_task(runtime_ctx, status="todo")
        data = _pull(client, runtime_ctx, max_tasks=2)
        pulled_ids = [t["task_id"] for t in data["tasks"]]
        # 最新优先（id 降序）：free 先派、blocker 次之；dependent 在每轮都被
        # 依赖门拦下，绝不出现
        assert pulled_ids == [free.id, blocker.id]
        # 第一轮 free（更新）排在 dependent 之前未触及依赖门；
        # 第二轮派发 blocker 前跳过 dependent 一次
        assert data["dependency_gate"]["skipped_blocked"] == 1
        assert dependent.id not in pulled_ids

    def test_no_blocked_candidates_has_no_gate_field(self, client, runtime_ctx):
        _make_task(runtime_ctx, status="todo")
        data = _pull(client, runtime_ctx)
        assert len(data["tasks"]) == 1
        assert "dependency_gate" not in data


# ── 依赖解除后恢复派发 ────────────────────────────────────────────────


class TestUnblockAfterCompletion:
    def test_completing_blocker_releases_in_later_pull(self, client, runtime_ctx):
        from models import AgentTaskLease, TaskStatus, db
        blocker = _make_task(runtime_ctx, status="todo")
        dependent = _make_task(runtime_ctx, status="todo", blocked_by=[blocker.id])
        _lease_task(runtime_ctx, blocker)

        data = _pull(client, runtime_ctx)
        assert data["tasks"] == []
        assert data["dependency_gate"]["skipped_blocked"] == 1

        # 前置任务完成（终态）并释放租约后，依赖解除，后续 pull 恢复派发下游
        blocker.status = TaskStatus.DONE
        db.session.commit()
        db.session.query(AgentTaskLease).filter_by(task_id=blocker.id).update({"active": False})
        db.session.commit()

        data = _pull(client, runtime_ctx)
        assert [t["task_id"] for t in data["tasks"]] == [dependent.id]
        assert "dependency_gate" not in data
