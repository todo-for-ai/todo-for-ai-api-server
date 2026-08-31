"""Tests for P2.3 failure self-healing loop (classify, repair subtask, escalation)."""

import uuid
from datetime import datetime, timedelta

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    """每测试独立内存库：自愈链路涉及任务/租约/事件的复杂级联，
    会话级共享库在 task id 回收时相互污染。"""
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


class TestClassifyFailure:
    def test_code_exact_match(self):
        from services.failure_recovery import classify_failure

        assert classify_failure("TESTS_FAILED", "") == "test_failure"
        assert classify_failure("DOD_CHECK_FAILED", "") == "test_failure"
        assert classify_failure("BUILD_FAILED", "") == "build_failure"
        assert classify_failure("LEASE_EXPIRED", "") == "transient"
        assert classify_failure("TIMEOUT", "") == "timeout"

    def test_reason_keywords(self):
        from services.failure_recovery import classify_failure

        assert classify_failure(None, "3 tests failed in suite") == "test_failure"
        assert classify_failure(None, "npm build failed") == "build_failure"
        assert classify_failure(None, "request timed out after 30s") == "timeout"
        assert classify_failure(None, "connection reset") == "transient"

    def test_unknown_fallback(self):
        from services.failure_recovery import classify_failure

        assert classify_failure("WEIRD_CODE", "no idea") == "unknown"
        assert classify_failure(None, None) == "unknown"


class TestFailedCommitRecovery:
    @pytest.fixture
    def runtime_ctx(self, client, db_session, user_factory, organization_factory, agent_factory):
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

        # failed commit 现在会写失败经验（P3.1）；删 Agent 前先清掉，避免 FK 冲突
        for agent in created_agents:
            AgentExperience.query.filter_by(agent_id=agent.id).delete()
        db_session.commit()

    def _failed_attempt(self, db_session, agent, task, attempt_id=None):
        """建一个 ABORTED attempt + 配套 active lease（commit 校验要求）。"""
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

        attempt_id = attempt_id or f"att_{uuid.uuid4().hex[:8]}"
        lease_id = f"lea_{uuid.uuid4().hex[:8]}"
        # (task_id, active) 唯一约束：先删本任务全部旧租约行（含 inactive）
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

    def _commit_failed(self, client, task, ctx, attempt_id, lease_id=None):
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

    def test_first_failure_creates_repair_subtask(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        from models import Task

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id, owner_id=ctx["org"].id, title="Heal me",
            is_ai_task=True, dod=[{"type": "test", "value": "pytest -q"}],
        )
        attempt_id, lease_id = self._failed_attempt(db_session, ctx["agent"], task)

        resp = self._commit_failed(client, task, ctx, attempt_id, lease_id=lease_id)
        assert resp.status_code == 200, resp.get_json()
        recovery = resp.get_json()["data"]["recovery"]
        assert recovery["action"] == "repair_created"
        assert recovery["category"] == "test_failure"

        # 修复子任务：父链接 + DoD 继承 + AI 任务（回流派发池）
        repair = Task.query.get(recovery["repair_task_id"])
        assert repair is not None
        assert repair.parent_task_id == task.id
        assert repair.dod == [{"type": "test", "value": "pytest -q"}]
        assert repair.is_ai_task is True
        assert repair.owner_id == ctx["org"].id  # 回流同 workspace

    def test_escalation_after_max_attempts(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        from models import AgentTaskEvent, Task

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["org"].id, title="Escalate me", is_ai_task=True)

        # 预置 2 次失败历史（默认封顶 2）
        for _ in range(2):
            self._failed_attempt(db_session, ctx["agent"], task)

        attempt_id, lease_id = self._failed_attempt(db_session, ctx["agent"], task)
        resp = self._commit_failed(client, task, ctx, attempt_id, lease_id=lease_id)
        assert resp.status_code == 200, resp.get_json()
        recovery = resp.get_json()["data"]["recovery"]
        assert recovery["action"] == "escalated_human"
        assert recovery["failed_attempts"] >= 2

        # 审批队列事件
        event = AgentTaskEvent.query.filter_by(
            task_id=task.id, event_type="interaction_request"
        ).first()
        assert event is not None
        assert event.payload["interaction_type"] == "repair_escalation"
        # 不生成修复子任务
        assert Task.query.filter(Task.parent_task_id == task.id).count() == 0

    def test_idempotent_per_attempt(self, client, db_session, runtime_ctx, project_factory, task_factory):
        from models import Task

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["org"].id, title="Idem check", is_ai_task=True)
        attempt_id, lease_id = self._failed_attempt(db_session, ctx["agent"], task)

        first = self._commit_failed(client, task, ctx, attempt_id, lease_id=lease_id)
        assert first.status_code == 200
        assert first.get_json()["data"]["recovery"]["action"] == "repair_created"

        # 同一 attempt 重放（新的有效 lease）→ 幂等跳过，不重复生成
        from models import AgentTaskLease
        lease2 = f"lea_{uuid.uuid4().hex[:8]}"
        db_session.add(AgentTaskLease(
            lease_id=lease2, task_id=task.id, attempt_id=attempt_id,
            agent_id=ctx["agent"].id, workspace_id=ctx["agent"].workspace_id,
            expires_at=datetime.utcnow() + timedelta(seconds=300), active=True, created_by="test",
        ))
        db_session.commit()
        replay = self._commit_failed(client, task, ctx, attempt_id, lease_id=lease2)
        assert replay.status_code == 200
        replay_data = replay.get_json()["data"]
        # 幂等重放（无 recovery 字段）或 recovery skipped 均视为未重复生成
        assert replay_data.get("recovery") is None or replay_data["recovery"]["action"] == "skipped"

        assert Task.query.filter(Task.parent_task_id == task.id).count() == 1
