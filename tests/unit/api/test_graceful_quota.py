"""优雅停车回归：LLM token 额度耗尽熔断 + 死循环的无进展退出点。

- quota_exhausted 是资源级故障：不生成修复子任务/不计重试封顶，
  直接熔断该 Agent 派发并写 interaction_request 上报用户；
  循环任务额外把循环强制 STALLED（带明确 last_error）。
- 无进展护栏：规划器连续看到 N 轮失败后不允许再 extend
  （complete 仍允许），保证「死循环」也有退出点。
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


def _make_loop_task(db_session, ctx, project, loop, status=None):
    from models import Task, TaskStatus

    task = Task(
        title=f"round_{uuid.uuid4().hex[:6]}",
        content='{"prompt":"do the round"}',
        project_id=project.id,
        owner_id=ctx["org"].id,
        is_ai_task=True,
        status=status or TaskStatus.IN_PROGRESS,
        dod=[],
    )
    db_session.add(task)
    db_session.flush()
    task.add_tag(loop.tag)
    db_session.commit()
    return task


def _failed_attempt(db_session, agent, task):
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


def _commit_failed(client, task, ctx, attempt_id, lease_id, code="TESTS_FAILED", reason="2 tests broke"):
    return client.post(
        f"{BASE_URL}/agent/tasks/{task.id}/commit",
        json={
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "status": "failed",
            "failure_code": code,
            "failure_reason": reason,
        },
        headers={**ctx["headers"], "Idempotency-Key": attempt_id},
    )


# ── 归因 ─────────────────────────────────────────────────────────────


class TestQuotaClassification:
    def test_quota_codes(self):
        from services.failure_recovery import classify_failure

        for code in ("QUOTA_EXCEEDED", "INSUFFICIENT_QUOTA", "BILLING_ERROR", "PAYMENT_REQUIRED"):
            assert classify_failure(code, "") == "quota_exhausted"

    def test_quota_reason_keywords(self):
        from services.failure_recovery import classify_failure

        assert classify_failure(None, "Error: insufficient_quota from provider") == "quota_exhausted"
        assert classify_failure(None, "Your credit balance is too low to run the model") == "quota_exhausted"
        assert classify_failure(None, "You exceeded your current quota") == "quota_exhausted"
        assert classify_failure(None, "402 payment required") == "quota_exhausted"
        # 普通 429 限流仍是可重试的瞬时错误
        assert classify_failure(None, "rate limit hit, retry later") == "transient"


# ── 失败自愈：额度耗尽不可重试 ───────────────────────────────────────


class TestQuotaRecovery:
    def test_quota_failure_escalates_without_repair_subtask(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        from models import AgentTaskEvent, Task, TaskStatus

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id, owner_id=ctx["org"].id, title="Quota fail",
            is_ai_task=True, dod=[],
        )
        attempt_id, lease_id = _failed_attempt(db_session, ctx["agent"], task)

        resp = _commit_failed(client, task, ctx, attempt_id, lease_id,
                              code="QUOTA_EXCEEDED", reason="insufficient_quota")
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["recovery"]["action"] == "escalated_quota_exhausted"
        assert body["recovery"]["quota_report"]["reported"] is True

        db_session.expire_all()
        # 非循环任务仍进 REVIEW（人看），但没有任何修复子任务
        assert task.status == TaskStatus.REVIEW
        assert Task.query.filter_by(parent_task_id=task.id).all() == []

        events = AgentTaskEvent.query.filter_by(task_id=task.id).all()
        quota_events = [
            e for e in events
            if (e.payload or {}).get('interaction_type') == 'token_quota_exhausted'
        ]
        assert len(quota_events) == 1

    def test_quota_report_idempotent_within_window(self, db_session, runtime_ctx, project_factory, task_factory):
        from services.failure_recovery import handle_failed_commit
        from services.quota_guard import has_pending_quota_block

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["org"].id, is_ai_task=True, dod=[])

        first = handle_failed_commit(task, ctx["agent"], attempt_id=f"att_{uuid.uuid4().hex[:6]}",
                                     failure_code="QUOTA_EXCEEDED", failure_reason="no credits")
        second = handle_failed_commit(task, ctx["agent"], attempt_id=f"att_{uuid.uuid4().hex[:6]}",
                                      failure_code="QUOTA_EXCEEDED", failure_reason="no credits")
        assert first["action"] == "escalated_quota_exhausted"
        assert first["quota_report"]["reported"] is True
        assert second["quota_report"]["reported"] is False
        assert second["quota_report"]["reason"] == "already_reported_in_window"
        assert has_pending_quota_block(ctx["agent"].id) is True

    def test_quota_block_expires_after_window(self, db_session, runtime_ctx, project_factory,
                                              task_factory, monkeypatch):
        from services import quota_guard
        from services.failure_recovery import handle_failed_commit

        monkeypatch.setenv('QUOTA_BLOCK_WINDOW_HOURS', '24')
        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["org"].id, is_ai_task=True, dod=[])
        handle_failed_commit(task, ctx["agent"], attempt_id=f"att_{uuid.uuid4().hex[:6]}",
                             failure_code="QUOTA_EXCEEDED", failure_reason="no credits")
        assert quota_guard.has_pending_quota_block(ctx["agent"].id) is True

        # 把事件 created_at 拨回窗口之外 → 熔断解除
        from models import AgentTaskEvent
        event = AgentTaskEvent.query.filter_by(agent_id=ctx["agent"].id).first()
        event.created_at = datetime.utcnow() - timedelta(hours=25)
        db_session.commit()
        assert quota_guard.has_pending_quota_block(ctx["agent"].id) is False


# ── 派发熔断门 ───────────────────────────────────────────────────────


class TestQuotaDispatchGate:
    def test_pull_blocked_by_quota(self, client, db_session, runtime_ctx, project_factory, task_factory):
        from models import AgentTaskEvent

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["org"].id, is_ai_task=True, dod=[])
        db_session.add(AgentTaskEvent(
            task_id=task.id, attempt_id="", agent_id=ctx["agent"].id,
            workspace_id=ctx["org"].id, event_type="interaction_request",
            seq=1, event_timestamp=datetime.utcnow(), payload={
                "interaction_id": "quota_x", "interaction_type": "token_quota_exhausted",
                "status": "pending_approval",
            },
            message="test", created_by="test",
        ))
        db_session.commit()
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1},
                           headers=ctx["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["tasks"] == []
        assert data["quota_block"]["reason"] == "token_quota_exhausted"


# ── 循环任务的额度耗尽：强制 STALLED 停车 ────────────────────────────


class TestLoopQuotaStall:
    def test_loop_task_quota_failure_stalls_loop(
        self, client, db_session, runtime_ctx, project_factory
    ):
        from models import GoalLoopStatus, Task, TaskStatus

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        task = _make_loop_task(db_session, ctx, project, loop)
        attempt_id, lease_id = _failed_attempt(db_session, ctx["agent"], task)

        resp = _commit_failed(client, task, ctx, attempt_id, lease_id,
                              code="QUOTA_EXCEEDED", reason="insufficient_quota")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["recovery"]["action"] == "escalated_quota_exhausted"

        db_session.expire_all()
        assert task.status == TaskStatus.CANCELLED
        assert loop.status == GoalLoopStatus.STALLED
        assert "额度" in (loop.last_error or "")
        assert loop.finished_at is not None

    def test_loop_task_quota_failure_no_repair_subtask(
        self, client, db_session, runtime_ctx, project_factory
    ):
        from models import Task

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        task = _make_loop_task(db_session, ctx, project, loop)
        attempt_id, lease_id = _failed_attempt(db_session, ctx["agent"], task)

        resp = _commit_failed(client, task, ctx, attempt_id, lease_id,
                              code="QUOTA_EXCEEDED", reason="billing")
        assert resp.status_code == 200
        db_session.expire_all()
        assert Task.query.filter_by(parent_task_id=task.id).all() == []


# ── 无进展护栏：死循环的退出点 ────────────────────────────────────────


class TestNoProgressGuardrail:
    def test_trailing_failure_streak(self, db_session, runtime_ctx, project_factory):
        from models import TaskStatus
        from services.goal_loop.query import trailing_failure_streak

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        _make_loop_task(db_session, ctx, project, loop, status=TaskStatus.CANCELLED)
        _make_loop_task(db_session, ctx, project, loop, status=TaskStatus.DONE)
        _make_loop_task(db_session, ctx, project, loop, status=TaskStatus.CANCELLED)
        _make_loop_task(db_session, ctx, project, loop, status=TaskStatus.CANCELLED)
        assert trailing_failure_streak(loop.id) == 2

    def test_extend_denied_at_no_progress_limit(self, db_session, runtime_ctx,
                                                project_factory, monkeypatch):
        from models import GoalLoopStatus
        from services.goal_loop import state_machine
        from services.goal_loop.state_machine import maybe_advance

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        # 已连续 3 轮失败（≥ 默认阈值 3）
        from models import TaskStatus
        for _ in range(3):
            _make_loop_task(db_session, ctx, project, loop, status=TaskStatus.CANCELLED)
        loop.plan = [{"title": "再试一轮", "content": "retry"}]
        loop.plan_index = 1
        db_session.commit()

        monkeypatch.setattr(
            state_machine, 'call_review',
            lambda l, s: {'action': 'extend',
                          'steps': [{'title': '继续烧', 'content': 'keep going'}]},
        )

        result = maybe_advance(loop.id)
        db_session.expire_all()
        # extend 被拒绝 → 计 stall；stall_limit=2 未到，循环仍在 RUNNING
        assert result['reason'] in ('stall_counted', 'stalled')
        assert loop.stall_count == 1
        assert loop.status == GoalLoopStatus.RUNNING
        assert 'no_progress' in (loop.last_error or '')

        # 再推一次（仍连续失败）→ 第二次计 stall → STALLED 终态退出
        maybe_advance(loop.id)
        db_session.expire_all()
        assert loop.status == GoalLoopStatus.STALLED

    def test_complete_still_allowed_at_no_progress_limit(self, db_session, runtime_ctx,
                                                         project_factory, monkeypatch):
        from models import GoalLoopStatus
        from services.goal_loop import state_machine
        from services.goal_loop.state_machine import maybe_advance

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        loop = _make_loop(db_session, ctx, project)
        from models import TaskStatus
        for _ in range(3):
            _make_loop_task(db_session, ctx, project, loop, status=TaskStatus.CANCELLED)
        loop.plan = [{"title": "验收", "content": "final check"}]
        loop.plan_index = 1
        db_session.commit()

        monkeypatch.setattr(
            state_machine, 'call_review',
            lambda l, s: {'action': 'complete', 'reason': '验收虽失败但目标已达成'},
        )
        monkeypatch.setattr(state_machine, 'trailing_failure_streak', lambda lid: 3)

        result = maybe_advance(loop.id)
        db_session.expire_all()
        assert result['reason'] == 'completed'
        assert loop.status == GoalLoopStatus.DONE
