"""评审者关卡服务（services/review_gate.py）缺口补测。

补齐：编排 role_assignments 解析评审者、PR 号过滤跳过不相关证据、
自评禁止分支。与 api 层测试互补，此处直测服务函数。
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import Task, TaskEvidenceRecord, User, db
from services.review_gate import (
    check_agent_review_gate,
    resolve_reviewer_agent_id,
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
    user = User(username=f"rg_{uuid.uuid4().hex[:8]}",
                email=f"rg_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    from models import Project
    project = Project(name=f"rp_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(project)
    db.session.flush()
    task = Task(project_id=project.id, owner_id=user.id,
                title="评审关卡任务", status="TODO", priority="MEDIUM")
    db.session.add(task)
    db.session.commit()
    return task


def _binding(require=True, reviewer_id=None):
    return SimpleNamespace(require_agent_review=require,
                           reviewer_agent_id=reviewer_id)


def _review(task, agent_id, status="passed", detail=None):
    db.session.add(TaskEvidenceRecord(
        task_id=task.id, agent_id=agent_id, evidence_type="review",
        status=status, summary="评审", detail=detail or {},
    ))
    db.session.commit()


class TestResolveReviewer:
    def test_binding_explicit_wins(self, task, monkeypatch):
        assert resolve_reviewer_agent_id(task, _binding(reviewer_id=5)) == 5

    def test_via_orchestration_assignments(self, task, monkeypatch):
        orchestration = SimpleNamespace(
            role_assignments={"reviewer": 9, "developer": 3})
        stub = MagicMock()
        stub.query.filter_by.return_value.order_by.return_value.first \
            .return_value = orchestration
        monkeypatch.setattr("models.TeamTaskOrchestration", stub)
        assert resolve_reviewer_agent_id(task, None) == 9

    def test_orchestration_without_dict_returns_none(self, task, monkeypatch):
        orchestration = SimpleNamespace(role_assignments="not-a-dict")
        stub = MagicMock()
        stub.query.filter_by.return_value.order_by.return_value.first \
            .return_value = orchestration
        monkeypatch.setattr("models.TeamTaskOrchestration", stub)
        assert resolve_reviewer_agent_id(task, None) is None

    def test_nothing_configured_returns_none(self, task):
        assert resolve_reviewer_agent_id(task, None) is None


class TestReviewGate:
    def test_gate_disabled_passes(self, task):
        assert check_agent_review_gate(task, None, pr_number=1) == {
            "passed": True, "reason": "gate_disabled"}
        assert check_agent_review_gate(task, _binding(require=False),
                                       pr_number=1)["passed"] is True

    def test_blocked_without_any_review(self, task):
        result = check_agent_review_gate(task, _binding(), pr_number=8)
        assert result == {"passed": False, "reason": "agent_review_required",
                          "reviewer_agent_id": None, "pr_number": 8}

    def test_failed_review_does_not_pass(self, task):
        _review(task, agent_id=4, status="failed")
        result = check_agent_review_gate(task, _binding(), pr_number=8)
        assert result["passed"] is False

    def test_irrelevant_pr_evidence_skipped(self, task):
        # PR 9 的通过证据对 PR 8 无效（detail.pr_number 不匹配则跳过）
        _review(task, agent_id=4, status="passed", detail={"pr_number": 9})
        result = check_agent_review_gate(task, _binding(), pr_number=8)
        assert result["passed"] is False

        # PR 号匹配的证据生效
        _review(task, agent_id=4, status="passed", detail={"pr_number": 8})
        result = check_agent_review_gate(task, _binding(), pr_number=8)
        assert result["passed"] is True

    def test_self_review_forbidden(self, task):
        _review(task, agent_id=6, status="passed", detail={"pr_number": 8})
        result = check_agent_review_gate(task, _binding(), pr_number=8,
                                         author_agent_id=6)
        assert result["passed"] is False
        assert result["reason"] == "self_review_forbidden"
        assert result["reviewer_agent_id"] == 6

    def test_passing_review_reports_reviewer(self, task):
        _review(task, agent_id=5, status="passed", detail={"pr_number": 8})
        result = check_agent_review_gate(task, _binding(), pr_number=8,
                                         author_agent_id=6)
        assert result == {"passed": True, "reason": "review_passed",
                          "reviewer_agent_id": 5, "pr_number": 8}

    def test_evidence_without_pr_number_matches_any(self, task):
        _review(task, agent_id=5, status="passed", detail={})
        result = check_agent_review_gate(task, _binding(), pr_number=None)
        assert result["passed"] is True
