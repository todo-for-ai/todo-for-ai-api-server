"""GoalLoop 路由缺口补测（api/projects/routes_goal_loops.py）。

补齐此前未覆盖的分支：列表端点全路径、创建校验（title/goal_text/stall_limit/
无可用 Agent/服务层异常）、详情 404/403、护栏调整 404/非法值/终止态以外的
服务层异常、pause|resume|stop 的循环缺失与工程越权、kick 端点全路径。
"""

import uuid

import pytest

from app import create_app
from models import db

from services import goal_loop_service as facade


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
    """owner + org + project + 可用 Agent + owner JWT（无角色模板的最小集）。"""
    from flask_jwt_extended import create_access_token
    from models import Agent, Organization, Project, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(
        name=f"agent_{uuid.uuid4().hex[:6]}",
        workspace_id=org.id,
        owner_id=user.id,
        creator_user_id=user.id,
        status="ACTIVE",
        runner_enabled=True,
    )
    db.session.add(agent)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "agent": agent, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
def outsider(_isolated_app):
    """对项目无任何访问权的第二用户。"""
    from flask_jwt_extended import create_access_token
    from models import User

    user = User(username=f"o_{uuid.uuid4().hex[:8]}", email=f"o_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


def _create_loop_payload(**overrides):
    payload = {
        "title": "夜跑目标",
        "goal_text": "连续七天完成构建",
        "done_definition": "七天全绿",
        "rounds_limit": 3,
    }
    payload.update(overrides)
    return payload


def _mk_loop(env, monkeypatch):
    """绕过路由直接造一个循环（挂到 env['project']）。"""
    return facade.create_loop(
        project=env["project"],
        agent=env["agent"],
        title="既有循环",
        goal_text="目标",
        done_definition="完成",
        rounds_limit=5,
        created_by=env["user"].id,
    )


class TestListGoalLoops:
    def test_project_not_found(self, client, env):
        resp = client.get(
            "/todo-for-ai/api/v1/projects/999999/goal-loops", headers=env["headers"])
        assert resp.status_code == 404

    def test_forbidden_for_outsider(self, client, env, outsider):
        resp = client.get(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=outsider["headers"])
        assert resp.status_code == 403

    def test_lists_loops_with_rounds(self, client, env):
        _mk_loop(env, None)
        resp = client.get(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"])
        assert resp.status_code == 200
        loops = resp.get_json()["data"]["goal_loops"]
        assert len(loops) == 1
        assert loops[0]["rounds_done"] == 0

    def test_list_failure_maps_to_500(self, client, env, monkeypatch):
        _mk_loop(env, None)  # 有循环才会进入取任务列表的异常路径
        monkeypatch.setattr(
            facade, "loop_tasks",
            lambda _id: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = client.get(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"])
        assert resp.status_code == 500


class TestCreateValidation:
    def test_project_not_found(self, client, env):
        resp = client.post(
            "/todo-for-ai/api/v1/projects/999999/goal-loops",
            headers=env["headers"], json=_create_loop_payload())
        assert resp.status_code == 404

    def test_title_required(self, client, env):
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"], json=_create_loop_payload(title="   "))
        assert resp.status_code == 400
        assert "title" in resp.get_json()["message"]

    def test_goal_text_required(self, client, env):
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"],
            json=_create_loop_payload(goal_text=""))
        assert resp.status_code == 400
        assert "goal_text" in resp.get_json()["message"]

    def test_stall_limit_out_of_range(self, client, env):
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"], json=_create_loop_payload(stall_limit=51))
        assert resp.status_code == 400
        assert "stall_limit" in resp.get_json()["message"]

    def test_non_integer_guardrail(self, client, env):
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"], json=_create_loop_payload(stall_limit="many"))
        assert resp.status_code == 400
        assert "integers" in resp.get_json()["message"]

    def test_no_active_agent(self, client, env):
        env["agent"].runner_enabled = False
        db.session.commit()
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"], json=_create_loop_payload())
        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "NO_ACTIVE_AGENT"

    def test_service_failure_maps_to_500(self, client, env, monkeypatch):
        monkeypatch.setattr(
            facade, "create_loop",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/{env['project'].id}/goal-loops",
            headers=env["headers"], json=_create_loop_payload())
        assert resp.status_code == 500


class TestGetGoalLoop:
    def test_loop_not_found(self, client, env):
        resp = client.get(
            "/todo-for-ai/api/v1/projects/goal-loops/999999", headers=env["headers"])
        assert resp.status_code == 404

    def test_forbidden_for_outsider(self, client, env, outsider):
        loop = _mk_loop(env, None)
        resp = client.get(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}",
            headers=outsider["headers"])
        assert resp.status_code == 403

    def test_detail_failure_maps_to_500(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)
        monkeypatch.setattr(
            facade, "loop_tasks",
            lambda _id: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = client.get(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}",
            headers=env["headers"])
        assert resp.status_code == 500


class TestUpdateGuardrails:
    def test_loop_not_found(self, client, env):
        resp = client.put(
            "/todo-for-ai/api/v1/projects/goal-loops/999999",
            headers=env["headers"], json={"rounds_limit": 5})
        assert resp.status_code == 404

    def test_stall_limit_out_of_range(self, client, env):
        loop = _mk_loop(env, None)
        resp = client.put(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}",
            headers=env["headers"], json={"stall_limit": 0.5})
        assert resp.status_code == 400
        assert "stall_limit" in resp.get_json()["message"]

    def test_non_integer_guardrail(self, client, env):
        loop = _mk_loop(env, None)
        resp = client.put(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}",
            headers=env["headers"], json={"rounds_limit": "ten"})
        assert resp.status_code == 400
        assert "integers" in resp.get_json()["message"]

    def test_lookup_error_maps_to_404(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)

        def _raise(*a, **kw):
            raise LookupError("gone")
        monkeypatch.setattr(facade, "update_guardrails", _raise)
        resp = client.put(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}",
            headers=env["headers"], json={"rounds_limit": 8})
        assert resp.status_code == 404

    def test_unexpected_failure_maps_to_500(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)

        def _raise(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(facade, "update_guardrails", _raise)
        resp = client.put(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}",
            headers=env["headers"], json={"rounds_limit": 8})
        assert resp.status_code == 500


class TestLoopActions:
    def test_pause_loop_not_found(self, client, env):
        resp = client.post(
            "/todo-for-ai/api/v1/projects/goal-loops/999999/pause",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_action_project_missing_maps_to_404(self, client, env):
        loop = _mk_loop(env, None)
        loop.project_id = 999999
        db.session.commit()
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}/stop",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_set_status_lookup_error_maps_to_404(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)

        def _raise(*a, **kw):
            raise LookupError("gone")
        monkeypatch.setattr(facade, "set_status", _raise)
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}/pause",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_action_unexpected_failure_maps_to_500(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)

        def _raise(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(facade, "set_status", _raise)
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}/pause",
            headers=env["headers"])
        assert resp.status_code == 500


class TestKick:
    def test_kick_loop_not_found(self, client, env):
        resp = client.post(
            "/todo-for-ai/api/v1/projects/goal-loops/999999/kick",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_kick_project_missing_maps_to_404(self, client, env):
        loop = _mk_loop(env, None)
        loop.project_id = 999999
        db.session.commit()
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}/kick",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_kick_success(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)
        monkeypatch.setattr(
            facade, "maybe_advance",
            lambda _id: {"advanced": True, "reason": "kicked"})
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}/kick",
            headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["result"]["advanced"] is True
        assert data["loop"]["id"] == loop.id

    def test_kick_failure_maps_to_500(self, client, env, monkeypatch):
        loop = _mk_loop(env, None)

        def _raise(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(facade, "maybe_advance", _raise)
        resp = client.post(
            f"/todo-for-ai/api/v1/projects/goal-loops/{loop.id}/kick",
            headers=env["headers"])
        assert resp.status_code == 500
