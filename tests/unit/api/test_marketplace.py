"""Tests for Phase 4 digital-employee marketplace (publish / list / install / audit)."""

import sys
import os

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


def _make_template(db_session, owner_auth, name="qa-engineer", published=False, **overrides):
    import uuid as _uuid
    from models import AgentRoleTemplate, AgentRoleTemplateStatus

    overrides.setdefault("category", "qa")
    overrides.setdefault("is_builtin", False)
    overrides.setdefault("workspace_id", owner_auth["org"].id)
    template = AgentRoleTemplate(
        created_by_user_id=owner_auth["user"].id,
        name=f"{name}-{str(_uuid.uuid4())[:6]}",
        display_name=f"数字员工 {name}",
        description="可安装的测试数字员工",
        capability_tags=["testing", "python"],
        system_prompt="You are a QA engineer.",
        published_to_marketplace=published,
        status=AgentRoleTemplateStatus.ACTIVE,
        **overrides,
    )
    db_session.add(template)
    db_session.commit()
    return template


class TestPublish:
    def test_publish_and_unpublish(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        template = _make_template(db_session, owner_auth)

        resp = client.post(
            f"{BASE_URL}/workspaces/{ws}/agent-role-templates/{template.id}/publish",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["published_to_marketplace"] is True

        down = client.post(
            f"{BASE_URL}/workspaces/{ws}/agent-role-templates/{template.id}/unpublish",
            headers=owner_auth["headers"],
        )
        assert down.status_code == 200
        assert down.get_json()["data"]["published_to_marketplace"] is False


class TestMarketListing:
    def test_builtin_and_published_listed_unpublished_not(self, client, db_session, owner_auth):
        published = _make_template(db_session, owner_auth, name="pub", published=True)
        hidden = _make_template(db_session, owner_auth, name="hidden", published=False)
        builtin = _make_template(db_session, owner_auth, name="builtin", published=False,
                                 is_builtin=True, workspace_id=None)
        resp = client.get(f"{BASE_URL}/marketplace/digital-employees",
                          headers=owner_auth["headers"])
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.get_json()["data"]["items"]}
        assert published.id in ids
        assert builtin.id in ids
        assert hidden.id not in ids

    def test_search_and_category_filters(self, client, db_session, owner_auth):
        qa = _make_template(db_session, owner_auth, name="qasearch", published=True)
        _make_template(db_session, owner_auth, name="devsearch", published=True, category="developer")

        by_search = client.get(
            f"{BASE_URL}/marketplace/digital-employees?search=qasearch",
            headers=owner_auth["headers"],
        )
        ids = {item["id"] for item in by_search.get_json()["data"]["items"]}
        assert qa.id in ids

        by_cat = client.get(
            f"{BASE_URL}/marketplace/digital-employees?category=developer",
            headers=owner_auth["headers"],
        )
        assert by_cat.status_code == 200
        items = by_cat.get_json()["data"]["items"]
        assert items and all(item["category"] == "developer" for item in items)


class TestInstall:
    def test_install_creates_workspace_copy_idempotently(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        template = _make_template(db_session, owner_auth, published=True)
        usage_before = template.usage_count or 0

        first = client.post(
            f"{BASE_URL}/marketplace/digital-employees/{template.id}/install",
            json={"workspace_id": ws},
            headers=owner_auth["headers"],
        )
        assert first.status_code == 200
        assert first.get_json()["data"]["created"] is True

        copy = first.get_json()["data"]["template"]
        assert copy["workspace_id"] == ws
        assert copy["parent_template_id"] == template.id
        assert copy["is_builtin"] is False

        # 幂等：重复安装返回既有副本
        second = client.post(
            f"{BASE_URL}/marketplace/digital-employees/{template.id}/install",
            json={"workspace_id": ws},
            headers=owner_auth["headers"],
        )
        assert second.status_code == 200
        assert second.get_json()["data"]["created"] is False
        assert second.get_json()["data"]["template"]["id"] == copy["id"]

        db_session.expire(template)
        assert template.usage_count == usage_before + 1

    def test_install_with_create_agent(self, client, db_session, owner_auth):
        from models import Agent

        ws = owner_auth["org"].id
        template = _make_template(db_session, owner_auth, published=True)
        agent_name = f"installed-agent-{template.id}"

        resp = client.post(
            f"{BASE_URL}/marketplace/digital-employees/{template.id}/install",
            json={"workspace_id": ws, "create_agent": True, "agent_name": agent_name},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        agent_payload = resp.get_json()["data"]["agent"]
        assert agent_payload["name"] == agent_name

        agent = db_session.get(Agent, agent_payload["id"])
        assert agent.workspace_id == ws
        assert agent.system_prompt == "You are a QA engineer."

    def test_install_unpublished_template_404(self, client, db_session, owner_auth):
        ws = owner_auth["org"].id
        hidden = _make_template(db_session, owner_auth, published=False)

        resp = client.post(
            f"{BASE_URL}/marketplace/digital-employees/{hidden.id}/install",
            json={"workspace_id": ws},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_install_requires_manage_access(self, client, db_session, _isolated_app, owner_auth, user_factory):
        import uuid as _uuid
        from flask_jwt_extended import create_access_token
        from models import User
        from werkzeug.security import generate_password_hash

        ws = owner_auth["org"].id
        template = _make_template(db_session, owner_auth, published=True)

        outsider = User(username=f"out_{str(_uuid.uuid4())[:6]}", email=f"out_{str(_uuid.uuid4())[:6]}@x.com")
        outsider.password_hash = generate_password_hash("password123")
        db_session.add(outsider)
        db_session.commit()
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.post(
            f"{BASE_URL}/marketplace/digital-employees/{template.id}/install",
            json={"workspace_id": ws},
            headers=headers,
        )
        assert resp.status_code == 403


class TestInstallAudit:
    def test_install_and_publish_audited(self, client, db_session, owner_auth):
        from models import AgentAuditEvent

        ws = owner_auth["org"].id
        template = _make_template(db_session, owner_auth, published=True)

        client.post(
            f"{BASE_URL}/marketplace/digital-employees/{template.id}/install",
            json={"workspace_id": ws},
            headers=owner_auth["headers"],
        )

        events = AgentAuditEvent.query.filter_by(
            workspace_id=ws, event_type="marketplace.employee_installed",
        ).all()
        assert len(events) == 1
        assert events[0].payload.get("source_template_id") == template.id
