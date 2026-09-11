"""迭代 51：agent_runtime_pull.py 分支覆盖回归（拉取协议缺口补齐）。

覆盖此前缺失的分支：能力密钥映射、密钥能力引用构建（owned/granted）、
可拉取任务过滤（allowed_project_ids/活跃租约跳过）、pull 端点的
工作时间门/并发门/预算门/max_tasks 校验/陈旧租约清理/IntegrityError 重试/
非 dict 内容包装、租约续期与释放矩阵。
认证方式与既有协议测试一致：introspect 换取会话令牌后 Bearer 调用。
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

from app import create_app
from models import (
    db,
    Agent,
    AgentSecret,
    AgentSecretGrant,
    AgentStatus,
    Task,
    TaskStatus,
)

import api.agent_runtime_pull as pull
from api.agent_runtime_pull import (
    _build_secret_capability_ref,
    _capability_keys_for_secret,
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
    from flask_jwt_extended import create_access_token
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


def _make_task_in(ctx, status="todo", content=None, **kw):
    from models import Task, TaskStatus, db
    task = Task(
        title=kw.get("title", f"t_{uuid.uuid4().hex[:6]}"),
        content=content if content is not None else '{"prompt":"x"}',
        project_id=kw.get("project_id", ctx["project"].id),
        owner_id=ctx["org"].id,
        is_ai_task=True,
        status=TaskStatus(status),
        dod=[],
    )
    db.session.add(task)
    db.session.commit()
    return task


# ── _capability_keys_for_secret / _build_secret_capability_ref ───────


class TestCapabilityKeys:
    def test_all_known_types(self):
        assert _capability_keys_for_secret("api_key") == ["credential.api.invoke"]
        assert _capability_keys_for_secret("oauth_token") == ["credential.oauth.invoke"]
        assert _capability_keys_for_secret("session_cookie") == ["credential.session.use"]
        assert _capability_keys_for_secret("webhook_secret") == ["credential.webhook.sign"]
        assert _capability_keys_for_secret("custom") == ["credential.custom.use"]

    def test_unknown_and_none_and_case_fall_back_to_custom(self):
        for raw in ("weird", None, "  "):
            assert _capability_keys_for_secret(raw) == ["credential.custom.use"]

    def test_case_insensitive_strip(self):
        assert _capability_keys_for_secret("  API_KEY ") == ["credential.api.invoke"]


class TestBuildSecretCapabilityRef:
    def _secret(self, **kw):
        from types import SimpleNamespace
        defaults = dict(id=7, name="cfg", secret_type="api_key",
                        scope_type="workspace", project_id=42)
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_without_grant(self):
        ref = _build_secret_capability_ref(self._secret(), source="owned", grant=None)
        assert ref["secret_id"] == 7
        assert ref["source"] == "owned"
        assert ref["allowed_actions"] == ["manage", "consume", "proxy_execute"]
        assert ref["grant"] is None
        assert ref["capability_keys"] == ["credential.api.invoke"]
        assert ref["project_id"] == 42

    def test_without_grant_project_none(self):
        ref = _build_secret_capability_ref(self._secret(project_id=None), source="owned")
        assert ref["project_id"] is None

    def test_with_grant_full_payload(self):
        grant = SimpleNamespace(
            grant_id="g1", from_agent_id=1, to_agent_id=2, grant_mode="proxy",
            status="active", max_uses=5, used_count=2,
            expires_at=datetime(2026, 9, 11, 12, 0, 0),
            task_id=77, attempt_id="att_1", chain_id=9,
        )
        secret = self._secret(project_id=None)
        ref = _build_secret_capability_ref(secret, source="granted", grant=grant)
        g = ref["grant"]
        assert g["remaining_uses"] == 3
        assert g["expires_at"] == "2026-09-11T12:00:00"
        assert g["task_id"] == 77 and g["chain_id"] == 9
        assert ref["source"] == "granted"
        assert ref["allowed_actions"] == ["consume", "proxy_execute"]

    def test_with_grant_null_max_uses(self):
        grant = SimpleNamespace(
            grant_id="g2", from_agent_id=1, to_agent_id=2, grant_mode="consume",
            status="active", max_uses=None, used_count=None,
            expires_at=None, task_id=None, attempt_id=None, chain_id=None,
        )
        ref = _build_secret_capability_ref(self._secret(), source="granted", grant=grant)
        assert ref["grant"]["remaining_uses"] is None
        assert ref["grant"]["max_uses"] is None


# ── 密钥能力引用（owned / granted） ─────────────────────────────────


class TestBuildSecretCapabilityRefs:
    def _secret_row(self, env, agent_id, name, secret_type="api_key"):
        from models import AgentSecret
        return AgentSecret(
            agent_id=agent_id, workspace_id=env["org"].id, name=name,
            secret_type=secret_type, scope_type="agent_private",
            secret_hash=f"h_{name}", secret_encrypted="enc", prefix="pre",
            created_by_user_id=env["user"].id, updated_by_user_id=env["user"].id,
        )

    def test_owned_and_granted_refs(self, client, runtime_ctx):
        from models import AgentSecret, AgentSecretGrant, db
        agent = runtime_ctx["agent"]
        granter = Agent(
            name=f"granter_{uuid.uuid4().hex[:4]}", owner_id=runtime_ctx["user"].id,
            creator_user_id=runtime_ctx["user"].id, status=AgentStatus.ACTIVE,
        )
        db.session.add(granter)
        db.session.flush()
        owned = self._secret_row(runtime_ctx, agent.id, "owned_cfg", "api_key")
        granted_secret = self._secret_row(runtime_ctx, granter.id, "granted_cfg", "oauth_token")
        db.session.add_all([owned, granted_secret])
        db.session.flush()
        grant = AgentSecretGrant(
            grant_id=f"g_{uuid.uuid4().hex[:8]}", secret_id=granted_secret.id,
            workspace_id=agent.workspace_id, from_agent_id=granter.id,
            to_agent_id=agent.id, grant_mode="consume", max_uses=5, used_count=1,
            expires_at=datetime.utcnow() + timedelta(hours=1), status="active",
        )
        db.session.add(grant)
        db.session.commit()

        from api.agent_runtime_pull import _build_secret_capability_refs
        names, refs, grant_ids = _build_secret_capability_refs(agent)
        assert names == sorted(["owned_cfg", "granted_cfg"])
        assert grant_ids == [grant.grant_id]
        by_name = {r["name"]: r for r in refs}
        assert by_name["owned_cfg"]["source"] == "owned"
        assert by_name["owned_cfg"]["secret_type"] == "api_key"
        assert by_name["granted_cfg"]["source"] == "granted"
        assert by_name["granted_cfg"]["grant"]["grant_id"] == grant.grant_id
        assert by_name["granted_cfg"]["grant"]["remaining_uses"] == 4

    def test_inactive_grant_secret_skipped(self, client, runtime_ctx):
        from models import AgentSecret, AgentSecretGrant, db
        agent = runtime_ctx["agent"]
        granter = Agent(
            name=f"granter_{uuid.uuid4().hex[:4]}", owner_id=runtime_ctx["user"].id,
            creator_user_id=runtime_ctx["user"].id, status=AgentStatus.ACTIVE,
        )
        db.session.add(granter)
        db.session.flush()
        granted_secret = AgentSecret(
            agent_id=granter.id, workspace_id=agent.workspace_id, name="dead_cfg",
            secret_type="api_key", scope_type="agent_private",
            secret_hash="h", secret_encrypted="e", prefix="pre",
            created_by_user_id=runtime_ctx["user"].id,
            updated_by_user_id=runtime_ctx["user"].id, is_active=False,
        )
        db.session.add(granted_secret)
        db.session.flush()
        grant = AgentSecretGrant(
            grant_id=f"g_{uuid.uuid4().hex[:8]}", secret_id=granted_secret.id,
            workspace_id=agent.workspace_id, from_agent_id=granter.id,
            to_agent_id=agent.id, status="active",
        )
        db.session.add(grant)
        db.session.commit()

        from api.agent_runtime_pull import _build_secret_capability_refs
        names, refs, grant_ids = _build_secret_capability_refs(agent)
        assert "dead_cfg" not in names
        assert all(r["name"] != "dead_cfg" for r in refs)


# ── pull 端点分支 ────────────────────────────────────────────────────


class TestPullEndpoint:
    def test_non_json_body_returns_tuple_branch(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", data="not-json",
                           content_type="text/plain", headers=runtime_ctx["headers"])
        assert resp.status_code in (400, 415, 422)

    def test_invalid_max_tasks_400(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": "abc"},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 400

    def test_working_window_blocked(self, client, runtime_ctx, monkeypatch):
        import api.agent_runtime_pull as pull
        monkeypatch.setattr(pull, "evaluate_working_window",
                            lambda schedule: {"in_window": False, "next_window_at": "soon"})
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["working_window"]["blocked"] is True
        assert data["tasks"] == []

    def test_capacity_blocked(self, client, runtime_ctx, monkeypatch):
        import api.agent_runtime_pull as pull
        monkeypatch.setattr(pull, "check_dispatch_capacity",
                            lambda ws, agent_id: {"allowed": False, "reason": "full",
                                                  "active_agents": 3, "limit": 3})
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["orchestration"]["blocked"] is True
        assert data["orchestration"]["limit"] == 3

    def test_budget_block(self, client, runtime_ctx, monkeypatch):
        task = Task(
            title=f"t_{uuid.uuid4().hex[:6]}", content='{"prompt":"x"}',
            project_id=runtime_ctx["project"].id, owner_id=runtime_ctx["org"].id,
            is_ai_task=True, status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.commit()
        monkeypatch.setattr(pull, "check_budgets",
                            lambda workspace_id, agent_id, project_id: [{"kind": "spend"}])
        monkeypatch.setattr(pull, "raise_budget_exceeded", lambda **kw: None)
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["budget_block"]["blocked"] is True

    def test_success_includes_agent_profile(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert "agent_profile" in data
        assert data["tasks"] == []

    def test_stale_lease_cleanup_delete_branch(self, client, runtime_ctx):
        """过期 ACTIVE 租约 + 已存在 INACTIVE 行 → 删除过期行后可领（覆盖唯一约束处理）。"""
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease, db
        task = Task(
            title=f"t_{uuid.uuid4().hex[:6]}", content='{"prompt":"stale"}',
            project_id=runtime_ctx["project"].id, owner_id=runtime_ctx["org"].id,
            is_ai_task=True, status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.flush()
        # 已存在的 INACTIVE 行（唯一约束的占用者）
        db.session.add(AgentTaskLease(
            lease_id=f"lea_old_{uuid.uuid4().hex[:4]}", task_id=task.id,
            attempt_id=f"att_old_{uuid.uuid4().hex[:4]}", agent_id=runtime_ctx["agent"].id,
            workspace_id=runtime_ctx["org"].id,
            expires_at=datetime.utcnow() - timedelta(seconds=5), active=False,
            created_by="test",
        ))
        # 过期的 ACTIVE 租约（应被清理删除）
        db.session.add(AgentTaskLease(
            lease_id=f"lea_stale_{uuid.uuid4().hex[:4]}", task_id=task.id,
            attempt_id=f"att_stale_{uuid.uuid4().hex[:4]}", agent_id=runtime_ctx["agent"].id,
            workspace_id=runtime_ctx["org"].id,
            expires_at=datetime.utcnow() - timedelta(seconds=5), active=True,
            created_by="test",
        ))
        db.session.commit()

        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1}, headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]["tasks"]) == 1

    def test_non_dict_content_wrapped(self, client, runtime_ctx):
        task = Task(
            title=f"t_{uuid.uuid4().hex[:6]}", content='[1, 2, 3]',
            project_id=runtime_ctx["project"].id, owner_id=runtime_ctx["org"].id,
            is_ai_task=True, status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.commit()
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 3},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        tasks = resp.get_json()["data"]["tasks"]
        assert len(tasks) >= 1
        assert tasks[0]["payload"]["content"] == "[1, 2, 3]"

    def test_non_json_content_wrapped(self, client, runtime_ctx):
        task = Task(
            title=f"t_{uuid.uuid4().hex[:6]}", content='plain text prompt',
            project_id=runtime_ctx["project"].id, owner_id=runtime_ctx["org"].id,
            is_ai_task=True, status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.commit()
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 1},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        tasks = resp.get_json()["data"]["tasks"]
        assert any(t["payload"].get("content") == "plain text prompt" for t in tasks)

    def test_multi_round_break_when_exhausted(self, client, runtime_ctx):
        task = Task(
            title=f"t_{uuid.uuid4().hex[:6]}", content='{"prompt":"only one"}',
            project_id=runtime_ctx["project"].id, owner_id=runtime_ctx["org"].id,
            is_ai_task=True, status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.commit()
        resp = client.post(f"{BASE_URL}/agent/tasks/pull", json={"max_tasks": 3},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        # 只有一个可领任务 → 第二轮 break
        assert len(data["tasks"]) == 1


# ── resolve ids / fetch next task ────────────────────────────────────


class TestFetchNextTask:
    def test_empty_allowed_ids_is_unrestricted(self, runtime_ctx):
        from api.agent_runtime_pull import _fetch_next_task, _resolve_accessible_project_ids
        agent = SimpleNamespace(allowed_project_ids=[], workspace_id=runtime_ctx["org"].id)
        # 空列表 falsy → 无项目限制语义（与 None 一致）
        assert _resolve_accessible_project_ids(agent) is None
        assert _fetch_next_task(agent) is None

    def test_allowed_ids_filter(self, runtime_ctx):
        from api.agent_runtime_pull import (
            _fetch_next_task, _resolve_accessible_project_ids,
        )
        agent = SimpleNamespace(allowed_project_ids=[runtime_ctx["project"].id, "abc"],
                                workspace_id=runtime_ctx["org"].id)
        assert _resolve_accessible_project_ids(agent) is not None
        _make_task_in(runtime_ctx)
        task = _fetch_next_task(agent)
        assert task is not None

    def test_no_tasks_returns_none(self, runtime_ctx):
        from api.agent_runtime_pull import _fetch_next_task
        agent = SimpleNamespace(allowed_project_ids=None, workspace_id=runtime_ctx["org"].id)
        assert _fetch_next_task(agent) is None

    def test_active_lease_skips_task(self, runtime_ctx):
        from models import AgentTaskLease, db
        from api.agent_runtime_pull import _fetch_next_task
        agent = SimpleNamespace(allowed_project_ids=None, workspace_id=runtime_ctx["org"].id)
        task = _make_task_in(runtime_ctx)
        db.session.add(AgentTaskLease(
            lease_id=f"lea_{uuid.uuid4().hex[:6]}", task_id=task.id,
            attempt_id=f"att_{uuid.uuid4().hex[:6]}", agent_id=runtime_ctx["agent"].id,
            workspace_id=runtime_ctx["org"].id, expires_at=datetime.utcnow() + timedelta(hours=1),
            active=True, created_by="test",
        ))
        db.session.commit()
        assert _fetch_next_task(agent) is None


# ── renew / release 租约矩阵 ─────────────────────────────────────────


class TestRenewLease:
    def _lease(self, runtime_ctx, task, expired=False, active=True):
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease
        attempt_id = f"att_{uuid.uuid4().hex[:6]}"
        lease_id = f"lea_{uuid.uuid4().hex[:6]}"
        attempt = AgentTaskAttempt(
            attempt_id=attempt_id, task_id=task.id, agent_id=runtime_ctx["agent"].id,
            workspace_id=runtime_ctx["org"].id, state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id, started_at=datetime.utcnow(), created_by="test",
        )
        lease = AgentTaskLease(
            lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
            agent_id=runtime_ctx["agent"].id, workspace_id=runtime_ctx["org"].id,
            expires_at=datetime.utcnow() + timedelta(seconds=120), active=active,
            created_by="test", version=1,
        )
        db_session = None
        from models import db
        db.session.add_all([attempt, lease])
        db.session.commit()
        return attempt_id, lease_id

    def test_missing_fields_400(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/1/lease/renew", json={},
                           headers=runtime_ctx["headers"])
        assert resp.status_code in (400, 422)

    def test_lease_not_owner_409(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/1/lease/renew",
                           json={"attempt_id": "att_x", "lease_id": "lea_x"},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 409

    def test_expired_lease_409(self, client, runtime_ctx):
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease, db
        task = _make_task_in(runtime_ctx)
        attempt_id = f"att_{uuid.uuid4().hex[:6]}"
        lease_id = f"lea_{uuid.uuid4().hex[:6]}"
        attempt = AgentTaskAttempt(
            attempt_id=attempt_id, task_id=task.id, agent_id=runtime_ctx["agent"].id,
            workspace_id=runtime_ctx["org"].id, state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id, started_at=datetime.utcnow(), created_by="test",
        )
        lease = AgentTaskLease(
            lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
            agent_id=runtime_ctx["agent"].id, workspace_id=runtime_ctx["org"].id,
            expires_at=datetime.utcnow() - timedelta(seconds=30), active=True,
            created_by="test", version=1,
        )
        db.session.add_all([attempt, lease])
        db.session.commit()
        resp = client.post(f"{BASE_URL}/agent/tasks/{task.id}/lease/renew",
                           json={"attempt_id": attempt_id, "lease_id": lease_id},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 409


class TestReleaseLease:
    def _lease(self, runtime_ctx, expired=False, active=True):
        from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease
        task = _make_task_in(runtime_ctx)
        attempt_id = f"att_{uuid.uuid4().hex[:6]}"
        lease_id = f"lea_{uuid.uuid4().hex[:6]}"
        attempt = AgentTaskAttempt(
            attempt_id=attempt_id, task_id=task.id, agent_id=runtime_ctx["agent"].id,
            workspace_id=runtime_ctx["org"].id, state=AgentTaskAttemptState.ACTIVE,
            lease_id=lease_id, started_at=datetime.utcnow(), created_by="test",
        )
        lease = AgentTaskLease(
            lease_id=lease_id, task_id=task.id, attempt_id=attempt_id,
            agent_id=runtime_ctx["agent"].id, workspace_id=runtime_ctx["org"].id,
            expires_at=(datetime.utcnow() + timedelta(hours=1)) if not expired
            else (datetime.utcnow() - timedelta(seconds=30)),
            active=active, created_by="test", version=1,
        )
        from models import db
        db.session.add_all([attempt, lease])
        db.session.commit()
        return task, attempt_id, lease_id

    def test_missing_fields_400(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/1/lease/release", json={},
                           headers=runtime_ctx["headers"])
        assert resp.status_code in (400, 422)

    def test_lease_not_owner_409(self, client, runtime_ctx):
        resp = client.post(f"{BASE_URL}/agent/tasks/1/lease/release",
                           json={"attempt_id": "att_x", "lease_id": "lea_x"},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 409

    def test_already_released_returns_was_active_false(self, client, runtime_ctx):
        task, attempt_id, lease_id = self._lease(runtime_ctx, active=False)
        resp = client.post(f"{BASE_URL}/agent/tasks/{task.id}/lease/release",
                           json={"attempt_id": attempt_id, "lease_id": lease_id},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["was_active"] is False

    def test_expired_active_lease_409(self, client, runtime_ctx):
        task, attempt_id, lease_id = self._lease(runtime_ctx, expired=True, active=True)
        resp = client.post(f"{BASE_URL}/agent/tasks/{task.id}/lease/release",
                           json={"attempt_id": attempt_id, "lease_id": lease_id},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 409

    def test_release_active_lease_success(self, client, runtime_ctx):
        task, attempt_id, lease_id = self._lease(runtime_ctx, active=True)
        resp = client.post(f"{BASE_URL}/agent/tasks/{task.id}/lease/release",
                           json={"attempt_id": attempt_id, "lease_id": lease_id},
                           headers=runtime_ctx["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["was_active"] is True
