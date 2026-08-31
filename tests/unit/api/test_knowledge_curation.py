"""Tests for P3.2 project knowledge auto-curation (proposals → human confirm)."""

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
def curator_ctx(db_session, owner_auth, agent_factory, project_factory, task_factory):
    """任务 + 失败 Agent，触发自动策展的最小上下文。"""
    from models import AgentExperience

    agent = agent_factory(workspace_id=owner_auth["org"].id)
    project = project_factory(owner_id=owner_auth["user"].id, organization_id=owner_auth["org"].id)
    task = task_factory(project_id=project.id, owner_id=owner_auth["user"].id)

    yield {"agent": agent, "task": task, "project": project}

    # 失败路径会写经验（P3.1）、确认路径会建知识条目（agent 维度）；
    # agent_factory teardown 删 Agent 前先清掉，避免 FK 冲突
    from models import AgentExperience, KnowledgeEntry

    AgentExperience.query.filter_by(agent_id=agent.id).delete()
    KnowledgeEntry.query.filter_by(agent_id=agent.id).delete()
    db_session.commit()


class TestProposeFromFailure:
    def test_failure_creates_proposal_and_is_idempotent(self, db_session, owner_auth, curator_ctx):
        from services.failure_recovery import handle_failed_commit
        from models import ProjectKnowledgeProposal

        task, agent = curator_ctx["task"], curator_ctx["agent"]

        result = handle_failed_commit(
            task, agent, attempt_id="att-cur-1",
            failure_code="TESTS_FAILED", failure_reason="assert x == y",
        )
        assert result["action"] == "repair_created"

        proposals = ProjectKnowledgeProposal.query.filter_by(project_id=task.project_id).all()
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.status == "proposed"
        assert proposal.source_type == "failure_attribution"
        assert proposal.proposed_by_agent_id == agent.id
        assert proposal.source_ref["category"] == "test_failure"
        assert proposal.title.startswith("[自动策展]")

        # 同一 attempt 再次提交（幂等路径）不产生新提案
        handle_failed_commit(
            task, agent, attempt_id="att-cur-1",
            failure_code="TESTS_FAILED", failure_reason="assert x == y",
        )
        assert ProjectKnowledgeProposal.query.filter_by(project_id=task.project_id).count() == 1

    def test_different_category_creates_separate_proposal(self, db_session, owner_auth, curator_ctx):
        from services.knowledge_curation import propose_from_failure
        from models import ProjectKnowledgeProposal

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        propose_from_failure(task, agent, "test_failure", "a")
        propose_from_failure(task, agent, "timeout", "b")

        assert ProjectKnowledgeProposal.query.filter_by(project_id=task.project_id).count() == 2

    def test_recovery_cleanup(self, db_session, owner_auth, curator_ctx):
        """失败产生的修复子任务/提案不阻塞 factory teardown 的 FK 清理。"""
        from services.failure_recovery import handle_failed_commit
        from models import ProjectKnowledgeProposal, Task

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        handle_failed_commit(
            task, agent, attempt_id="att-cur-clean",
            failure_code="TESTS_FAILED", failure_reason="x",
        )
        ProjectKnowledgeProposal.query.filter_by(project_id=task.project_id).delete()
        Task.query.filter(Task.parent_task_id == task.id).delete()
        db_session.commit()
        assert ProjectKnowledgeProposal.query.filter_by(project_id=task.project_id).count() == 0


class TestProposalDecisionAPI:
    def test_confirm_creates_project_knowledge_entry(self, client, db_session, owner_auth, curator_ctx):
        from services.knowledge_curation import propose_from_failure
        from models import KnowledgeEntry, ProjectKnowledgeProposal

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        proposal = propose_from_failure(task, agent, "test_failure", "assert x == y")
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/knowledge-proposals/{proposal.id}/confirm",
            json={"title": "永远先跑测试再提交"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["proposal"]["status"] == "confirmed"
        assert data["knowledge_entry"]["title"] == "永远先跑测试再提交"

        entry = db_session.get(KnowledgeEntry, data["knowledge_entry"]["id"])
        assert entry.shared_with_project is True
        assert entry.project_id == task.project_id
        assert entry.agent_id == agent.id

        db_session.expire(proposal)
        assert proposal.status == "confirmed"
        assert proposal.decided_by_user_id == owner_auth["user"].id

    def test_confirm_is_idempotent(self, client, db_session, owner_auth, curator_ctx):
        from services.knowledge_curation import propose_from_failure
        from models import KnowledgeEntry

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        proposal = propose_from_failure(task, agent, "timeout", "timed out")
        db_session.commit()

        first = client.post(
            f"{BASE_URL}/knowledge-proposals/{proposal.id}/confirm",
            json={"title": "第一次确认标题"}, headers=owner_auth["headers"],
        )
        assert first.status_code == 200
        second = client.post(
            f"{BASE_URL}/knowledge-proposals/{proposal.id}/confirm",
            json={"title": "第二次不应生效"}, headers=owner_auth["headers"],
        )
        assert second.status_code == 200
        assert second.get_json()["data"]["knowledge_entry"]["title"] == "第一次确认标题"
        assert KnowledgeEntry.query.filter_by(project_id=task.project_id).count() == 1

    def test_dismiss_archives_without_entry(self, client, db_session, owner_auth, curator_ctx):
        from services.knowledge_curation import propose_from_failure
        from models import KnowledgeEntry, ProjectKnowledgeProposal

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        proposal = propose_from_failure(task, agent, "lint_failure", "lint error")
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/knowledge-proposals/{proposal.id}/dismiss",
            json={"reason": "不值得沉淀"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["proposal"]["status"] == "dismissed"
        assert KnowledgeEntry.query.filter_by(project_id=task.project_id).count() == 0

    def test_list_filter_by_status(self, client, db_session, owner_auth, curator_ctx):
        from services.knowledge_curation import propose_from_failure

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        propose_from_failure(task, agent, "test_failure", "a")
        propose_from_failure(task, agent, "timeout", "b")
        db_session.commit()

        listed = client.get(
            f"{BASE_URL}/projects/{task.project_id}/knowledge-proposals?status=proposed",
            headers=owner_auth["headers"],
        )
        assert listed.status_code == 200
        assert listed.get_json()["data"]["pagination"]["total"] == 2

        bad = client.get(
            f"{BASE_URL}/projects/{task.project_id}/knowledge-proposals?status=nope",
            headers=owner_auth["headers"],
        )
        assert bad.status_code == 400

    def test_decision_requires_manage_access(self, client, db_session, _isolated_app, owner_auth, curator_ctx, user_factory):
        from services.knowledge_curation import propose_from_failure
        from flask_jwt_extended import create_access_token

        task, agent = curator_ctx["task"], curator_ctx["agent"]
        proposal = propose_from_failure(task, agent, "test_failure", "a")
        db_session.commit()

        outsider = user_factory()
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        denied = client.post(
            f"{BASE_URL}/knowledge-proposals/{proposal.id}/confirm",
            json={}, headers=headers,
        )
        assert denied.status_code == 403
