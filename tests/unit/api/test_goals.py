"""Tests for P2.1 goal layer (Goal/Epic CRUD, agent proposals, batch decisions, expand)."""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    """每测试独立内存库（goal layer 跨表级联较多）。"""
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
def goal(owner_auth, db_session):
    from models import Goal, GoalStatus

    goal = Goal(
        workspace_id=owner_auth["org"].id,
        title="Grow activation",
        metrics=["activation rate > 40%"],
        status=GoalStatus.ACTIVE,
        owner_id=owner_auth["user"].id,
    )
    db_session.add(goal)
    db_session.commit()
    return goal


class TestGoalCrud:
    def test_create_and_get_goal(self, client, owner_auth):
        resp = client.post(f"{BASE_URL}/goals", json={
            "workspace_id": owner_auth["org"].id,
            "title": "Reduce churn",
            "metrics": ["churn < 2%"],
        }, headers=owner_auth["headers"])
        assert resp.status_code == 200, resp.get_json()
        goal_id = resp.get_json()["data"]["id"]

        resp = client.get(f"{BASE_URL}/goals/{goal_id}", headers=owner_auth["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["title"] == "Reduce churn"

    def test_list_goals_by_workspace(self, client, owner_auth, goal):
        resp = client.get(
            f"{BASE_URL}/goals?workspace_id={owner_auth['org'].id}",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["pagination"]["total"] >= 1


class TestEpicProposals:
    def test_human_creates_epic_accepted_by_default(self, client, owner_auth, goal):
        resp = client.post(
            f"{BASE_URL}/goals/{goal.id}/epics",
            json={"title": "Onboarding revamp", "description": "simplify first-run"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["status"] == "accepted"
        assert resp.get_json()["data"]["agent_proposed"] is False

    def test_agent_proposed_epic_starts_pending(self, client, db_session, owner_auth, goal, agent_factory):
        agent = agent_factory(workspace_id=owner_auth["org"].id)
        resp = client.post(
            f"{BASE_URL}/goals/{goal.id}/epics/propose",
            json={"title": "Self-serve import", "description": "CSV import wizard", "agent_id": agent.id},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["status"] == "proposed"
        assert data["agent_proposed"] is True

    def test_single_decision_accepts_proposal(self, client, db_session, owner_auth, goal, agent_factory):
        from models import Epic

        agent = agent_factory(workspace_id=owner_auth["org"].id)
        client.post(
            f"{BASE_URL}/goals/{goal.id}/epics/propose",
            json={"title": "Epic A", "agent_id": agent.id},
            headers=owner_auth["headers"],
        )
        epic = Epic.query.filter_by(goal_id=goal.id).first()

        resp = client.post(
            f"{BASE_URL}/epics/{epic.id}/decide",
            json={"decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["status"] == "accepted"

    def test_batch_decision(self, client, db_session, owner_auth, goal, agent_factory):
        from models import Epic

        agent = agent_factory(workspace_id=owner_auth["org"].id)
        for title in ("Epic 1", "Epic 2", "Epic 3"):
            client.post(
                f"{BASE_URL}/goals/{goal.id}/epics/propose",
                json={"title": title, "agent_id": agent.id},
                headers=owner_auth["headers"],
            )

        epic_ids = [e.id for e in Epic.query.filter_by(goal_id=goal.id).all()]
        # 批量批准前两个，拒绝第三个
        resp = client.post(
            f"{BASE_URL}/goals/{goal.id}/epics/decide-batch",
            json={"decision": "approved", "epic_ids": epic_ids[:2]},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["count"] == 2

        resp = client.post(
            f"{BASE_URL}/goals/{goal.id}/epics/decide-batch",
            json={"decision": "rejected"},
            headers=owner_auth["headers"],
        )
        assert resp.get_json()["data"]["count"] == 1

        statuses = {e.title: e.status.value for e in Epic.query.filter_by(goal_id=goal.id).all()}
        assert statuses == {"Epic 1": "accepted", "Epic 2": "accepted", "Epic 3": "dropped"}


class TestEpicExpansion:
    def test_expand_creates_task_graph_with_dod(self, client, db_session, owner_auth, goal, agent_factory, project_factory):
        from models import Task, Epic

        project = project_factory(owner_id=owner_auth["user"].id, organization_id=owner_auth["org"].id)

        agent = agent_factory(workspace_id=owner_auth["org"].id)
        client.post(
            f"{BASE_URL}/goals/{goal.id}/epics/propose",
            json={"title": "Epic to expand", "agent_id": agent.id},
            headers=owner_auth["headers"],
        )
        epic = Epic.query.filter_by(goal_id=goal.id).first()
        client.post(
            f"{BASE_URL}/epics/{epic.id}/decide",
            json={"decision": "approved"},
            headers=owner_auth["headers"],
        )

        import json as _json

        def json_dumps(obj):
            return _json.dumps(obj)

        llm_response = MagicMock()
        llm_response.content = json_dumps({
            "tasks": [
                {"title": "Schema", "description": "db schema", "priority": "high",
                 "dod": [{"type": "test", "value": "pytest tests/db"}], "depends_on": []},
                {"title": "API", "description": "api layer", "priority": "medium",
                 "dod": [{"type": "test", "value": "pytest tests/api"},
                         {"type": "lint", "value": "ruff check ."}],
                 "depends_on": [1]},
            ],
            "execution_order": "顺序",
        })

        def json_dumps(obj):
            import json as _json
            return _json.dumps(obj)

        with patch("services.ai_service.call_llm_production", return_value=llm_response):
            resp = client.post(
                f"{BASE_URL}/epics/{epic.id}/expand",
                json={"workspace_context": "test", "project_id": project.id},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert len(data["tasks"]) == 2
        assert len(data["tasks"]) == 2

        tasks = Task.query.filter(Task.epic_id == epic.id).all()
        assert len(tasks) == 2
        api_task = next(t for t in tasks if t.title == "API")
        assert api_task.blocked_by_task_ids
        assert api_task.dod[0]["type"] == "test"

    def test_expand_rejected_when_epic_not_accepted(self, client, db_session, owner_auth, goal, agent_factory):
        agent = agent_factory(workspace_id=owner_auth["org"].id)
        client.post(
            f"{BASE_URL}/goals/{goal.id}/epics/propose",
            json={"title": "Still proposed", "agent_id": agent.id},
            headers=owner_auth["headers"],
        )
        from models import Epic
        epic = Epic.query.filter_by(goal_id=goal.id).first()

        resp = client.post(f"{BASE_URL}/epics/{epic.id}/expand", json={}, headers=owner_auth["headers"])
        assert resp.status_code == 409
