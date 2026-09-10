"""迭代 47：_core.py 拆分后 agents_crud / agent_assignments 全路由回归。

行为钉子（拆分前逐行转录，含两处修复）：
- review-queue 默认 action=all 的 or_/and_ 过滤（原文件缺导入必 500）；
- self-register 的 AuditLog.record 用 resource_type/resource_id
  （原 target_type/target_id 是 TypeError：Agent 已建但调用方收 500）。
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from models import db, TaskAssignmentState


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
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def env(_isolated_app):
    from flask_jwt_extended import create_access_token
    from models import Agent, Organization, Project, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(project)
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        owner_id=user.id,
        creator_user_id=user.id,
        status="ACTIVE",
    )
    db.session.add(agent)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "project": project, "agent": agent,
        "headers": {"Authorization": f"Bearer {token}"},
    }


BASE = "/todo-for-ai/api/v1/agents"


def _make_task(env, status="todo", is_ai=True):
    from models import Task, TaskStatus

    task = Task(
        title=f"t_{uuid.uuid4().hex[:6]}",
        content="c",
        project_id=env["project"].id,
        owner_id=env["user"].id,
        is_ai_task=is_ai,
        status=TaskStatus(status) if isinstance(status, str) else status,
    )
    db.session.add(task)
    db.session.commit()
    return task


def _make_assignment(env, task, state="claimed", lease_expires_at=None):
    from models import TaskAssignment, TaskAssignmentState

    assignment = TaskAssignment(
        task_id=task.id,
        agent_id=env["agent"].id,
        assigned_by_user_id=env["user"].id,
        state=TaskAssignmentState(state) if isinstance(state, str) else state,
        lease_expires_at=lease_expires_at or datetime.utcnow() + timedelta(hours=1),
        claimed_at=datetime.utcnow(),
        last_heartbeat_at=datetime.utcnow(),
        progress_rate=0,
        created_by=env["user"].email,
    )
    db.session.add(assignment)
    db.session.commit()
    return assignment


@pytest.fixture
def quiet(monkeypatch):
    """默认静默所有横切助手：清扫函数零动作、SSE 不冲刷。"""
    import api.agents.agents_crud as ac
    import api.agents.agent_assignments as aa

    monkeypatch.setattr(ac, "mark_stale_agents_offline", lambda **kw: 0)
    monkeypatch.setattr(ac, "expire_stale_assignments", lambda **kw: 0)
    monkeypatch.setattr(aa, "mark_stale_agents_offline", lambda **kw: 0)
    monkeypatch.setattr(aa, "expire_stale_assignments", lambda **kw: 0)
    monkeypatch.setattr(aa, "expire_stale_assignments_for_task", lambda task_id: None)
    monkeypatch.setattr(aa, "expire_assignment", lambda assignment: None)
    monkeypatch.setattr(aa, "flush_sse_notifications", lambda: None)
    monkeypatch.setattr(aa, "record_task_event", lambda *a, **kw: None)
    monkeypatch.setattr(aa, "build_task_snapshot", lambda task: {"title": task.title})
    return monkeypatch


# ── agents_crud：列表 / 创建 ─────────────────────────────────────────


class TestListAgents:
    def test_success_default(self, client, env, quiet):
        resp = client.get(BASE, headers=env["headers"])
        assert resp.status_code == 200
        assert "items" in resp.get_json()["data"]

    def test_sweep_commit_branch(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "mark_stale_agents_offline", lambda **kw: 1)
        quiet.setattr(ac, "expire_stale_assignments", lambda **kw: 1)
        assert client.get(BASE, headers=env["headers"]).status_code == 200

    def test_status_filter(self, client, env, quiet):
        assert client.get(BASE, query_string={"status": "active"}, headers=env["headers"]).status_code == 200

    def test_status_filter_invalid(self, client, env, quiet):
        assert client.get(BASE, query_string={"status": "bogus"}, headers=env["headers"]).status_code == 400

    def test_search(self, client, env, quiet):
        assert client.get(BASE, query_string={"search": "agent"}, headers=env["headers"]).status_code == 200

    def test_sort_branches(self, client, env, quiet):
        assert client.get(BASE, query_string={"sort_by": "name"}, headers=env["headers"]).status_code == 200
        assert client.get(BASE, query_string={"sort_by": "last_seen_at", "sort_order": "desc"},
                          headers=env["headers"]).status_code == 200

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "mark_stale_agents_offline", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        assert client.get(BASE, headers=env["headers"]).status_code == 500


class TestCreateAgent:
    def test_missing_name_400(self, client, env, quiet):
        resp = client.post(BASE, json={}, headers=env["headers"])
        assert resp.status_code in (400, 422)

    def test_invalid_working_schedule_400(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "normalize_working_schedule",
                      lambda v: (_ for _ in ()).throw(ValueError("bad window")))
        resp = client.post(BASE, json={"name": "a", "working_schedule": {"x": 1}}, headers=env["headers"])
        assert resp.status_code == 400

    def test_invalid_kind_400(self, client, env, quiet):
        resp = client.post(BASE, json={"name": "a", "kind": "bogus"}, headers=env["headers"])
        assert resp.status_code == 400

    def test_valid_working_schedule_success(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "normalize_working_schedule", lambda v: {"normalized": True})
        resp = client.post(BASE, json={"name": "a", "working_schedule": {"raw": 1}},
                           headers=env["headers"])
        assert resp.status_code in (200, 201)

    def test_success_writes_audit_log(self, client, env, quiet):
        from models import AuditLog
        name = f"created_{uuid.uuid4().hex[:6]}"
        resp = client.post(BASE, json={"name": name}, headers=env["headers"])
        assert resp.status_code in (200, 201)
        row = AuditLog.query.filter_by(action="agent.created").order_by(AuditLog.id.desc()).first()
        assert row is not None and row.resource_type == "agent"

    def test_inactive_status_skips_last_seen(self, client, env, quiet):
        resp = client.post(BASE, json={"name": f"off_{uuid.uuid4().hex[:4]}", "status": "inactive"},
                           headers=env["headers"])
        assert resp.status_code in (200, 201)

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "_client_ip", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = client.post(BASE, json={"name": "a"}, headers=env["headers"])
        assert resp.status_code == 500


# ── agents_crud：自助注册（本轮修复 AuditLog kwargs） ────────────────


class TestSelfRegister:
    def _post(self, client, env, **overrides):
        payload = {"name": f"sr_{uuid.uuid4().hex[:6]}", "provider": "cli"}
        payload.update(overrides)
        return client.post(f"{BASE}/self-register", json=payload, headers=env["headers"])

    def test_update_existing_uses_resource_kwargs(self, client, env, quiet):
        from models import Agent, AuditLog
        agent = Agent(name="sr_fixed", provider="cli", owner_id=env["user"].id, status="INACTIVE")
        db.session.add(agent)
        db.session.commit()
        resp = client.post(f"{BASE}/self-register",
                           json={"name": "sr_fixed", "provider": "cli",
                                 "description": "d", "model": "m", "capabilities": ["c"],
                                 "config": {"k": 1}, "collaboration_role": "leader"},
                           headers=env["headers"])
        assert resp.status_code == 200
        assert agent.description == "d" and agent.model == "m"
        assert agent.capabilities == ["c"] and agent.config == {"k": 1}
        assert agent.collaboration_role == "leader"
        row = AuditLog.query.filter_by(action="agent.self_register").order_by(AuditLog.id.desc()).first()
        assert row is not None and row.resource_type == "agent" and row.resource_id == agent.id
        assert row.detail.get("action") == "updated"

    def test_create_new_success(self, client, env, quiet):
        resp = self._post(client, env)
        assert resp.status_code in (200, 201)
        from models import AuditLog
        row = AuditLog.query.filter_by(action="agent.self_register").order_by(AuditLog.id.desc()).first()
        assert row is not None and row.detail.get("action") == "created"

    def test_invalid_kind_400(self, client, env, quiet):
        resp = client.post(f"{BASE}/self-register",
                           json={"name": f"k_{uuid.uuid4().hex[:4]}", "kind": "bogus"},
                           headers=env["headers"])
        assert resp.status_code == 400

    def test_missing_name_400(self, client, env, quiet):
        assert client.post(f"{BASE}/self-register", json={}, headers=env["headers"]).status_code in (400, 422)

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "_client_ip", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert client.post(f"{BASE}/self-register", json={"name": "x"}, headers=env["headers"]).status_code == 500


# ── agents_crud：发现 / 详情 / 更新 / 心跳 ───────────────────────────


class TestDiscoverAgents:
    def test_default_active(self, client, env, quiet):
        assert client.get(f"{BASE}/discover", headers=env["headers"]).status_code == 200

    def test_filters(self, client, env, quiet):
        resp = client.get(f"{BASE}/discover",
                          query_string=[("kind", "assistant"), ("collaboration_role", "leader"),
                                  ("capability", "code"), ("capability", "review")],
                          headers=env["headers"])
        assert resp.status_code == 200

    def test_invalid_status_and_kind_ignored(self, client, env, quiet):
        resp = client.get(f"{BASE}/discover", query_string={"status": "bogus", "kind": "bogus"},
                          headers=env["headers"])
        assert resp.status_code == 200

    def test_capability_postfilter_loop(self, client, env, quiet):
        env["agent"].capabilities = ["code", "review"]
        db.session.commit()
        resp = client.get(f"{BASE}/discover", query_string={"capability": "code"},
                          headers=env["headers"])
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]) == 1

    def test_get_agent_sweep_commit(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "mark_stale_agents_offline", lambda **kw: 1)
        quiet.setattr(ac, "expire_stale_assignments", lambda **kw: 1)
        assert client.get(f"{BASE}/{env['agent'].id}", headers=env["headers"]).status_code == 200


class TestGetAgent:
    def test_404(self, client, env, quiet):
        assert client.get(f"{BASE}/99999999", headers=env["headers"]).status_code == 404

    def test_success(self, client, env, quiet):
        assert client.get(f"{BASE}/{env['agent'].id}", headers=env["headers"]).status_code == 200


class TestUpdateAgent:
    def _put(self, client, env, payload, agent_id=None):
        return client.put(f"{BASE}/{agent_id or env['agent'].id}", json=payload, headers=env["headers"])

    def test_404(self, client, env, quiet):
        assert self._put(client, env, {"name": "x"}, agent_id=99999999).status_code == 404

    def test_invalid_working_schedule_400(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "normalize_working_schedule",
                      lambda v: (_ for _ in ()).throw(ValueError("bad")))
        assert self._put(client, env, {"working_schedule": {}}).status_code == 400

    def test_invalid_body_returns_tuple_branch(self, client, env, quiet):
        resp = client.put(
            f"{BASE}/{env['agent'].id}",
            data="not-json",
            content_type="text/plain",
            headers=env["headers"],
        )
        assert resp.status_code in (400, 415, 422)

    def test_role_template_empty_clears(self, client, env, quiet):
        assert self._put(client, env, {"role_template_id": ""}).status_code == 200

    def test_role_template_missing_400(self, client, env, quiet):
        assert self._put(client, env, {"role_template_id": 987654}).status_code == 400

    def test_role_template_builtin_ok(self, client, env, quiet):
        from models import AgentRoleTemplate, AgentRoleTemplateStatus
        tpl = AgentRoleTemplate(name=f"tpl_{uuid.uuid4().hex[:6]}", display_name="内置模板",
                                is_builtin=True, status=AgentRoleTemplateStatus.ACTIVE,
                                created_by_user_id=env["user"].id)
        db.session.add(tpl)
        db.session.commit()
        assert self._put(client, env, {"role_template_id": tpl.id}).status_code == 200

    def test_role_template_foreign_400(self, client, env, quiet):
        from models import AgentRoleTemplate, AgentRoleTemplateStatus
        tpl = AgentRoleTemplate(name=f"tpl_{uuid.uuid4().hex[:6]}", display_name="外部模板",
                                is_builtin=False, workspace_id=(env["org"].id or 0) + 999,
                                status=AgentRoleTemplateStatus.ACTIVE,
                                created_by_user_id=env["user"].id)
        db.session.add(tpl)
        db.session.commit()
        assert self._put(client, env, {"role_template_id": tpl.id}).status_code == 400

    def test_invalid_kind_and_status_400(self, client, env, quiet):
        assert self._put(client, env, {"kind": "bogus"}).status_code == 400
        assert self._put(client, env, {"status": "bogus"}).status_code == 400

    def test_capabilities_merge(self, client, env, quiet):
        env["agent"].capabilities = ["code"]
        db.session.commit()
        resp = self._put(client, env, {"name": "  renamed  ", "capabilities": ["review"]})
        assert resp.status_code == 200
        assert set(env["agent"].capabilities) == {"code", "review"}
        assert env["agent"].name == "renamed"

    def test_status_active_touches_last_seen(self, client, env, quiet):
        env["agent"].last_seen_at = None
        db.session.commit()
        assert self._put(client, env, {"status": "active"}).status_code == 200
        assert env["agent"].last_seen_at is not None

    def test_config_change_notifies(self, client, env, quiet):
        import api.agents.agents_crud as ac
        sent = []
        quiet.setattr(ac, "_queue_sse", lambda user_id, et, payload: sent.append(et))
        resp = self._put(client, env, {"config": {"k": 1}})
        assert resp.status_code == 200
        assert sent == ["agent_config_changed"]

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "_queue_sse", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        assert self._put(client, env, {"config": {"k": 1}}).status_code == 500


class TestHeartbeat:
    def test_with_status(self, client, env, quiet):
        resp = client.post(f"{BASE}/{env['agent'].id}/heartbeat", json={"status": "active"},
                           headers=env["headers"])
        assert resp.status_code == 200

    def test_invalid_status_400(self, client, env, quiet):
        resp = client.post(f"{BASE}/{env['agent'].id}/heartbeat", json={"status": "bogus"},
                           headers=env["headers"])
        assert resp.status_code == 400

    def test_default_heartbeat_branch(self, client, env, quiet):
        resp = client.post(f"{BASE}/{env['agent'].id}/heartbeat", json={}, headers=env["headers"])
        assert resp.status_code == 200

    def test_404(self, client, env, quiet):
        assert client.post(f"{BASE}/99999999/heartbeat", json={}, headers=env["headers"]).status_code == 404

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agents_crud as ac
        quiet.setattr(ac, "get_owned_agent_or_response",
                      lambda agent_id, user: (_ for _ in ()).throw(RuntimeError("boom")))
        assert client.post(f"{BASE}/{env['agent'].id}/heartbeat", json={}, headers=env["headers"]).status_code == 500


# ── agent_assignments：审查队列（本轮修复 or_/and_） ─────────────────


class TestReviewQueue:
    def test_action_all_default_hits_or_and_filter(self, client, env, quiet):
        task = _make_task(env, status="review")
        _make_assignment(env, task, state="waiting_human")
        resp = client.get(f"{BASE}/review-queue", headers=env["headers"])
        assert resp.status_code == 200  # 原缺陷：or_/and_ 未导入 → 500

    def test_action_human_feedback(self, client, env, quiet):
        assert client.get(f"{BASE}/review-queue", query_string={"action": "human_feedback"},
                          headers=env["headers"]).status_code == 200

    def test_action_final_review(self, client, env, quiet):
        task = _make_task(env, status="review")
        _make_assignment(env, task, state="done")
        resp = client.get(f"{BASE}/review-queue", query_string={"action": "final_review"},
                          headers=env["headers"])
        assert resp.status_code == 200

    def test_invalid_action_400(self, client, env, quiet):
        resp = client.get(f"{BASE}/review-queue", query_string={"action": "bogus"}, headers=env["headers"])
        assert resp.status_code == 400

    def test_sweep_commit_branch(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "mark_stale_agents_offline", lambda **kw: 1)
        quiet.setattr(aa, "expire_stale_assignments", lambda **kw: 1)
        assert client.get(f"{BASE}/review-queue", headers=env["headers"]).status_code == 200

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "mark_stale_agents_offline", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
        assert client.get(f"{BASE}/review-queue", headers=env["headers"]).status_code == 500


# ── agent_assignments：推荐 / 认领 ───────────────────────────────────


class TestRecommendedTasks:
    def test_404(self, client, env, quiet):
        assert client.get(f"{BASE}/99999999/recommended-tasks", headers=env["headers"]).status_code == 404

    def test_empty_with_zero_scores(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "score_task_for_agent",
                      lambda task, agent: {"score": 0, "matched_capabilities": [], "matched_tags": [],
                                           "matched_text": [], "missing_required": []})
        _make_task(env)
        resp = client.get(f"{BASE}/{env['agent'].id}/recommended-tasks", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"] == []

    def test_scored_and_project_filter_and_limit(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "score_task_for_agent",
                      lambda task, agent: {"score": 5, "matched_capabilities": ["code"], "matched_tags": [],
                                           "matched_text": [], "missing_required": []})
        _make_task(env)
        resp = client.get(f"{BASE}/{env['agent'].id}/recommended-tasks",
                          query_string={"project_id": env["project"].id, "limit": 5},
                          headers=env["headers"])
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]) == 1


class TestListAgentAssignments:
    def test_success(self, client, env, quiet):
        task = _make_task(env)
        _make_assignment(env, task)
        resp = client.get(f"{BASE}/{env['agent'].id}/assignments", headers=env["headers"])
        assert resp.status_code == 200

    def test_404(self, client, env, quiet):
        assert client.get(f"{BASE}/99999999/assignments", headers=env["headers"]).status_code == 404

    def test_sweep_commit_branch(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "mark_stale_agents_offline", lambda **kw: 1)
        quiet.setattr(aa, "expire_stale_assignments", lambda **kw: 1)
        assert client.get(f"{BASE}/{env['agent'].id}/assignments", headers=env["headers"]).status_code == 200

    def test_invalid_state_400(self, client, env, quiet):
        resp = client.get(f"{BASE}/{env['agent'].id}/assignments", query_string={"state": "bogus"},
                          headers=env["headers"])
        assert resp.status_code == 400

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "mark_stale_agents_offline", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
        assert client.get(f"{BASE}/{env['agent'].id}/assignments", headers=env["headers"]).status_code == 500


class TestClaimTask:
    def _claim(self, client, env, agent_id=None, payload=None):
        return client.post(f"{BASE}/{agent_id or env['agent'].id}/claim",
                           json=payload or {}, headers=env["headers"])

    def test_disabled_agent_409(self, client, env, quiet):
        from models import AgentStatus
        env["agent"].status = AgentStatus.DISABLED
        db.session.commit()
        assert self._claim(client, env).status_code == 409

    def test_unknown_agent_404(self, client, env, quiet):
        assert self._claim(client, env, agent_id=99999999).status_code == 404

    def test_out_of_working_window_409(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "is_in_working_window", lambda schedule: False)
        quiet.setattr(aa, "evaluate_working_window", lambda schedule: {"next_window_at": "soon"})
        resp = self._claim(client, env)
        assert resp.status_code == 409
        assert resp.get_json()["next_window_at"] == "soon"

    def test_explicit_task_already_active_409(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        active = SimpleNamespace(to_dict=lambda include_agent=False: {"id": 1})
        quiet.setattr(aa, "find_active_assignment", lambda task_id, for_update=False: active)
        resp = self._claim(client, env, payload={"task_id": task.id})
        assert resp.status_code == 409
        assert "assignment" in resp.get_json()

    def test_explicit_task_404(self, client, env, quiet):
        assert self._claim(client, env, payload={"task_id": 987654}).status_code == 404

    def test_no_claimable_task_200_null(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        quiet.setattr(aa, "find_claimable_task", lambda user, **kw: (None, None))
        resp = self._claim(client, env)
        assert resp.status_code == 200
        assert resp.get_json().get("data") is None

    def test_agent_claim_success_offline_wake(self, client, env, quiet):
        from models import AgentStatus
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        env["agent"].status = AgentStatus.OFFLINE
        db.session.commit()
        quiet.setattr(aa, "find_claimable_task",
                      lambda user, **kw: (task, {"capabilities": ["code"]}))
        resp = self._claim(client, env, payload={"lease_seconds": 1})
        assert resp.status_code in (200, 201)
        data = resp.get_json()["data"]
        assert data["assignment"]["task_id"] == task.id
        assert data["run"]["id"] is not None
        assert env["agent"].status == AgentStatus.ACTIVE
        assert data["assignment"]["lease_expires_at"] is not None

    def test_manual_dispatch_success(self, client, env, quiet):
        task = _make_task(env)
        resp = self._claim(client, env, payload={"task_id": task.id, "dispatch_source": "human",
                                                 "lease_seconds": 999999})
        assert resp.status_code in (200, 201)
        assert resp.get_json()["data"]["run"]["id"]

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        quiet.setattr(aa, "record_task_event",
                      lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = self._claim(client, env, payload={"task_id": task.id, "dispatch_source": "human"})
        assert resp.status_code == 500


# ── agent_assignments：分配查询与更新 ────────────────────────────────


class TestListTaskAssignments:
    def test_404(self, client, env, quiet):
        assert client.get(f"{BASE}/tasks/987654/assignments", headers=env["headers"]).status_code == 404

    def test_success(self, client, env, quiet):
        task = _make_task(env)
        _make_assignment(env, task)
        assert client.get(f"{BASE}/tasks/{task.id}/assignments", headers=env["headers"]).status_code == 200

    def test_state_active(self, client, env, quiet):
        task = _make_task(env)
        _make_assignment(env, task, state="running")
        resp = client.get(f"{BASE}/tasks/{task.id}/assignments", query_string={"state": "active"},
                          headers=env["headers"])
        assert resp.status_code == 200

    def test_invalid_state_400(self, client, env, quiet):
        task = _make_task(env)
        resp = client.get(f"{BASE}/tasks/{task.id}/assignments", query_string={"state": "bogus"},
                          headers=env["headers"])
        assert resp.status_code == 400

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        quiet.setattr(aa, "expire_stale_assignments_for_task",
                      lambda task_id: (_ for _ in ()).throw(RuntimeError("x")))
        assert client.get(f"{BASE}/tasks/{task.id}/assignments", headers=env["headers"]).status_code == 500


class TestUpdateAssignment:
    def _put(self, client, env, assignment, payload=None, agent_id=None):
        return client.put(
            f"{BASE}/{agent_id or env['agent'].id}/assignments/{assignment.id}",
            json=payload or {"progress_rate": 50},
            headers=env["headers"],
        )

    def test_agent_not_found_404(self, client, env, quiet):
        task = _make_task(env)
        assignment = _make_assignment(env, task)
        resp = client.put(
            f"{BASE}/99999999/assignments/{assignment.id}",
            json={"progress_rate": 50},
            headers=env["headers"],
        )
        assert resp.status_code == 404

    def test_assignment_not_found_404(self, client, env, quiet):
        task = _make_task(env)
        _make_assignment(env, task)
        resp = client.put(
            f"{BASE}/{env['agent'].id}/assignments/987654",
            json={"progress_rate": 50},
            headers=env["headers"],
        )
        assert resp.status_code == 404

    def test_invalid_body_returns_tuple_branch(self, client, env, quiet):
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        resp = client.put(
            f"{BASE}/{env['agent'].id}/assignments/{assignment.id}",
            data="not-json",
            content_type="text/plain",
            headers=env["headers"],
        )
        assert resp.status_code in (400, 415, 422)

    def test_task_404_when_project_owned_by_other(self, client, env, quiet):
        from models import Project, User
        other = User(username=f"o_{uuid.uuid4().hex[:8]}", email=f"o_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(other)
        db.session.flush()
        foreign_proj = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=other.id)
        db.session.add(foreign_proj)
        db.session.flush()
        task = _make_task(env)
        assignment = _make_assignment(env, task)
        task.project_id = foreign_proj.id
        db.session.commit()
        resp = self._put(client, env, assignment)
        assert resp.status_code == 404

    def test_expired_lease_409(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task)
        quiet.setattr(aa, "expire_assignment", lambda a: True)
        resp = self._put(client, env, assignment)
        assert resp.status_code == 409

    def test_assignment_update_error_path(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "apply_assignment_update",
                      lambda user, a, t, data, actor_agent: (_ for _ in ()).throw(
                          aa.AssignmentUpdateError("nope", 409)))
        assert assignment.state == TaskAssignmentState.RUNNING
        resp = self._put(client, env, assignment)
        assert resp.status_code == 409

    def test_value_error_path_400(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "apply_assignment_update",
                      lambda user, a, t, data, actor_agent: (_ for _ in ()).throw(ValueError("bad")))
        assert self._put(client, env, assignment).status_code == 400

    def test_success_with_run(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        fake_run = SimpleNamespace(to_dict=lambda: {"id": 7})
        quiet.setattr(aa, "apply_assignment_update",
                      lambda user, a, t, data, actor_agent: fake_run)
        resp = self._put(client, env, assignment)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["run"] == {"id": 7}

    def test_success_without_run(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "apply_assignment_update", lambda user, a, t, data, actor_agent: None)
        resp = self._put(client, env, assignment)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["run"] is None

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "expire_assignment",
                      lambda a: (_ for _ in ()).throw(RuntimeError("x")))
        assert self._put(client, env, assignment).status_code == 500


class TestUpdateTaskAssignment:
    def _put(self, client, env, task, assignment, payload=None):
        return client.put(
            f"{BASE}/tasks/{task.id}/assignments/{assignment.id}",
            json=payload or {"progress_rate": 10},
            headers=env["headers"],
        )

    def test_assignment_not_found_404(self, client, env, quiet):
        task = _make_task(env)
        assignment = _make_assignment(env, task)
        resp = client.put(
            f"{BASE}/tasks/{task.id}/assignments/{assignment.id + 999}",
            json={"progress_rate": 1}, headers=env["headers"])
        assert resp.status_code == 404

    def test_expired_lease_409(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task)
        quiet.setattr(aa, "expire_assignment", lambda a: True)
        assert self._put(client, env, task, assignment).status_code == 409

    def test_assignment_update_error_path(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "apply_assignment_update",
                      lambda user, a, t, data, actor_agent: (_ for _ in ()).throw(
                          aa.AssignmentUpdateError("denied", 403)))
        resp = self._put(client, env, task, assignment)
        assert resp.status_code == 403

    def test_value_error_path_400(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "apply_assignment_update",
                      lambda user, a, t, data, actor_agent: (_ for _ in ()).throw(ValueError("bad")))
        assert self._put(client, env, task, assignment).status_code == 400

    def test_task_not_found_404(self, client, env, quiet):
        resp = client.put(
            f"{BASE}/tasks/987654/assignments/1",
            json={"progress_rate": 1}, headers=env["headers"])
        assert resp.status_code == 404

    def test_invalid_body_returns_tuple_branch(self, client, env, quiet):
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        resp = client.put(
            f"{BASE}/tasks/{task.id}/assignments/{assignment.id}",
            data="not-json",
            content_type="text/plain",
            headers=env["headers"],
        )
        assert resp.status_code in (400, 415, 422)

    def test_success(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        fake_run = SimpleNamespace(to_dict=lambda: {"id": 9})
        quiet.setattr(aa, "apply_assignment_update",
                      lambda user, a, t, data, actor_agent: fake_run)
        resp = self._put(client, env, task, assignment)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["run"] == {"id": 9}

    def test_internal_error_500(self, client, env, quiet):
        import api.agents.agent_assignments as aa
        task = _make_task(env)
        assignment = _make_assignment(env, task, state="running")
        quiet.setattr(aa, "expire_assignment",
                      lambda a: (_ for _ in ()).throw(RuntimeError("x")))
        assert self._put(client, env, task, assignment).status_code == 500
