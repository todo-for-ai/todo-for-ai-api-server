"""长跑（Endurance）语义回归：目标循环任务的 failed 提交不再卡 REVIEW。

背景：循环任务 failed 提交曾被置为 REVIEW（活跃态），循环状态机等它
终态 → 循环挂死在人手里。现在循环任务失败直接置 CANCELLED（终态），
由 maybe_advance 推进规划器评审（extend 重试 / blocked 计 stall）；
失败自愈通道对循环任务关闭（重规划是规划器的职责，双通道会重复派发）。
"""

import uuid
from datetime import datetime, timedelta

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    from models import db

    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture(autouse=True)
def _scripted_planner(monkeypatch):
    """评审/拆解走确定性 scripted 规划器，单元测试不发 LLM 请求。"""
    monkeypatch.setenv('GOAL_LOOP_PLANNER', 'scripted')


@pytest.fixture
def runtime_ctx(client, db_session, user_factory, organization_factory, agent_factory):
    from models import AgentExperience, AgentKey

    created_agents = []

    def _create():
        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(workspace_id=org.id, runner_enabled=True)
        created_agents.append(agent)
        key_row, raw_key = AgentKey.generate_key(
            name=f"Key {uuid.uuid4().hex[:6]}", workspace_id=org.id,
            agent_id=agent.id, created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()
        resp = client.post(f"{BASE_URL}/agent/auth/introspect", json={"agent_key": raw_key})
        assert resp.status_code == 200
        token = resp.get_json()["data"]["access_token"]
        return {"user": user, "org": org, "agent": agent,
                "headers": {"Authorization": f"Bearer {token}"}}

    yield _create

    for agent in created_agents:
        AgentExperience.query.filter_by(agent_id=agent.id).delete()
    db_session.commit()


def _make_loop(db_session, ctx, project):
    from models import GoalLoop, GoalLoopStatus

    loop = GoalLoop(
        workspace_id=ctx["org"].id,
        project_id=project.id,
        agent_id=ctx["agent"].id,
        title=f"loop_{uuid.uuid4().hex[:6]}",
        goal_text="把目标做成",
        status=GoalLoopStatus.RUNNING,
        rounds_limit=10,
        stall_limit=2,
        created_by=ctx["user"].id,
    )
    db_session.add(loop)
    db_session.commit()
    return loop


def _make_loop_task(db_session, ctx, project, loop):
    from models import Task, TaskStatus

    task = Task(
        title=f"round_{uuid.uuid4().hex[:6]}",
        content='{"prompt":"do the round"}',
        project_id=project.id,
        owner_id=ctx["org"].id,
        is_ai_task=True,
        status=TaskStatus.IN_PROGRESS,
        dod=[],
    )
    db_session.add(task)
    db_session.flush()
    task.add_tag(loop.tag)
    db_session.commit()
    return task


def _failed_attempt(db_session, agent, task):
    """建 ABORTED attempt + 配套 active lease（commit 校验要求）。"""
    from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

    attempt_id = f"att_{uuid.uuid4().hex[:8]}"
    lease_id = f"lea_{uuid.uuid4().hex[:8]}"
    AgentTaskLease.query.filter_by(task_id=task.id).delete(synchronize_session=False)
    db_session.commit()
    db_session.add(AgentTaskAttempt(
        attempt_id=attempt_id, task_id=task.id, agent_id=agent.id,
        workspace_id=agent.workspace_id, state=AgentTaskAttemptState.ABORTED,
        lease_id=lease_id,
        started_at=datetime.utcnow() - timedelta(seconds=60),
        ended_at=datetime.utcnow(), created_by="test",
    ))
    db_session.add(AgentTaskLease(
        lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
        agent_id=agent.id, workspace_id=agent.workspace_id,
        expires_at=datetime.utcnow() + timedelta(seconds=300), active=True, created_by="test",
    ))
    db_session.commit()
    return attempt_id, lease_id


def _commit_failed(client, task, ctx, attempt_id, lease_id=None):
    return client.post(
        f"{BASE_URL}/agent/tasks/{task.id}/commit",
        json={
            "attempt_id": attempt_id,
            "lease_id": lease_id or f"lea_{uuid.uuid4().hex[:8]}",
            "status": "failed",
            "failure_code": "TESTS_FAILED",
            "failure_reason": "2 tests broke",
        },
        headers={**ctx["headers"], "Idempotency-Key": attempt_id},
    )


class TestLoopDrivenFailedCommit:
    def test_failed_commit_cancels_loop_task_and_stall_counts(
        self, client, db_session, runtime_ctx, project_factory
    ):
        from models import GoalLoopStatus, Task, TaskStatus

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        task = _make_loop_task(db_session, ctx, project, loop)
        attempt_id, lease_id = _failed_attempt(db_session, ctx["agent"], task)

        resp = _commit_failed(client, task, ctx, attempt_id, lease_id)
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["final_status"] == "failed"
        # 自愈通道对循环任务关闭：归因照记，但不生成修复子任务
        assert body["recovery"]["action"] == "loop_replan"

        db_session.expire_all()
        # 核心断言：任务进终态而非 REVIEW，循环不被挂死
        assert task.status == TaskStatus.CANCELLED
        children = Task.query.filter_by(parent_task_id=task.id).all()
        assert children == []

        # 规划器已接手：scripted 评审对 failed 轮给 blocked → stall 计 1，循环仍在跑
        assert loop.status == GoalLoopStatus.RUNNING
        assert loop.stall_count == 1

    def test_second_consecutive_failure_stalls_loop(
        self, client, db_session, runtime_ctx, project_factory
    ):
        """连续两次失败轮 → stall_limit 触发 STALLED（护栏仍兜底，不会无限烧）。"""
        from models import GoalLoopStatus

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)

        for _ in range(2):
            task = _make_loop_task(db_session, ctx, project, loop)
            attempt_id, lease_id = _failed_attempt(db_session, ctx["agent"], task)
            resp = _commit_failed(client, task, ctx, attempt_id, lease_id)
            assert resp.status_code == 200

        db_session.expire_all()
        assert loop.status == GoalLoopStatus.STALLED
        assert loop.stall_count >= 2

    def test_non_loop_failed_commit_keeps_review_and_repair(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        """非循环任务维持原语义：REVIEW + 修复子任务回流（回归保护）。"""
        from models import Task, TaskStatus

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id, owner_id=ctx["org"].id, title="Plain AI task",
            is_ai_task=True, dod=[],
        )
        attempt_id, lease_id = _failed_attempt(db_session, ctx["agent"], task)

        resp = _commit_failed(client, task, ctx, attempt_id, lease_id)
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["recovery"]["action"] == "repair_created"

        db_session.expire_all()
        assert task.status == TaskStatus.REVIEW
        repairs = Task.query.filter_by(parent_task_id=task.id).all()
        assert len(repairs) == 1


class TestLoopTaskLookup:
    def test_active_loop_id_for_task(self, db_session, runtime_ctx, project_factory):
        from models import GoalLoopStatus
        from services.goal_loop.query import active_loop_id_for_task

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        task = _make_loop_task(db_session, ctx, project, loop)

        assert active_loop_id_for_task(task) == loop.id

        loop.status = GoalLoopStatus.STALLED
        db_session.commit()
        assert active_loop_id_for_task(task) is None

    def test_untagged_task_has_no_loop(self, db_session, runtime_ctx, project_factory, task_factory):
        from services.goal_loop.query import active_loop_id_for_task

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["org"].id)
        assert active_loop_id_for_task(task) is None

    def test_recent_history_carries_failure_reason(self, db_session, runtime_ctx, project_factory):
        """cancelled 轮的失败归因进评审上下文，规划器才能对症重规划。"""
        from models import AgentTaskAttempt, AgentTaskAttemptState, TaskStatus
        from services.goal_loop.query import recent_history

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        task = _make_loop_task(db_session, ctx, project, loop)
        task.status = TaskStatus.CANCELLED
        db_session.add(AgentTaskAttempt(
            attempt_id=f"att_{uuid.uuid4().hex[:8]}", task_id=task.id,
            agent_id=ctx["agent"].id, workspace_id=ctx["org"].id,
            state=AgentTaskAttemptState.ABORTED, lease_id=f"lea_{uuid.uuid4().hex[:8]}",
            failure_code="TESTS_FAILED", failure_reason="3 tests broke",
            started_at=datetime.utcnow(), ended_at=datetime.utcnow(), created_by="test",
        ))
        db_session.commit()

        history = recent_history(loop)
        assert history[-1]["status"] == "cancelled"
        assert history[-1]["failure"] == "TESTS_FAILED: 3 tests broke"
