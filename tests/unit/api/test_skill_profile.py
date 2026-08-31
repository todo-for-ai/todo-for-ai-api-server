"""Tests for P3.1 skill profile (aggregation, API, dispatch scoring bonus)."""

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
def agent(db_session, owner_auth, organization_factory):
    from models import Agent, AgentStatus

    agent = Agent(
        name=f"skill-agent-{str(_uuid_mod())[:6]}",
        display_name="Skill Agent",
        workspace_id=owner_auth["org"].id,
        creator_user_id=owner_auth["user"].id,
        status=AgentStatus.ACTIVE,
        capabilities=["python"],
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _uuid_mod():
    import uuid
    return uuid.uuid4()


class TestBuildSkillProfile:
    def test_aggregates_experiences_and_assignments(self, db_session, owner_auth, agent, task_factory):
        from datetime import datetime, timedelta

        from models import AgentExperience, TaskAssignment, TaskAssignmentState
        from services.skill_profile import rebuild_skill_profile

        now = datetime.utcnow()
        for exp_type in ("success_pattern", "strategy", "failure_pattern"):
            db_session.add(AgentExperience(
                agent_id=agent.id,
                experience_type=exp_type,
                domain="python",
                task_type="bug_fix",
                capabilities_used=["python", "flask"],
                confidence=0.8,
            ))
        task_done = task_factory(owner_id=owner_auth["user"].id)
        task_failed = task_factory(owner_id=owner_auth["user"].id)
        db_session.add(TaskAssignment(
            task_id=task_done.id, agent_id=agent.id,
            state=TaskAssignmentState.DONE, completed_at=now,
        ))
        db_session.add(TaskAssignment(
            task_id=task_failed.id, agent_id=agent.id,
            state=TaskAssignmentState.FAILED,
        ))
        db_session.commit()

        profile = rebuild_skill_profile(agent.id)

        skills = {(s["name"], s["kind"]): s for s in profile["skills"]}
        python_domain = skills[("python", "domain")]
        assert python_domain["count"] == 3
        assert python_domain["success_rate"] == round(2 / 3, 3)

        flask_cap = skills[("flask", "capability")]
        assert flask_cap["count"] == 3

        assert profile["assignments"] == {"completed": 1, "failed": 1}
        assert profile["experience_count"] == 3

        db_session.expire(agent)
        assert agent.skill_profile is not None
        assert agent.skill_profile_updated_at is not None

        # 清理 assignment 引用，避免 factory teardown FK 冲突
        db_session.query(TaskAssignment).filter_by(agent_id=agent.id).delete()
        db_session.commit()

    def test_invalid_experience_ignored(self, db_session, owner_auth, agent):
        from models import AgentExperience
        from services.skill_profile import build_skill_profile

        db_session.add(AgentExperience(
            agent_id=agent.id, experience_type="success_pattern",
            domain="rust", is_valid=False,
        ))
        db_session.commit()

        profile = build_skill_profile(agent)
        assert profile["skills"] == []


class TestSkillProfileAPI:
    def test_get_and_rebuild(self, client, db_session, owner_auth, agent):
        from models import AgentExperience

        db_session.add(AgentExperience(
            agent_id=agent.id, experience_type="success_pattern", domain="devops",
        ))
        db_session.commit()

        resp = client.get(
            f"{BASE_URL}/agents/{agent.id}/skill-profile",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["profile"] is None  # 尚未重建

        rebuilt = client.post(
            f"{BASE_URL}/agents/{agent.id}/skill-profile/rebuild",
            headers=owner_auth["headers"],
        )
        assert rebuilt.status_code == 200
        names = [s["name"] for s in rebuilt.get_json()["data"]["profile"]["skills"]]
        assert "devops" in names

        after = client.get(
            f"{BASE_URL}/agents/{agent.id}/skill-profile",
            headers=owner_auth["headers"],
        )
        data = after.get_json()["data"]
        assert data["profile"]["skills"]
        assert data["stale"] is False

    def test_rebuild_requires_manage_access(self, client, db_session, _isolated_app, owner_auth, agent, user_factory):
        outsider = user_factory()
        from flask_jwt_extended import create_access_token
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.post(
            f"{BASE_URL}/agents/{agent.id}/skill-profile/rebuild",
            headers=headers,
        )
        assert resp.status_code == 403

    def test_404_for_unknown_agent(self, client, owner_auth):
        resp = client.get(
            f"{BASE_URL}/agents/999999/skill-profile",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404


class TestDispatchScoring:
    def test_profile_bonus_applied(self, db_session, owner_auth, agent, task_factory):
        from models import Task
        from api.agents._shared import score_task_for_agent

        agent.skill_profile = {
            "skills": [{"name": "python", "kind": "capability", "count": 5, "success_rate": 0.9}],
            "assignments": {"completed": 5, "failed": 0},
            "experience_count": 5,
            "generated_at": "2026-08-31T00:00:00Z",
        }
        task = task_factory(owner_id=owner_auth["user"].id, title="Python data pipeline")
        db_session.expire(task)

        result = score_task_for_agent(task, agent)
        assert result["skill_profile_bonus"] > 0

    def test_no_profile_no_bonus(self, db_session, owner_auth, agent, task_factory):
        from api.agents._shared import score_task_for_agent

        task = task_factory(owner_id=owner_auth["user"].id, title="Python data pipeline")
        db_session.expire(task)

        result = score_task_for_agent(task, agent)
        assert result.get("skill_profile_bonus", 0) == 0
