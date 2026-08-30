"""Tests for P2.4 reviewer gate (role templates, review evidence, merge blocking)."""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))

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
def review_setup(client, db_session, owner_auth, agent_factory, project_factory, task_factory):
    """绑定仓库（启用评审者关卡）+ 任务 + PR 证据；返回评审者/执行者 agent。"""
    from models import TaskEvidenceRecord

    executor = agent = agent_factory(workspace_id=owner_auth["org"].id)
    client.put(
        f"{BASE_URL}/projects/{project.id}" if False else f"{BASE_URL}/projects/0/repo",
        json={}, headers=owner_auth["headers"],
    ) if False else None
    project = project_factory(owner_id=owner_auth["user"].id, organization_id=owner_auth["org"].id)
    resp = client.put(
        f"{BASE_URL}/projects/{project.id}/repo",
        json={"repo_owner": "acme", "repo_name": "widget", "require_agent_review": True},
        headers=owner_auth["headers"],
    )
    assert resp.status_code == 200
    task = task_factory(project_id=project.id, owner_id=owner_auth["user"].id, title="Review gate task")
    db_session.add(TaskEvidenceRecord(
        task_id=task.id, evidence_type="pr", status="unknown",
        detail={"pr_number": 10, "repo": "acme/widget"}, created_by="test",
    ))
    db_session.commit()
    db_session.expire(task)
    return {"executor": executor, "task": task, "project": project}


class TestBuiltinRoleTemplates:
    def test_seed_creates_three_roles(self, db_session, owner_auth):
        from services.review_gate import ensure_builtin_role_templates
        from models import AgentRoleTemplate

        created = ensure_builtin_role_templates(owner_auth["org"].id, created_by_user_id=owner_auth["user"].id)
        assert created == 3
        names = {t.name for t in AgentRoleTemplate.query.filter_by(workspace_id=owner_auth["org"].id).all()}
        assert {"developer", "reviewer", "tester"} <= names

    def test_seed_is_idempotent(self, db_session, owner_auth):
        from services.review_gate import ensure_builtin_role_templates

        first = ensure_builtin_role_templates(owner_auth["org"].id, created_by_user_id=owner_auth["user"].id)
        second = ensure_builtin_role_templates(owner_auth["org"].id, created_by_user_id=owner_auth["user"].id)
        assert first == 3
        assert second == 0


class TestReviewerGate:
    def _merge(self, client, task, owner_auth):
        fake_client = MagicMock()
        fake_client.merge_pull_request.return_value = {"sha": "gatesha", "merged": True}
        fake_client.get_pull_request.return_value = {
            "number": 10, "state": "closed", "merged": True,
            "html_url": "https://github.com/acme/widget/pull/10",
            "head": {"ref": "agent/x"}, "base": {"ref": "main"},
        }
        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            return client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
                json={"merge_method": "squash"},
                headers=owner_auth["headers"],
            ), fake_client

    def test_merge_blocked_without_review(self, client, db_session, owner_auth, review_setup):
        task = review_setup["task"]
        resp, fake_client = self._merge(client, task, owner_auth)

        assert resp.status_code == 409
        assert resp.get_json()["error_details"]["code"] == "AGENT_REVIEW_REQUIRED"
        fake_client.merge_pull_request.assert_not_called()

    def test_merge_allowed_after_passing_review(self, client, db_session, owner_auth, review_setup):
        from models import Task

        task = review_setup["task"]

        # 无评审时被关卡拦截
        resp, _ = self._merge(client, task, owner_auth)
        assert resp.status_code == 409

        # 评审者提交通过评审（未指定绑定评审者时任意 agent 可评）
        reviewer = review_setup["executor"]
        review_resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/review",
            json={"decision": "approved", "reviewer_agent_id": reviewer.id,
                  "summary": "LGTM", "pr_number": 10},
            headers=owner_auth["headers"],
        )
        assert review_resp.status_code == 200, review_resp.get_json()

        # 再次合并 → 关卡通过，合并执行
        resp2, fake_client = self._merge(client, task, owner_auth)
        assert resp2.status_code == 200, resp2.get_json()
        assert resp2.get_json()["data"]["merged"] is True

        db_session.expire(task)
        assert task.status.value == "done"

    def test_non_designated_reviewer_rejected(self, client, db_session, owner_auth, review_setup):
        from models import Agent

        other_agent = Agent(workspace_id=owner_auth["org"].id, creator_user_id=owner_auth["user"].id,
                            name="Not reviewer")
        db_session.add(other_agent)
        db_session.commit()

        # 指定评审者为 executor 之外：直接把绑定 reviewer 指向新建 agent
        binding_resp = client.put(
            f"{BASE_URL}/projects/{review_setup['project'].id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget",
                  "require_agent_review": True, "reviewer_agent_id": other_agent.id},
            headers=owner_auth["headers"],
        )
        assert binding_resp.status_code == 200

        resp = client.post(
            f"{BASE_URL}/tasks/{review_setup['task'].id}/pull-request/review",
            json={"decision": "approved", "reviewer_agent_id": other_agent.id + 1},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 403
        assert resp.get_json()["error_details"]["code"] == "NOT_DESIGNATED_REVIEWER"
