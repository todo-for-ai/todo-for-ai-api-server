"""交接沉降窗口（AGENT_HANDOFF_SETTLE_SECONDS）：依赖解锁的派发延迟。

竞态：上游 commit 终态与交接产出（shared_context）写入之间存在时间窗，
空闲 Agent 在窗口内即可抢走下游任务，pull payload 里没有 upstream 数据。
沉降窗口让「已终态但刚终态」的上游在派发路径仍视为未解除；事件留痕
（task_handoff.downstream_unblocked_by）保持「终态即解锁」语义不受影响。
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
        "AGENT_HANDOFF_SETTLE_SECONDS": 0,
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
        "app": _isolated_app, "user": user, "org": org, "agent": agent, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _make_task(ctx, status="todo", blocked_by=None, updated_at=None):
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
    if updated_at is not None:
        # 直接落库模拟「终态发生于 N 秒前」（updated_at 有 onupdate，绕过 ORM 钩子）
        db.session.query(Task).filter(Task.id == task.id).update(
            {Task.updated_at: updated_at}, synchronize_session=False)
        db.session.commit()
    return task


def _pull(client, ctx, max_tasks=1):
    resp = client.post(f"{BASE_URL}/agent/tasks/pull",
                       json={"max_tasks": max_tasks}, headers=ctx["headers"])
    assert resp.status_code == 200
    return resp.get_json()["data"]


def _settle(app, seconds):
    app.config["AGENT_HANDOFF_SETTLE_SECONDS"] = seconds


# ── 派发路径（apply_settle=True）─────────────────────────────────────

def test_settle_zero_keeps_current_behavior(client, runtime_ctx):
    """默认 0：上游终态即放行（历史行为回归）。"""
    upstream = _make_task(runtime_ctx, status="done")
    downstream = _make_task(runtime_ctx, blocked_by=[upstream.id])
    data = _pull(client, runtime_ctx)
    assert [t["task_id"] for t in data["tasks"]] == [downstream.id]
    assert "dependency_gate" not in data


def test_settle_holds_freshly_terminal_upstream(client, runtime_ctx):
    """settle>0：上游刚进终态（窗口内）→ 下游被挡，附 dependency_gate。"""
    _settle(runtime_ctx["app"], 30)
    upstream = _make_task(runtime_ctx, status="done", updated_at=datetime.utcnow())
    downstream = _make_task(runtime_ctx, blocked_by=[upstream.id])
    data = _pull(client, runtime_ctx)
    assert data["tasks"] == []
    assert data["dependency_gate"]["blocked"] is True
    assert data["dependency_gate"]["skipped_blocked"] >= 1


def test_settle_releases_after_window_elapsed(client, runtime_ctx):
    """settle>0：上游终态已超窗 → 下游正常派发。"""
    _settle(runtime_ctx["app"], 30)
    upstream = _make_task(runtime_ctx, status="done",
                          updated_at=datetime.utcnow() - timedelta(seconds=60))
    downstream = _make_task(runtime_ctx, blocked_by=[upstream.id])
    data = _pull(client, runtime_ctx)
    assert [t["task_id"] for t in data["tasks"]] == [downstream.id]


def test_settle_applies_to_cancelled_terminal_too(client, runtime_ctx):
    """CANCELLED 同为终态，同样受沉降窗口约束。"""
    _settle(runtime_ctx["app"], 30)
    upstream = _make_task(runtime_ctx, status="cancelled", updated_at=datetime.utcnow())
    _make_task(runtime_ctx, blocked_by=[upstream.id])
    data = _pull(client, runtime_ctx)
    assert data["tasks"] == []
    assert data["dependency_gate"]["blocked"] is True


def test_non_terminal_upstream_still_blocks_with_settle_off(client, runtime_ctx):
    """settle=0 时未终态上游照常阻塞（基本语义回归）。"""
    upstream = _make_task(runtime_ctx, status="in_progress")
    _make_task(runtime_ctx, blocked_by=[upstream.id])
    # 上游挂活跃租约（模拟其他 Agent 在跑）：否则上游自身也是可拉候选
    _lease_task(runtime_ctx, upstream)
    data = _pull(client, runtime_ctx)
    assert data["tasks"] == []
    assert data["dependency_gate"]["blocked"] is True


def _lease_task(ctx, task):
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


# ── 事件留痕路径（apply_settle=False）────────────────────────────────

def test_event_trail_ignores_settle_window(runtime_ctx):
    """downstream_unblocked_by 不受沉降窗口影响：终态即解锁（通知语义）。"""
    from services.task_handoff import downstream_unblocked_by
    _settle(runtime_ctx["app"], 30)
    upstream = _make_task(runtime_ctx, status="done", updated_at=datetime.utcnow())
    downstream = _make_task(runtime_ctx, blocked_by=[upstream.id])
    # 派发路径会被 settle 挡住（对照）
    from api.agent_runtime_pull import _unsatisfied_blocker_ids
    assert upstream.id in _unsatisfied_blocker_ids(downstream)
    # 事件路径不受 settle 影响
    assert downstream_unblocked_by(upstream) == [downstream]


def test_upstream_output_task_not_blocked_by_unrelated_tasks(client, runtime_ctx):
    """沉降窗口只作用于 blocked_by 引用的上游，不影响无依赖任务派发。"""
    _settle(runtime_ctx["app"], 30)
    fresh_done = _make_task(runtime_ctx, status="done", updated_at=datetime.utcnow())
    independent = _make_task(runtime_ctx)
    data = _pull(client, runtime_ctx)
    assert [t["task_id"] for t in data["tasks"]] == [independent.id]
    assert fresh_done.id not in [t["task_id"] for t in data["tasks"]]
