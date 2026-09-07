"""知识自动策展服务（services/knowledge_curation.py）缺口补测。

补齐：幂等去重命中路径、评审来源提案、无发起 Agent 的确认拒绝、
已确认提案不可驳回。与 api 层测试互补，此处直测服务函数。
"""

import uuid

import pytest

from models import (
    KnowledgeEntry,
    Project,
    ProjectKnowledgeProposal,
    Task,
    User,
    db,
)
from services.knowledge_curation import (
    confirm_proposal,
    dismiss_proposal,
    propose_from_failure,
    propose_from_review,
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
def task():
    user = User(username=f"kc_{uuid.uuid4().hex[:8]}",
                email=f"kc_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    project = Project(name=f"kp_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(project)
    db.session.flush()
    task = Task(project_id=project.id, owner_id=user.id,
                title="策展源任务", status="TODO", priority="MEDIUM")
    db.session.add(task)
    db.session.commit()
    return task


class TestProposeFromFailure:
    def test_dedupes_same_task_and_category(self, task):
        first = propose_from_failure(task, None, "test_failure", "assert 1 == 2")
        second = propose_from_failure(task, None, "test_failure", "assert 1 == 2")
        assert first.id == second.id
        assert second.status == ProjectKnowledgeProposal.STATUS_PROPOSED
        rows = ProjectKnowledgeProposal.query.filter_by(
            project_id=task.project_id).all()
        assert len(rows) == 1

    def test_different_category_creates_separate(self, task):
        a = propose_from_failure(task, None, "test_failure", "x")
        b = propose_from_failure(task, None, "timeout", "y")
        assert a.id != b.id


class TestProposeFromReview:
    def test_creates_review_insight(self, task):
        proposal = propose_from_review(
            task, None, reviewer_agent_id=7, pr_number=42,
            review_summary="整体良好，边界待补")
        assert proposal.proposal_type == "review_insight"
        assert proposal.source_type == ProjectKnowledgeProposal.SOURCE_PR_REVIEW
        assert proposal.source_ref["pr_number"] == 42
        assert proposal.source_ref["reviewer_agent_id"] == 7
        assert "PR #42" in proposal.title

    def test_review_dedupes_per_pr(self, task):
        a = propose_from_review(task, None, 7, 42, "一次")
        b = propose_from_review(task, None, 7, 42, "二次")
        assert a.id == b.id


class TestConfirmAndDismiss:
    def test_confirm_requires_originating_agent(self, task):
        proposal = propose_from_failure(task, None, "timeout", "t")
        user = User.query.first()
        with pytest.raises(ValueError, match="no originating agent"):
            confirm_proposal(proposal, user)

    def test_confirm_creates_entry_and_is_idempotent(self, task):
        from models import Agent
        agent = Agent(workspace_id=None, owner_id=task.owner_id,
                      creator_user_id=task.owner_id, name="curator")
        db.session.add(agent)
        db.session.commit()
        proposal = propose_from_failure(task, agent, "build_failure", "boom")
        user = User.query.first()

        entry = confirm_proposal(proposal, user, title="构建教训")
        assert entry.id is not None
        assert entry.entry_type == "rule"
        assert entry.project_id == task.project_id
        assert entry.agent_id == agent.id

        again = confirm_proposal(proposal, user)
        assert again.id == entry.id
        assert KnowledgeEntry.query.count() == 1

    def test_dismiss_confirmed_raises(self, task):
        from models import Agent
        agent = Agent(workspace_id=None, owner_id=task.owner_id,
                      creator_user_id=task.owner_id, name="curator2")
        db.session.add(agent)
        db.session.commit()
        proposal = propose_from_failure(task, agent, "lint_failure", "lint")
        user = User.query.first()
        confirm_proposal(proposal, user)

        with pytest.raises(ValueError, match="cannot be dismissed"):
            dismiss_proposal(proposal, user)

    def test_dismiss_archives(self, task):
        proposal = propose_from_failure(task, None, "auth_error", "denied")
        user = User.query.first()
        dismiss_proposal(proposal, user, reason="不适用")
        assert proposal.status == ProjectKnowledgeProposal.STATUS_DISMISSED
        assert proposal.dismissal_reason == "不适用"
