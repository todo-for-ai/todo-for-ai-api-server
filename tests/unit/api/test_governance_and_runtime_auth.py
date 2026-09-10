"""迭代 50：小模块收口——治理规则（agent_governance_rules）与 Agent Runtime 鉴权
（agent_runtime_auth）100% 行覆盖回归。

治理规则：GET/PUT /workspaces/<id>/governance/rules（owner 可读写、成员/陌生人
403、rules 必须为数组、落库经 SystemSettings、写审计事件）。
Runtime 鉴权：POST /agent/auth/introspect（缺 agent_key 400、无效 key 401、
非活跃 Agent 401、成功签发 15 分钟会话令牌并带 Agent 信息）。
"""

import uuid

import pytest

from app import create_app
from models import (
    db,
    Agent,
    AgentKey,
    AgentStatus,
    Organization,
    User,
)


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
        status=AgentStatus.ACTIVE,
    )
    db.session.add(agent)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {
        "user": user, "org": org, "agent": agent,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
def stranger(env):
    from flask_jwt_extended import create_access_token
    from models import User

    user = User(username=f"s_{uuid.uuid4().hex[:8]}", email=f"s_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.commit()
    return {"user": user, "headers": {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}}


# ── 治理规则 ─────────────────────────────────────────────────────────


class TestGovernanceRules:
    def test_get_unknown_workspace_404(self, client, env):
        resp = client.get("/todo-for-ai/api/v1/workspaces/987654/governance/rules",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_get_default_empty_rules(self, client, env):
        resp = client.get(f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/governance/rules",
                          headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["rules"] == []

    def test_get_stranger_403(self, client, env, stranger):
        resp = client.get(f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/governance/rules",
                          headers=stranger["headers"])
        assert resp.status_code == 403

    def test_put_unknown_workspace_404(self, client, env):
        resp = client.put("/todo-for-ai/api/v1/workspaces/987654/governance/rules",
                          json={"rules": []}, headers=env["headers"])
        assert resp.status_code == 404

    def test_put_stranger_403(self, client, env, stranger):
        resp = client.put(f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/governance/rules",
                          json={"rules": [{"type": "x"}]}, headers=stranger["headers"])
        assert resp.status_code == 403

    def test_put_missing_rules_400(self, client, env):
        resp = client.put(f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/governance/rules",
                          json={}, headers=env["headers"])
        assert resp.status_code in (400, 422)

    def test_put_non_array_rules_400(self, client, env):
        resp = client.put(f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/governance/rules",
                          json={"rules": {"type": "x"}}, headers=env["headers"])
        assert resp.status_code == 400
        assert "array" in resp.get_json()["message"]

    def test_put_and_get_roundtrip(self, client, env):
        url = f"/todo-for-ai/api/v1/workspaces/{env['org'].id}/governance/rules"
        rules = [{"type": "max_daily_spend", "value": 100},
                 {"type": "require_review", "value": True}]
        resp = client.put(url, json={"rules": rules}, headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["rules"] == rules

        from models import AgentAuditEvent
        audit = AgentAuditEvent.query.filter_by(
            event_type="governance.rules_updated").order_by(AgentAuditEvent.id.desc()).first()
        assert audit is not None
        assert audit.payload.get("rule_count") == 2

        resp = client.get(url, headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["rules"] == rules


# ── Agent Runtime 鉴权 ───────────────────────────────────────────────


class TestAgentAuthIntrospect:
    URL = "/todo-for-ai/api/v1/agent/auth/introspect"

    def test_missing_agent_key_400(self, client):
        resp = client.post(self.URL, json={})
        assert resp.status_code in (400, 422)

    def test_invalid_key_401(self, client):
        resp = client.post(self.URL, json={"agent_key": "agk_bogus"})
        assert resp.status_code == 401
        assert "Invalid agent key" in resp.get_json()["message"]

    def test_success_returns_session_token(self, client, env):
        row, raw = AgentKey.generate_key(
            name="runtime key", workspace_id=env["org"].id,
            agent_id=env["agent"].id, created_by_user_id=env["user"].id)
        db.session.add(row)
        db.session.commit()

        resp = client.post(self.URL, json={"agent_key": raw})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["access_token"].startswith("ags_")
        assert data["expires_in"] == 900
        assert data["agent"]["id"] == env["agent"].id
        assert data["agent"]["workspace_id"] == env["org"].id
        assert data["agent"]["working_schedule"] == {}
        # verify_key 侧效果：usage_count 自增
        assert row.usage_count == 1

    def test_inactive_agent_401(self, client, env):
        from models import AgentStatus
        row, raw = AgentKey.generate_key(
            name="inactive key", workspace_id=env["org"].id,
            agent_id=env["agent"].id, created_by_user_id=env["user"].id)
        db.session.add(row)
        env["agent"].status = AgentStatus.DISABLED
        db.session.commit()

        resp = client.post(self.URL, json={"agent_key": raw})
        assert resp.status_code == 401
        assert "inactive" in resp.get_json()["message"]
