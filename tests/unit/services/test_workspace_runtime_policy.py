"""工作区运行时配额与空闲回收（云端 Phase 2）回归测试。"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from models import db, AgentTaskAttempt


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


class FakeProvider:
    """不需要任何真实后端的 provider 替身（归一化状态契约）。"""

    name = "fake"
    MAX_PODS_PER_WORKSPACE = 10
    POD_IDLE_TIMEOUT_MINUTES = 30

    def __init__(self, runtimes=None):
        self.runtimes = runtimes or []
        self.terminated = []

    def list_runtimes(self, workspace_id=None):
        return self.runtimes

    def terminate(self, agent_id):
        self.terminated.append(agent_id)
        return True


def _runtime(agent_id=11, workspace_id=22, phase="Running"):
    return {"agent_id": agent_id, "workspace_id": workspace_id,
            "phase": phase, "started_at": None}


def _attempt(agent_id, started_hours_ago, ended_hours_ago=None, state="ACTIVE"):
    now = datetime.utcnow()
    return AgentTaskAttempt(
        attempt_id=f"att_{uuid.uuid4().hex[:8]}",
        task_id=1,
        agent_id=agent_id,
        workspace_id=22,
        state=state,
        lease_id=f"lea_{uuid.uuid4().hex[:8]}",
        started_at=now - timedelta(hours=started_hours_ago),
        ended_at=now - timedelta(hours=ended_hours_ago) if ended_hours_ago is not None else None,
    )


def test_settings_default_without_row():
    from services.workspace_runtime_policy import get_workspace_runtime_setting
    setting = get_workspace_runtime_setting(FakeProvider(), 22)
    assert setting == {"max_pods": 10, "idle_timeout_minutes": 30,
                      "max_concurrent_agents": 5}


def test_set_then_get_settings():
    from services.workspace_runtime_policy import (
        get_workspace_runtime_setting,
        set_workspace_runtime_setting,
    )
    set_workspace_runtime_setting(22, max_pods=3, idle_timeout_minutes=15)
    setting = get_workspace_runtime_setting(FakeProvider(), 22)
    assert setting == {"max_pods": 3, "idle_timeout_minutes": 15,
                      "max_concurrent_agents": 5}
    # 未设置的工作区不受影响
    assert get_workspace_runtime_setting(FakeProvider(), 99)["max_pods"] == 10


def test_recycle_removes_idle_pod_and_skips_active():
    from services.workspace_runtime_policy import recycle_idle_pods

    provider = FakeProvider(runtimes=[_runtime(agent_id=11), _runtime(agent_id=12)])
    # agent 11：2 小时前的已结束 attempt（空闲）；agent 12：ACTIVE attempt（在干活）
    db.session.add(_attempt(11, started_hours_ago=3, ended_hours_ago=2, state="COMMITTED"))
    db.session.add(_attempt(12, started_hours_ago=0.1))
    db.session.commit()

    result = recycle_idle_pods(provider)
    assert result["checked"] == 2
    assert result["recycled"] == 1
    assert result["skipped_active"] == 1
    assert provider.terminated == [11]


def test_recycle_zero_threshold_disables():
    from services.workspace_runtime_policy import (
        recycle_idle_pods,
        set_workspace_runtime_setting,
    )
    set_workspace_runtime_setting(22, idle_timeout_minutes=0)
    provider = FakeProvider(runtimes=[_runtime(agent_id=11)])
    result = recycle_idle_pods(provider)
    assert result["skipped_recent"] == 0 and result["recycled"] == 0
    assert provider.terminated == []


def test_recycle_uses_workspace_threshold_override():
    from services.workspace_runtime_policy import (
        recycle_idle_pods,
        set_workspace_runtime_setting,
    )
    # 默认阈值 30 分钟；该工作区放宽到 600 分钟 → 2 小时空闲不回收
    set_workspace_runtime_setting(22, idle_timeout_minutes=600)
    provider = FakeProvider(runtimes=[_runtime(agent_id=11)])
    db.session.add(_attempt(11, started_hours_ago=3, ended_hours_ago=2, state="COMMITTED"))
    db.session.commit()

    result = recycle_idle_pods(provider)
    assert result["skipped_recent"] == 1
    assert provider.terminated == []


def test_quota_settings_api_roundtrip(client):
    """PUT 后 GET 生效（工作区 owner 视角）。"""
    from flask_jwt_extended import create_access_token
    from models import Organization, Project, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}",
                       owner_id=user.id)
    db.session.add(org)
    db.session.commit()

    headers = {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}
    base = "/todo-for-ai/api/v1"

    resp = client.put(
        f"{base}/workspaces/{org.id}/runtime/settings",
        json={"max_pods": 5, "idle_timeout_minutes": 45},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["settings"] == {
        "max_pods": 5, "idle_timeout_minutes": 45, "max_concurrent_agents": 5}

    resp = client.get(f"{base}/workspaces/{org.id}/runtime/settings", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["settings"]["idle_timeout_minutes"] == 45

    # 越界拒绝
    resp = client.put(
        f"{base}/workspaces/{org.id}/runtime/settings",
        json={"max_pods": 999},
        headers=headers,
    )
    assert resp.status_code == 400

    # 旁观者 403
    outsider = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(outsider)
    db.session.commit()
    resp = client.put(
        f"{base}/workspaces/{org.id}/runtime/settings",
        json={"max_pods": 1},
        headers={"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"},
    )
    assert resp.status_code == 403


def test_ensure_agent_pod_uses_workspace_quota_override(_isolated_app):
    """ensure_agent_pod 的上限从工作区设置读取（覆盖类默认 10）。"""
    from unittest.mock import MagicMock, patch
    from types import SimpleNamespace

    from services.agent_runtime_controller import AgentRuntimeController
    from services.workspace_runtime_policy import set_workspace_runtime_setting

    with patch.object(AgentRuntimeController, "_init_k8s_client", return_value=None):
        controller = AgentRuntimeController()
    controller.core_v1 = MagicMock()
    controller.get_agent_pod_status = MagicMock(return_value=None)
    controller._list_workspace_pods = MagicMock(return_value=[
        SimpleNamespace(status=SimpleNamespace(phase="Running")),
        SimpleNamespace(status=SimpleNamespace(phase="Running")),
    ])

    from models import Agent
    agent = Agent()
    agent.id = 77
    agent.workspace_id = 22
    agent.sandbox_profile = "standard"
    agent.sandbox_policy = {}

    # 工作区上限 2 → 已有 2 个在岗 → 拒绝创建
    set_workspace_runtime_setting(22, max_pods=2)
    result = controller.ensure_agent_pod(agent, "agk_x")
    assert result["status"] == "workspace_pod_limit"
    assert result["cap"] == 2
