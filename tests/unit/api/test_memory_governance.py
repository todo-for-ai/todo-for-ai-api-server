"""Tests for P3.4 memory governance (versioning, audit query, forget)."""

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


@pytest.fixture
def agent(db_session, owner_auth):
    import uuid as _uuid
    from models import Agent, AgentStatus

    agent = Agent(
        name=f"memory-agent-{str(_uuid.uuid4())[:6]}",
        display_name="Memory Agent",
        workspace_id=owner_auth["org"].id,
        creator_user_id=owner_auth["user"].id,
        status=AgentStatus.ACTIVE,
        capabilities=["python"],
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _rebuild(client, agent_id, headers):
    return client.post(
        f"{BASE_URL}/agents/{agent_id}/skill-profile/rebuild",
        headers=headers,
    )


class TestMemoryVersioning:
    def test_rebuild_writes_versioned_snapshot(self, client, db_session, owner_auth, agent):
        from models import AgentSoulVersion
        from models.agent_soul_version import MEMORY_KIND_SKILL_PROFILE

        first = _rebuild(client, agent.id, owner_auth["headers"])
        assert first.status_code == 200, first.get_json()
        second = _rebuild(client, agent.id, owner_auth["headers"])
        assert second.status_code == 200

        rows = AgentSoulVersion.query.filter_by(
            agent_id=agent.id, memory_kind=MEMORY_KIND_SKILL_PROFILE,
        ).order_by(AgentSoulVersion.version.asc()).all()
        assert [row.version for row in rows] == [1, 2]
        snapshot = rows[0].snapshot_as_dict()
        assert snapshot is not None and "skills" in snapshot
        assert rows[0].edited_by_user_id == owner_auth["user"].id

    def test_soul_and_profile_versions_coexist(self, client, db_session, owner_auth, agent):
        """SOUL v1 与技能画像 v1 同 Agent 共存（放宽后的唯一约束）。"""
        from models import AgentSoulVersion

        db_session.add(AgentSoulVersion(
            agent_id=agent.id, workspace_id=owner_auth["org"].id,
            version=1, memory_kind="soul",
            soul_markdown="# Soul v1",
            change_summary="seed", edited_by_user_id=owner_auth["user"].id,
            created_by="test",
        ))
        db_session.commit()

        rebuilt = _rebuild(client, agent.id, owner_auth["headers"])
        assert rebuilt.status_code == 200

        rows = AgentSoulVersion.query.filter_by(agent_id=agent.id).all()
        kinds = {row.memory_kind for row in rows}
        assert kinds == {"soul", "skill_profile"}

    def test_memory_versions_endpoint_with_kind_filter(self, client, db_session, owner_auth, agent):
        from models import AgentSoulVersion

        db_session.add(AgentSoulVersion(
            agent_id=agent.id, workspace_id=owner_auth["org"].id,
            version=1, memory_kind="soul", soul_markdown="# Soul",
            edited_by_user_id=owner_auth["user"].id, created_by="test",
        ))
        db_session.commit()
        assert _rebuild(client, agent.id, owner_auth["headers"]).status_code == 200

        all_resp = client.get(
            f"{BASE_URL}/agents/{agent.id}/memory/versions",
            headers=owner_auth["headers"],
        )
        assert all_resp.status_code == 200
        assert all_resp.get_json()["data"]["pagination"]["total"] == 2

        profile_only = client.get(
            f"{BASE_URL}/agents/{agent.id}/memory/versions?kind=skill_profile",
            headers=owner_auth["headers"],
        )
        assert profile_only.status_code == 200
        items = profile_only.get_json()["data"]["items"]
        assert len(items) == 1 and items[0]["memory_kind"] == "skill_profile"
        assert items[0]["snapshot"] is not None

        bad_kind = client.get(
            f"{BASE_URL}/agents/{agent.id}/memory/versions?kind=nope",
            headers=owner_auth["headers"],
        )
        assert bad_kind.status_code == 400


class TestMemoryAudit:
    def test_audit_endpoint_lists_memory_events(self, client, owner_auth, agent):
        assert _rebuild(client, agent.id, owner_auth["headers"]).status_code == 200
        forgotten = client.delete(
            f"{BASE_URL}/agents/{agent.id}/skill-profile",
            headers=owner_auth["headers"],
        )
        assert forgotten.status_code == 200

        audit = client.get(
            f"{BASE_URL}/agents/{agent.id}/memory/audit",
            headers=owner_auth["headers"],
        )
        assert audit.status_code == 200
        event_types = {item["event_type"] for item in audit.get_json()["data"]["items"]}
        assert "agent.skill_profile.rebuilt" in event_types
        assert "agent.skill_profile.forgotten" in event_types

    def test_audit_endpoint_filter_by_event_type(self, client, owner_auth, agent):
        _rebuild(client, agent.id, owner_auth["headers"])
        audit = client.get(
            f"{BASE_URL}/agents/{agent.id}/memory/audit?event_type=agent.skill_profile.rebuilt",
            headers=owner_auth["headers"],
        )
        assert audit.status_code == 200
        items = audit.get_json()["data"]["items"]
        assert items and all(i["event_type"] == "agent.skill_profile.rebuilt" for i in items)


class TestForgetProfile:
    def test_forget_clears_profile_and_writes_tombstone(self, client, db_session, owner_auth, agent):
        from models import Agent, AgentSoulVersion
        from models.agent_soul_version import MEMORY_KIND_SKILL_PROFILE

        assert _rebuild(client, agent.id, owner_auth["headers"]).status_code == 200

        forgotten = client.delete(
            f"{BASE_URL}/agents/{agent.id}/skill-profile",
            headers=owner_auth["headers"],
        )
        assert forgotten.status_code == 200
        assert forgotten.get_json()["data"]["profile"]["forgotten"] is True

        db_session.expire(agent)
        assert agent.skill_profile is None
        assert agent.skill_profile_updated_at is None

        tombstone = (
            AgentSoulVersion.query
            .filter_by(agent_id=agent.id, memory_kind=MEMORY_KIND_SKILL_PROFILE)
            .order_by(AgentSoulVersion.version.desc())
            .first()
        )
        assert tombstone is not None
        assert "forgotten" in (tombstone.change_summary or "")

    def test_forget_requires_manage_access(self, client, db_session, _isolated_app, owner_auth, agent, user_factory):
        outsider = user_factory()
        from flask_jwt_extended import create_access_token
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.delete(
            f"{BASE_URL}/agents/{agent.id}/skill-profile",
            headers=headers,
        )
        assert resp.status_code == 403

    def test_404_for_unknown_agent(self, client, owner_auth):
        resp = client.get(
            f"{BASE_URL}/agents/999999/memory/versions",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404
