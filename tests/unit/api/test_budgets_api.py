"""Tests for P2.2 budget management API (CRUD + usage)."""

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

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


@pytest.fixture
def owner_auth(_isolated_app, db_session):
    import uuid as _uuid
    from models import User, Organization
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique_id = str(_uuid.uuid4())[:8]
    user = User(username=f"testuser_{unique_id}", email=f"test_{unique_id}@example.com")
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    org = Organization(name=f"org-{unique_id}", slug=f"org-{unique_id}", owner_id=user.id)
    db_session.add(org)
    db_session.commit()

    with _isolated_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "org": org, "headers": {"Authorization": f"Bearer {token}"}}


def _auth_headers(_isolated_app, user):
    from flask_jwt_extended import create_access_token
    with _isolated_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


class TestBudgetCrud:
    def test_create_and_list_workspace_budget(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "workspace", "resource": "tokens", "limit_value": 100000},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()["data"]
        assert body["scope_type"] == "workspace"
        assert body["resource"] == "tokens"
        assert body["period"] == "total"
        assert "usage" in body

        lst = client.get(f"{BASE_URL}/workspaces/{ws}/budgets", headers=owner_auth["headers"])
        assert lst.status_code == 200
        assert len(lst.get_json()["data"]["budgets"]) == 1

    def test_create_validates_enums(self, client, owner_auth):
        ws = owner_auth["org"].id
        for payload in (
            {"scope_type": "galaxy", "resource": "tokens", "limit_value": 1},
            {"scope_type": "workspace", "resource": "credits", "limit_value": 1},
            {"scope_type": "workspace", "resource": "tokens", "limit_value": 1, "period": "hourly"},
        ):
            resp = client.post(
                f"{BASE_URL}/workspaces/{ws}/budgets", json=payload,
                headers=owner_auth["headers"],
            )
            assert resp.status_code == 400, payload

    def test_create_rejects_non_positive_limit(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "workspace", "resource": "tokens", "limit_value": 0},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_agent_scope_requires_workspace_agent(self, client, db_session, owner_auth, agent_factory, organization_factory):
        ws = owner_auth["org"].id
        # 缺 agent_id
        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "agent", "resource": "concurrent", "limit_value": 2},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

        # 别的工作区的 agent
        foreign_org = organization_factory()
        foreign_agent = agent_factory(workspace_id=foreign_org.id)
        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "agent", "resource": "concurrent", "limit_value": 2,
                  "agent_id": foreign_agent.id},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

        # 本工作区 agent 合法
        agent = agent_factory(workspace_id=ws)
        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "agent", "resource": "concurrent", "limit_value": 2,
                  "agent_id": agent.id},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["agent_id"] == agent.id

    def test_duplicate_scope_resource_period_conflicts(self, client, owner_auth):
        ws = owner_auth["org"].id
        payload = {"scope_type": "workspace", "resource": "tokens", "limit_value": 100}
        first = client.post(f"{BASE_URL}/workspaces/{ws}/budgets", json=payload,
                            headers=owner_auth["headers"])
        assert first.status_code == 200
        dup = client.post(f"{BASE_URL}/workspaces/{ws}/budgets", json=payload,
                          headers=owner_auth["headers"])
        assert dup.status_code == 409

    def test_update_and_delete(self, client, owner_auth):
        ws = owner_auth["org"].id
        created = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "workspace", "resource": "duration_minutes", "limit_value": 60},
            headers=owner_auth["headers"],
        ).get_json()["data"]
        budget_id = created["id"]

        updated = client.put(
            f"{BASE_URL}/workspaces/{ws}/budgets/{budget_id}",
            json={"limit_value": 120, "is_active": False},
            headers=owner_auth["headers"],
        )
        assert updated.status_code == 200
        assert updated.get_json()["data"]["limit_value"] == 120
        assert updated.get_json()["data"]["is_active"] is False

        deleted = client.delete(
            f"{BASE_URL}/workspaces/{ws}/budgets/{budget_id}",
            headers=owner_auth["headers"],
        )
        assert deleted.status_code == 200
        lst = client.get(f"{BASE_URL}/workspaces/{ws}/budgets", headers=owner_auth["headers"])
        assert lst.get_json()["data"]["budgets"] == []

    def test_write_requires_workspace_admin(self, client, db_session, _isolated_app, owner_auth, user_factory):
        ws = owner_auth["org"].id
        outsider = user_factory()
        headers = _auth_headers(_isolated_app, outsider)

        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "workspace", "resource": "tokens", "limit_value": 100},
            headers=headers,
        )
        assert resp.status_code == 403

        deleted = client.delete(f"{BASE_URL}/workspaces/{ws}/budgets/1", headers=headers)
        assert deleted.status_code == 403

    def test_404_for_unknown_budget(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.put(
            f"{BASE_URL}/workspaces/{ws}/budgets/99999",
            json={"limit_value": 5},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404


class TestBudgetUsageEndpoint:
    def test_duration_usage_and_violating_flag(self, client, db_session, owner_auth, agent_factory):
        from models import AgentRun, AgentRunState

        ws = owner_auth["org"].id
        agent = agent_factory(workspace_id=ws)
        now = datetime.utcnow()
        db_session.add(AgentRun(
            workspace_id=ws, agent_id=agent.id,
            state=AgentRunState.QUEUED.value,
            started_at=now - timedelta(minutes=90), ended_at=now,
        ))
        db_session.commit()

        created = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "agent", "resource": "duration_minutes", "limit_value": 60,
                  "agent_id": agent.id},
            headers=owner_auth["headers"],
        ).get_json()["data"]
        assert created["usage"]["used"] >= 60
        assert created["usage_ratio"] is not None and created["usage_ratio"] >= 1

        usage = client.get(
            f"{BASE_URL}/workspaces/{ws}/budgets/{created['id']}/usage",
            headers=owner_auth["headers"],
        )
        assert usage.status_code == 200
        assert usage.get_json()["data"]["violating"] is True

        # agent_factory teardown 会删 Agent，先解除 agent_runs 的 FK 引用
        db_session.query(AgentRun).filter_by(agent_id=agent.id).delete()
        db_session.commit()

    def test_usage_endpoint_for_member_view(self, client, owner_auth):
        ws = owner_auth["org"].id
        created = client.post(
            f"{BASE_URL}/workspaces/{ws}/budgets",
            json={"scope_type": "workspace", "resource": "concurrent", "limit_value": 3},
            headers=owner_auth["headers"],
        ).get_json()["data"]
        usage = client.get(
            f"{BASE_URL}/workspaces/{ws}/budgets/{created['id']}/usage",
            headers=owner_auth["headers"],
        )
        assert usage.status_code == 200
        data = usage.get_json()["data"]
        assert data["usage"] == {"used": 0, "not_tracked": False}
        assert data["violating"] is False
