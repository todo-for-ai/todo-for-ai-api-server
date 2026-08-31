"""P1.2 验证门测试：commit 协议的 DoD 证据强制与证据查询端点。"""

import uuid
from datetime import datetime, timedelta

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _cleanup_runtime_rows(db_session):
    """清理提交协议侧的持久行，避免会话级测试库中的跨用例残留。

    本文件与 test_agent_runtime_protocol.py 共享同一个会话级 SQLite 库，
    双向清理（用例前+用例后）active lease 等行，防止 UNIQUE 冲突。
    """
    from models import AgentTaskLease, AgentTaskAttempt, AgentResultDedup, TaskEvidenceRecord

    def _purge():
        db_session.rollback()
        db_session.query(AgentTaskLease).delete(synchronize_session=False)
        db_session.query(AgentTaskAttempt).delete(synchronize_session=False)
        db_session.query(AgentResultDedup).delete(synchronize_session=False)
        db_session.query(TaskEvidenceRecord).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()


@pytest.fixture
def owner_auth(app, db_session):
    """创建用户并返回其 JWT headers（用于用户侧端点）。

    与 conftest.auth_headers 一致地直接建用户（不注册到 user_factory 清理列表，
    因为端点产生的 UserActivity 会在清理删除用户时触发级联断言）。
    """
    import uuid
    from models import User
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique_id = str(uuid.uuid4())[:8]
    user = User(
        username=f"testuser_{unique_id}",
        email=f"test_{unique_id}@example.com",
    )
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def runtime_ctx(client, db_session, user_factory, organization_factory, agent_factory):
    """创建带 runtime 认证的 Agent 上下文（introspect 换取 agent token）。"""
    from models import AgentExperience, AgentKey

    created_agents = []

    def _create():
        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(
            workspace_id=org.id,
            creator_user_id=user.id,
            runner_enabled=True,
        )
        created_agents.append(agent)
        key_row, raw_key = AgentKey.generate_key(
            name=f"Runtime Key {uuid.uuid4().hex[:6]}",
            workspace_id=org.id,
            agent_id=agent.id,
            created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()

        auth_resp = client.post(
            f"{BASE_URL}/agent/auth/introspect",
            json={"agent_key": raw_key},
        )
        assert auth_resp.status_code == 200, auth_resp.get_json()
        token = auth_resp.get_json()["data"]["access_token"]

        return {
            "user": user,
            "org": org,
            "agent": agent,
            "raw_key": raw_key,
            "headers": {"Authorization": f"Bearer {token}"},
        }

    yield _create

    # failed commit 会写失败经验（P3.1）；删 Agent 前先清掉，避免 FK 冲突
    for agent in created_agents:
        AgentExperience.query.filter_by(agent_id=agent.id).delete()
    db_session.commit()


def _make_lease(db_session, agent, task):
    """为任务创建 attempt + 有效租约，返回 (attempt_id, lease_id)。"""
    from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease

    attempt_id = f"att_{uuid.uuid4().hex[:8]}"
    lease_id = f"lea_{uuid.uuid4().hex[:8]}"
    attempt = AgentTaskAttempt(
        attempt_id=attempt_id,
        task_id=task.id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        state=AgentTaskAttemptState.ACTIVE,
        lease_id=lease_id,
        started_at=datetime.utcnow(),
        created_by="test",
    )
    lease = AgentTaskLease(
        lease_id=lease_id,
        task_id=task.id,
        attempt_id=attempt_id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        expires_at=datetime.utcnow() + timedelta(seconds=120),
        active=True,
        created_by="test",
    )
    db_session.add(attempt)
    db_session.add(lease)
    db_session.commit()
    return attempt_id, lease_id


def _commit(client, task, attempt_id, lease_id, status, evidence=None, extra_json=None, headers=None):
    payload = {"attempt_id": attempt_id, "lease_id": lease_id, "status": status}
    if evidence is not None:
        payload["evidence"] = evidence
    if extra_json:
        payload.update(extra_json)
    request_headers = dict(headers or {})
    request_headers["Idempotency-Key"] = attempt_id
    return client.post(
        f"{BASE_URL}/agent/tasks/{task.id}/commit",
        json=payload,
        headers=request_headers,
    )


class TestCommitDodEvidenceGate:
    """commit succeeded 时按任务 DoD 强制证据。"""

    def test_commit_without_evidence_rejected_when_dod_present(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=ctx["user"].id,
            title="DoD test task",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}, {"type": "build", "value": "make build"}],
        )
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "succeeded", headers=ctx["headers"])

        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error_details"]["code"] == "DOD_EVIDENCE_MISSING"
        assert len(body["error_details"]["unmet"]) == 2
        # 状态不得变更（拒绝发生在状态变更之前，租约保持有效以便重试提交）
        db_session.expire(task)
        assert task.status.value != "done"

    def test_commit_with_passing_evidence_succeeds(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        from models import TaskEvidenceRecord

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=ctx["user"].id,
            title="DoD happy path",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}, {"type": "lint", "value": "ruff check ."}],
        )
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "succeeded", evidence=[
            {"evidence_type": "test", "status": "passed", "summary": "24 passed", "detail": {"exit_code": 0}},
            {"evidence_type": "lint", "status": "passed", "summary": "0 issues"},
        ], headers=ctx["headers"])

        assert resp.status_code == 200
        assert resp.get_json()["data"]["evidence_count"] == 2
        db_session.expire(task)
        assert task.status.value == "done"
        stored = TaskEvidenceRecord.query.filter_by(task_id=task.id).all()
        assert {e.evidence_type for e in stored} == {"test", "lint"}
        assert all(e.status == "passed" for e in stored)

    def test_commit_with_failed_evidence_rejected(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=ctx["user"].id,
            title="DoD failed evidence",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}],
        )
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "succeeded", evidence=[
            {"evidence_type": "test", "status": "failed", "summary": "1 failed"},
        ], headers=ctx["headers"])

        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "DOD_EVIDENCE_MISSING"

    def test_commit_invalid_evidence_type_rejected(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["user"].id, title="Bad evidence type")
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "succeeded", evidence=[
            {"evidence_type": "vibes", "status": "passed"},
        ], headers=ctx["headers"])

        assert resp.status_code == 400
        assert "invalid evidence_type" in resp.get_json()["message"]

    def test_commit_without_evidence_ok_when_no_dod(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        """向后兼容：未声明 DoD 的任务，无证据也可提交成功。"""
        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=ctx["user"].id,
            title="Legacy task without dod",
            is_ai_task=True,
        )
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "succeeded", headers=ctx["headers"])

        assert resp.status_code == 200
        assert resp.get_json()["data"]["evidence_count"] == 0
        db_session.expire(task)
        assert task.status.value == "done"

    def test_evidence_stored_even_without_dod(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        """任务未声明 DoD 时，主动提交的证据仍作为审计材料保留。"""
        from models import TaskEvidenceRecord

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["user"].id, title="Optional evidence")
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "succeeded", evidence=[
            {"evidence_type": "command", "status": "passed", "summary": "make verify ok", "url": "https://ci.example.com/run/1"},
        ], headers=ctx["headers"])

        assert resp.status_code == 200
        stored = TaskEvidenceRecord.query.filter_by(task_id=task.id).all()
        assert len(stored) == 1
        assert stored[0].url == "https://ci.example.com/run/1"
        assert stored[0].agent_id == ctx["agent"].id

    def test_failed_commit_ignores_dod_gate(
        self, client, db_session, runtime_ctx, project_factory, task_factory
    ):
        """failed/cancelled 提交不受 DoD 门限制（失败本身就该上报并进入 review）。"""
        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(
            project_id=project.id,
            owner_id=ctx["user"].id,
            title="Failed commit with dod",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}],
        )
        attempt_id, lease_id = _make_lease(db_session, ctx["agent"], task)

        resp = _commit(client, task, attempt_id, lease_id, "failed",
                       extra_json={"failure_code": "TESTS_FAILED", "failure_reason": "3 tests broke"},
                       headers=ctx["headers"])

        assert resp.status_code == 200
        db_session.expire(task)
        assert task.status.value == "review"


class TestTaskEvidenceEndpoint:
    """用户侧证据查询端点。"""

    def test_list_task_evidence(self, client, db_session, owner_auth, project_factory, task_factory):
        from models import TaskEvidenceRecord

        project = project_factory(owner_id=owner_auth["user"].id)
        task = task_factory(project_id=project.id, title="Evidence listing", dod=[{"type": "test", "value": "pytest"}])
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="test", status="passed",
            summary="ok", created_by="agent:1",
        ))
        db_session.commit()

        resp = client.get(f"{BASE_URL}/tasks/{task.id}/evidence", headers=owner_auth["headers"])

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["dod"] == [{"type": "test", "value": "pytest"}]
        assert len(data["evidence"]) == 1
        assert data["evidence"][0]["evidence_type"] == "test"

    def test_list_task_evidence_requires_project_access(
        self, client, db_session, owner_auth, project_factory, task_factory, user_factory
    ):
        """非项目成员访问他人项目的任务证据应被拒绝。"""
        other_user = user_factory()
        project = project_factory(owner_id=other_user.id)
        task = task_factory(project_id=project.id, title="Private evidence")

        resp = client.get(f"{BASE_URL}/tasks/{task.id}/evidence", headers=owner_auth["headers"])

        assert resp.status_code == 403


class TestAcrInstrumentation:
    """ACR 埋点：人类变更 AI 任务状态时 human_intervention_count 递增。"""

    def test_human_status_change_increments_counter(
        self, client, db_session, owner_auth, project_factory, task_factory
    ):
        project = project_factory(owner_id=owner_auth["user"].id)
        task = task_factory(project_id=project.id, title="AI task", is_ai_task=True)
        assert (task.human_intervention_count or 0) == 0

        resp = client.put(
            f"{BASE_URL}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()

        db_session.expire(task)
        assert task.human_intervention_count == 1

    def test_non_ai_task_not_counted(
        self, client, db_session, owner_auth, project_factory, task_factory
    ):
        project = project_factory(owner_id=owner_auth["user"].id)
        task = task_factory(project_id=project.id, title="Human task", is_ai_task=False)

        resp = client.put(
            f"{BASE_URL}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()

        db_session.expire(task)
        assert (task.human_intervention_count or 0) == 0


class TestDodViaTaskApi:
    """通过任务 API 声明/更新 DoD。"""

    def test_create_task_with_dod(self, client, db_session, owner_auth, project_factory):
        project = project_factory(owner_id=owner_auth["user"].id)
        resp = client.post(
            f"{BASE_URL}/tasks",
            json={
                "project_id": project.id,
                "title": "Task with dod",
                "dod": [
                    {"type": "test", "value": "pytest -q"},
                    {"type": "build", "value": "make build"},
                ],
            },
            headers=owner_auth["headers"],
        )
        assert resp.status_code in (200, 201), resp.get_json()
        data = resp.get_json()["data"]
        assert data["dod"] == [
            {"type": "test", "value": "pytest -q"},
            {"type": "build", "value": "make build"},
        ]

    def test_create_task_rejects_invalid_dod_type(self, client, db_session, owner_auth, project_factory):
        project = project_factory(owner_id=owner_auth["user"].id)
        resp = client.post(
            f"{BASE_URL}/tasks",
            json={
                "project_id": project.id,
                "title": "Bad dod",
                "dod": [{"type": "vibes", "value": "trust me"}],
            },
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_update_task_dod(self, client, db_session, owner_auth, project_factory, task_factory):
        project = project_factory(owner_id=owner_auth["user"].id)
        task = task_factory(project_id=project.id, title="Update dod")
        resp = client.put(
            f"{BASE_URL}/tasks/{task.id}",
            json={"dod": [{"type": "lint", "value": "ruff check ."}]},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        db_session.expire(task)
        assert task.dod == [{"type": "lint", "value": "ruff check ."}]


class TestRuntimeFullLoop:
    """runtime 视角全链路：pull 下发 dod → 拒绝无证据提交 → 带证据提交成功。"""

    def test_full_loop_pull_reject_then_success(self, client, db_session, runtime_ctx, project_factory, task_factory):
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease, TaskStatus

        ctx = runtime_ctx()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        # pull 协议按 task.owner_id == agent.workspace_id 匹配任务
        task = task_factory(
            project_id=project.id,
            owner_id=ctx["org"].id,
            title="Full loop dod task",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}],
        )

        # 1) runtime pull：payload 必须下发 dod，并生成 attempt/lease
        resp = client.post(
            f"{BASE_URL}/agent/tasks/pull",
            json={"max_tasks": 1},
            headers=ctx["headers"],
        )
        assert resp.status_code == 200
        tasks = resp.get_json()["data"]["tasks"]
        assert tasks, "expected at least one pulled task"
        pulled = next(t for t in tasks if t["task_id"] == task.id)
        assert pulled["payload"]["dod"] == [{"type": "test", "value": "pytest -q"}]
        attempt_id = pulled["attempt_id"]
        lease_id = pulled["lease_id"]

        # 2) 无证据提交被验证门拒绝
        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/commit",
            json={"attempt_id": attempt_id, "lease_id": lease_id, "status": "succeeded"},
            headers={**ctx["headers"], "Idempotency-Key": attempt_id},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "DOD_EVIDENCE_MISSING"

        # 3) 带通过证据重新提交 → 任务完成，证据入库
        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/commit",
            json={
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "status": "succeeded",
                "evidence": [
                    {"evidence_type": "test", "status": "passed",
                     "summary": "1 passed", "detail": {"command": "pytest -q", "exit_code": 0}},
                ],
            },
            headers={**ctx["headers"], "Idempotency-Key": f"{attempt_id}-with-evidence"},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["evidence_count"] == 1

        db_session.expire(task)
        assert task.status.value == "done"

        # 4) 用户侧证据端点可见
        from flask_jwt_extended import create_access_token
        with client.application.app_context():
            token = create_access_token(identity=str(ctx["user"].id))
        resp = client.get(
            f"{BASE_URL}/tasks/{task.id}/evidence",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["dod"] == [{"type": "test", "value": "pytest -q"}]
        assert len(data["evidence"]) == 1
