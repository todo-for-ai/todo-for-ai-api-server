"""Agent 云端运行时管理端点回归（api/agent_runtime_mgmt.py）。

覆盖 spawn/terminate/status/list/settings 全端点的鉴权、幂等护栏、
凭据解密降级、配额校验分支；业务断言同时盯 Agent 执行模式落库。
"""

import pytest

from app import create_app
from models import db


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
    """owner + 工作区 + 归属该工作区的 Agent + owner JWT。"""
    import uuid
    from flask_jwt_extended import create_access_token
    from models import Agent, Organization, User

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
    )
    db.session.add(agent)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "agent": agent,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
def outsider(_isolated_app):
    """无工作区访问权的第二用户（用于 403 分支）。"""
    import uuid
    from flask_jwt_extended import create_access_token
    from models import User

    user = User(username=f"o_{uuid.uuid4().hex[:8]}", email=f"o_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def fake_controller(monkeypatch):
    """替换业务层引用的后端获取入口（部署级 get_runtime_provider 与
    按 Agent 解析的 get_runtime_provider_for_agent 都指向同一个桩）。"""
    from unittest.mock import MagicMock
    from services.cloud_runtime import management

    controller = MagicMock()
    controller.name = "fake"
    controller.get_runtime_status.return_value = None
    controller.list_runtimes.return_value = []
    controller.spawn.return_value = {"pod_name": "agent-1-abc", "status": "creating"}
    controller.terminate.return_value = True
    monkeypatch.setattr(management, "get_runtime_provider", lambda kind=None: controller)
    monkeypatch.setattr(management, "get_runtime_provider_for_agent",
                        lambda agent: controller)
    return controller


def _spawn_url(env):
    return f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/{env['agent'].id}/runtime/spawn"


class TestSpawnRuntime:
    def test_agent_not_found(self, client, env, fake_controller):
        resp = client.post(
            f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/999999/runtime/spawn",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_forbidden_for_outsider(self, client, env, outsider, fake_controller):
        resp = client.post(_spawn_url(env), headers=outsider["headers"], json={})
        assert resp.status_code == 403

    def test_conflict_when_already_running(self, client, env, fake_controller):
        fake_controller.get_runtime_status.return_value = {
            "phase": "Running", "pod_name": "agent-1-xyz"}
        resp = client.post(_spawn_url(env), headers=env["headers"], json={})
        assert resp.status_code == 409
        assert resp.get_json()["error_details"]["existing"]["pod_name"] == "agent-1-xyz"
        fake_controller.spawn.assert_not_called()

    def test_key_decrypt_failure_returns_500(self, client, env, fake_controller):
        from models.agent_key import AgentKey
        row, _raw = AgentKey.generate_key(
            name="k", workspace_id=env["org"].id, agent_id=env["agent"].id,
            created_by_user_id=env["user"].id)
        db.session.add(row)
        db.session.commit()
        row.reveal = lambda: None  # 模拟密文不可解（identity map 保证路由内同实例）

        resp = client.post(_spawn_url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        fake_controller.spawn.assert_not_called()

    def test_reuses_existing_key_when_revealable(self, client, env, fake_controller):
        from models.agent_key import AgentKey
        row, raw = AgentKey.generate_key(
            name="k", workspace_id=env["org"].id, agent_id=env["agent"].id,
            created_by_user_id=env["user"].id)
        db.session.add(row)
        db.session.commit()

        resp = client.post(_spawn_url(env), headers=env["headers"], json={})
        assert resp.status_code == 200
        assert fake_controller.spawn.call_args.kwargs["agent_key"] == raw

    def test_success_generates_key_and_enables_runner(self, client, env, fake_controller):
        from models.agent_key import AgentKey

        resp = client.post(_spawn_url(env), headers=env["headers"], json={})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["data"]["pod"]["pod_name"] == "agent-1-abc"
        assert body["data"]["agent"]["id"] == env["agent"].id

        assert env["agent"].runner_enabled is True
        assert env["agent"].execution_mode == "managed_runner"
        key_row = AgentKey.query.filter_by(agent_id=env["agent"].id).first()
        assert key_row is not None and key_row.reveal()

    def test_spawn_uses_requested_sandbox_profile(self, client, env, fake_controller):
        client.post(_spawn_url(env), headers=env["headers"], json={"sandbox_profile": "minimal"})
        assert fake_controller.spawn.call_args.kwargs["sandbox_profile"] == "minimal"

    def test_controller_failure_returns_500(self, client, env, fake_controller):
        fake_controller.spawn.side_effect = RuntimeError("kube boom")
        resp = client.post(_spawn_url(env), headers=env["headers"], json={})
        assert resp.status_code == 500
        assert "kube boom" in resp.get_json()["message"]


class TestTerminateRuntime:
    def test_success_disables_runner(self, client, env, fake_controller):
        env["agent"].runner_enabled = True
        db.session.commit()
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/{env['agent'].id}/runtime/terminate"
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["terminated"] is True
        assert env["agent"].runner_enabled is False
        assert env["agent"].execution_mode == "external_pull"

    def test_no_running_runtime_returns_404(self, client, env, fake_controller):
        fake_controller.terminate.return_value = False
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/{env['agent'].id}/runtime/terminate"
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 404

    def test_agent_not_found(self, client, env, fake_controller):
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/999999/runtime/terminate"
        resp = client.post(url, headers=env["headers"])
        assert resp.status_code == 404


class TestStatusAndList:
    def test_status_reports_pod(self, client, env, fake_controller):
        fake_controller.get_runtime_status.return_value = {"phase": "Running"}
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/{env['agent'].id}/runtime/status"
        resp = client.get(url, headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["agent_id"] == env["agent"].id
        assert data["pod"] == {"phase": "Running"}

    def test_agent_not_found(self, client, env, fake_controller):
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/agents/999999/runtime/status"
        resp = client.get(url, headers=env["headers"])
        assert resp.status_code == 404

    def test_list_pods_empty(self, client, env, fake_controller):
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/runtime/pods"
        resp = client.get(url, headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"] == {"pods": [], "total": 0}

    def test_list_pods_forbidden_for_outsider(self, client, env, outsider, fake_controller):
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/runtime/pods"
        resp = client.get(url, headers=outsider["headers"])
        assert resp.status_code == 403


@pytest.fixture
def settings_controller(monkeypatch):
    """settings 读取链需要真实类属性默认值，用 SimpleNamespace 桩。"""
    from types import SimpleNamespace
    from services.cloud_runtime import management

    controller = SimpleNamespace(MAX_PODS_PER_WORKSPACE=5, POD_IDLE_TIMEOUT_MINUTES=10)
    monkeypatch.setattr(management, "get_runtime_provider", lambda: controller)
    return controller


def _settings_url(env):
    return f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/runtime/settings"


class TestWorkspaceRuntimeSettings:
    def test_defaults_when_unset(self, client, env, settings_controller):
        resp = client.get(_settings_url(env), headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["settings"] == {
            "max_pods": 5, "idle_timeout_minutes": 10, "max_concurrent_agents": 5}

    def test_workspace_not_found(self, client, env, settings_controller):
        resp = client.get(
            "/todo-for-ai/api/v1/workspaces/999999/runtime/settings",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_put_workspace_not_found(self, client, env, settings_controller):
        resp = client.put(
            "/todo-for-ai/api/v1/workspaces/999999/runtime/settings",
            headers=env["headers"], json={"max_pods": 3})
        assert resp.status_code == 404

    def test_forbidden_for_outsider(self, client, env, outsider, settings_controller):
        resp = client.get(_settings_url(env), headers=outsider["headers"])
        assert resp.status_code == 403

    def test_put_updates_both_fields(self, client, env, settings_controller):
        resp = client.put(_settings_url(env), headers=env["headers"],
                          json={"max_pods": 3, "idle_timeout_minutes": 30})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["settings"] == {
            "max_pods": 3, "idle_timeout_minutes": 30, "max_concurrent_agents": 5}
        from models.workspace_runtime_setting import WorkspaceRuntimeSetting
        row = WorkspaceRuntimeSetting.query.filter_by(workspace_id=env["org"].id).first()
        assert row.max_pods == 3 and row.idle_timeout_minutes == 30

    def test_put_partial_keeps_other_field(self, client, env, settings_controller):
        resp = client.put(_settings_url(env), headers=env["headers"], json={"max_pods": 7})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["settings"]["max_pods"] == 7
        assert resp.get_json()["data"]["settings"]["idle_timeout_minutes"] == 10

    def test_put_rejects_out_of_range_max_pods(self, client, env, settings_controller):
        resp = client.put(_settings_url(env), headers=env["headers"], json={"max_pods": 101})
        assert resp.status_code == 400

    def test_put_rejects_out_of_range_idle(self, client, env, settings_controller):
        resp = client.put(_settings_url(env), headers=env["headers"],
                          json={"idle_timeout_minutes": 20000})
        assert resp.status_code == 400

    def test_put_rejects_non_integer(self, client, env, settings_controller):
        resp = client.put(_settings_url(env), headers=env["headers"], json={"max_pods": "abc"})
        assert resp.status_code == 400

    def test_put_rejects_wrong_type(self, client, env, settings_controller):
        resp = client.put(_settings_url(env), headers=env["headers"],
                          json={"idle_timeout_minutes": [30]})
        assert resp.status_code == 400
