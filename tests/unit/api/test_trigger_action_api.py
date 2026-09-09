"""触发器动作字段（action/action_payload）API 回归。

覆盖：cron 触发器 run_agent 默认值、create_task 合法 payload 落库、
payload 校验分支（缺 title/未知项目/非法优先级/run_agent 带 payload/
未知 action）、Patch 切换动作与 payload 清理、task_event 触发器拒绝
create_task 之外的口径一致性。
"""

import uuid

import pytest
from flask_jwt_extended import create_access_token

from models import (
    Agent,
    AgentStatus,
    Organization,
    Project,
    User,
    db,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
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
    user = User(username=f"ta_{uuid.uuid4().hex[:8]}", email=f"ta_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:6]}", slug=f"o_{uuid.uuid4().hex[:6]}",
                       owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    agent = Agent(workspace_id=org.id, owner_id=user.id, creator_user_id=user.id,
                  name=f"ag_{uuid.uuid4().hex[:6]}", status=AgentStatus.ACTIVE)
    db.session.add(agent)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "agent": agent, "project": project,
        "headers": {"Authorization": f"Bearer {token}"},
        "base": "/todo-for-ai/api/v1",
    }


def _url(env):
    return (f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/{env['agent'].id}/triggers")


def _cron_payload(**kw):
    payload = {"name": f"t_{uuid.uuid4().hex[:6]}",
               "trigger_type": "cron",
               "cron_expr": "*/15 * * * *"}
    payload.update(kw)
    return payload


class TestTriggerActionCreate:
    def test_default_action_is_run_agent(self, env, client):
        resp = client.post(_url(env), headers=env["headers"], json=_cron_payload())
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["action"] == "run_agent"
        assert data["action_payload"] == {}

    def test_create_task_action_persists_payload(self, env, client):
        payload = _cron_payload(
            action="create_task",
            action_payload={"project_id": env["project"].id, "title": "周报汇总",
                            "priority": "high", "tags": ["weekly"]})
        resp = client.post(_url(env), headers=env["headers"], json=payload)
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["action"] == "create_task"
        assert data["action_payload"]["title"] == "周报汇总"
        assert data["action_payload"]["project_id"] == env["project"].id

    def test_create_task_requires_title(self, env, client):
        payload = _cron_payload(
            action="create_task",
            action_payload={"project_id": env["project"].id})
        resp = client.post(_url(env), headers=env["headers"], json=payload)
        assert resp.status_code == 400

    def test_create_task_requires_payload_object(self, env, client):
        resp = client.post(_url(env), headers=env["headers"],
                           json=_cron_payload(action="create_task"))
        assert resp.status_code == 400

    def test_create_task_project_must_be_in_workspace(self, env, client):
        outsider = User(username=f"ta_{uuid.uuid4().hex[:8]}", email=f"ta_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(outsider)
        db.session.flush()
        foreign_project = Project(name=f"p_{uuid.uuid4().hex[:6]}",
                                  owner_id=outsider.id, organization_id=None)
        db.session.add(foreign_project)
        db.session.commit()
        payload = _cron_payload(
            action="create_task",
            action_payload={"project_id": foreign_project.id, "title": "越权任务"})
        resp = client.post(_url(env), headers=env["headers"], json=payload)
        assert resp.status_code == 400

    def test_create_task_invalid_priority(self, env, client):
        payload = _cron_payload(
            action="create_task",
            action_payload={"project_id": env["project"].id, "title": "T",
                            "priority": "whenever"})
        resp = client.post(_url(env), headers=env["headers"], json=payload)
        assert resp.status_code == 400

    def test_run_agent_rejects_payload(self, env, client):
        payload = _cron_payload(action="run_agent", action_payload={"title": "x"})
        resp = client.post(_url(env), headers=env["headers"], json=payload)
        assert resp.status_code == 400

    def test_unknown_action_rejected(self, env, client):
        resp = client.post(_url(env), headers=env["headers"],
                           json=_cron_payload(action="explode"))
        assert resp.status_code == 400


class TestTriggerActionPatch:
    def _create(self, env, client, **kw):
        resp = client.post(_url(env), headers=env["headers"], json=_cron_payload(**kw))
        assert resp.status_code == 201
        return resp.get_json()["data"]["id"]

    def test_switch_to_create_task_then_back_clears_payload(self, env, client):
        trigger_id = self._create(env, client)

        resp = client.patch(f"{_url(env)}/{trigger_id}", headers=env["headers"],
                            json={"action": "create_task",
                                  "action_payload": {"project_id": env["project"].id,
                                                     "title": "巡检报告"}})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["action"] == "create_task"
        assert data["action_payload"]["title"] == "巡检报告"

        resp = client.patch(f"{_url(env)}/{trigger_id}", headers=env["headers"],
                            json={"action": "run_agent"})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["action"] == "run_agent"
        assert data["action_payload"] == {}

    def test_patch_invalid_project_rejected(self, env, client):
        trigger_id = self._create(env, client)
        resp = client.patch(f"{_url(env)}/{trigger_id}", headers=env["headers"],
                            json={"action": "create_task",
                                  "action_payload": {"project_id": 987654321,
                                                     "title": "越权"}})
        assert resp.status_code == 400
