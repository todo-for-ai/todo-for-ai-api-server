"""Tests for P3.3 insight actions (load forecast throttle, rework → DoD recommendations)."""

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


@pytest.fixture
def agent(db_session, owner_auth):
    import uuid as _uuid
    from models import Agent, AgentStatus

    agent = Agent(
        name=f"load-agent-{str(_uuid.uuid4())[:6]}",
        display_name="Load Agent",
        workspace_id=owner_auth["org"].id,
        creator_user_id=owner_auth["user"].id,
        status=AgentStatus.ACTIVE,
        capabilities=["python"],
    )
    db_session.add(agent)
    db_session.commit()
    return agent


@pytest.fixture
def project_ctx(db_session, owner_auth):
    import uuid as _uuid
    from models import Project

    project = Project(
        name=f"insight-project-{str(_uuid.uuid4())[:6]}",
        owner_id=owner_auth["user"].id,
        organization_id=owner_auth["org"].id,
        status="ACTIVE",
    )
    db_session.add(project)
    db_session.commit()
    return project


def _assignment(db_session, agent, task, state, completed_at=None):
    from models import TaskAssignment

    row = TaskAssignment(
        task_id=task.id, agent_id=agent.id, state=state,
        completed_at=completed_at,
    )
    db_session.add(row)
    return row


class TestLoadForecast:
    def test_overloaded_agent_is_throttled(self, db_session, owner_auth, agent, task_factory):
        from services.insight_actions import predict_agent_load, compute_load_throttle
        from models import TaskAssignmentState

        now = datetime.utcnow()
        # 3 个活跃（无排空迹象：窗口内零完成）→ 积压 ∞ → 超载
        for _ in range(3):
            _assignment(db_session, agent, task_factory(owner_id=owner_auth["user"].id),
                        TaskAssignmentState.RUNNING)
        # 窗口外完成 1 单（不计入吞吐）
        _assignment(db_session, agent, task_factory(owner_id=owner_auth["user"].id),
                    TaskAssignmentState.DONE, completed_at=now - timedelta(days=30))
        db_session.commit()

        load = predict_agent_load(agent)
        assert load["active_assignments"] == 3
        assert load["completed_window"] == 0
        assert load["overloaded"] is True

        penalty, load2 = compute_load_throttle(agent)
        assert penalty > 0 and load2["overloaded"] is True

    def test_healthy_agent_not_throttled(self, db_session, owner_auth, agent, task_factory):
        from services.insight_actions import predict_agent_load, compute_load_throttle
        from models import TaskAssignmentState

        now = datetime.utcnow()
        _assignment(db_session, agent, task_factory(owner_id=owner_auth["user"].id),
                    TaskAssignmentState.RUNNING)
        for i in range(7):
            _assignment(db_session, agent, task_factory(owner_id=owner_auth["user"].id),
                        TaskAssignmentState.DONE, completed_at=now - timedelta(days=i))
        db_session.commit()

        load = predict_agent_load(agent)
        assert load["active_assignments"] == 1
        assert load["throughput_per_day"] >= 0.9
        assert load["overloaded"] is False

        penalty, _ = compute_load_throttle(agent)
        assert penalty == 0

    def test_forecast_endpoint(self, client, owner_auth, agent):
        resp = client.get(
            f"{BASE_URL}/agents/{agent.id}/load-forecast",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["active_assignments"] == 0
        assert data["overloaded"] is False
        assert data["throttled_in_dispatch"] is False


class TestDispatchThrottleIntegration:
    def test_score_includes_load_throttle(self, db_session, owner_auth, agent, task_factory):
        from api.agents._shared import score_task_for_agent
        from models import TaskAssignmentState

        for _ in range(3):
            _assignment(db_session, agent, task_factory(owner_id=owner_auth["user"].id),
                        TaskAssignmentState.RUNNING)
        db_session.commit()

        task = task_factory(owner_id=owner_auth["user"].id, title="Python job", tags=["python"])
        db_session.expire(task)

        result = score_task_for_agent(task, agent)
        assert result["load_throttle_penalty"] > 0
        assert result["load_forecast"]["overloaded"] is True
        # 活跃负载本身也有既有惩罚，总分应被扣减但保持非负
        assert result["score"] >= 0


class TestDodRecommendations:
    def test_repair_tasks_drive_recommendations(self, db_session, owner_auth, project_ctx, task_factory):
        from services.insight_actions import recommend_dod_templates

        # 3 个 test_failure 修复任务 + 1 个 timeout
        for category in ("test_failure", "test_failure", "test_failure", "timeout"):
            task_factory(
                project_id=project_ctx.id,
                owner_id=owner_auth["user"].id,
                title=f"repair {category}",
                creator_type="ai",
                creator_identifier=f"recovery:{category}",
                parent_task_id=task_factory(project_id=project_ctx.id).id,
            )

        rec = recommend_dod_templates(project_ctx.id)
        assert rec["rework"]["total_repair_tasks"] == 4
        by_cat = rec["rework"]["by_category"]
        assert by_cat["test_failure"] == 3
        assert by_cat["timeout"] == 1

        recs = {item["category"]: item for item in rec["recommendations"]}
        assert recs["test_failure"]["rework_count"] == 3
        assert recs["test_failure"]["recommended"] is True
        assert recs["test_failure"]["dod_template"][0]["type"] == "test"
        assert recs["timeout"]["dod_template"][0]["type"] == "manual"
        assert rec["has_recommendations"] is True

    def test_no_rework_no_recommendations(self, db_session, owner_auth, project_ctx):
        from services.insight_actions import recommend_dod_templates

        rec = recommend_dod_templates(project_ctx.id)
        assert rec["rework"]["total_repair_tasks"] == 0
        assert rec["has_recommendations"] is False

    def test_apply_merges_and_dedupes(self, client, db_session, owner_auth, project_ctx, task_factory):
        task = task_factory(project_id=project_ctx.id, owner_id=owner_auth["user"].id)
        task.dod = [{"type": "test", "value": "run project test suite"}]
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/projects/{project_ctx.id}/insights/dod-recommendations/apply",
            json={"task_id": task.id, "categories": ["test_failure", "lint_failure"]},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        # test 模板已存在（去重不加），lint 模板新增
        assert len(data["added"]) == 1
        assert data["added"][0]["type"] == "lint"
        assert len(data["dod"]) == 2

        # 再次 apply → 无新增
        again = client.post(
            f"{BASE_URL}/projects/{project_ctx.id}/insights/dod-recommendations/apply",
            json={"task_id": task.id, "categories": ["test_failure", "lint_failure"]},
            headers=owner_auth["headers"],
        )
        assert again.status_code == 200
        assert again.get_json()["data"]["added"] == []

    def test_apply_requires_manage(self, client, db_session, _isolated_app, owner_auth, project_ctx, task_factory, user_factory):
        import uuid as _uuid
        from flask_jwt_extended import create_access_token
        from models import User
        from werkzeug.security import generate_password_hash

        # 项目 owner 是 owner_auth 用户；另建普通成员不可管理
        outsider = User(username=f"out_{str(_uuid.uuid4())[:6]}", email=f"out_{str(_uuid.uuid4())[:6]}@x.com")
        outsider.password_hash = generate_password_hash("password123")
        db_session.add(outsider)
        db_session.commit()
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.post(
            f"{BASE_URL}/projects/{project_ctx.id}/insights/dod-recommendations/apply",
            json={"task_id": 1, "categories": ["test_failure"]},
            headers=headers,
        )
        assert resp.status_code == 403
