"""P1.1 代码平面测试：项目仓库绑定 + 任务 PR 创建/同步（GitHub API 已 mock）。"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _cleanup_repo_bindings(db_session):
    """清理项目绑定行，避免会话级测试库中项目 id 回收后的残留绑定。"""
    from models import ProjectRepoBinding, TaskEvidenceRecord

    def _purge():
        db_session.rollback()
        db_session.query(ProjectRepoBinding).delete(synchronize_session=False)
        db_session.query(TaskEvidenceRecord).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()


@pytest.fixture
def owner_auth(app, db_session):
    """创建用户并返回其 JWT headers。"""
    import uuid as _uuid
    from models import User
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique_id = str(_uuid.uuid4())[:8]
    user = User(username=f"testuser_{unique_id}", email=f"test_{unique_id}@example.com")
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def owned_project(db_session, owner_auth, project_factory):
    """owner_auth 用户拥有的项目。"""
    project = project_factory(owner_id=owner_auth["user"].id)
    return project


def _pr_data(number=7, state="open", merged=False):
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "title": "[Task #1] sample",
        "html_url": f"https://github.com/acme/widget/pull/{number}",
        "merge_commit_sha": "abc123" if merged else None,
        "head": {"ref": "agent/task-1"},
        "base": {"ref": "main"},
    }


class TestProjectRepoBinding:
    def test_bind_and_get_repo(self, client, owner_auth, owned_project):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "default_branch": "main"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["repo_full_name"] == "acme/widget"
        assert data["has_binding_token"] is False
        assert "token_encrypted" not in data

        resp = client.get(f"{BASE_URL}/projects/{owned_project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["repo_name"] == "widget"

    def test_bind_rejects_non_github_provider(self, client, owner_auth, owned_project):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "provider": "gitlab"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_bind_requires_manage_permission(self, client, db_session, owner_auth, user_factory, project_factory):
        other_user = user_factory()
        project = project_factory(owner_id=other_user.id)
        resp = client.put(
            f"{BASE_URL}/projects/{project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 403

    def test_get_repo_404_when_unbound(self, client, owner_auth, owned_project):
        resp = client.get(f"{BASE_URL}/projects/{owned_project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_REPO_BOUND"

    def test_unbind(self, client, owner_auth, owned_project):
        client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget"},
            headers=owner_auth["headers"],
        )
        resp = client.delete(f"{BASE_URL}/projects/{owned_project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["unbound"] is True


class TestTaskPullRequest:
    def _bind(self, client, owner_auth, owned_project):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200

    def test_create_pr_success(self, client, db_session, owner_auth, owned_project, project_factory, task_factory):
        from models import TaskEvidenceRecord

        self._bind(client, owner_auth, owned_project)
        task = task_factory(
            project_id=owned_project.id,
            owner_id=owner_auth["user"].id,
            title="PR task",
            is_ai_task=True,
        )

        fake_client = MagicMock()
        fake_client.ensure_branch.return_value = True
        fake_client.create_pull_request.return_value = _pr_data()

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/task-1"},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["pr_created"] is True
        assert data["pr"]["number"] == 7
        assert data["pr"]["url"].startswith("https://github.com/acme/widget/pull/")

        evidence = TaskEvidenceRecord.query.filter_by(task_id=task.id, evidence_type="pr").first()
        assert evidence is not None
        assert evidence.detail["pr_number"] == 7
        assert evidence.status == "unknown"  # open PR 尚无结论

    def test_create_pr_no_commits_yet_is_soft_success(
        self, client, db_session, owner_auth, owned_project, project_factory, task_factory
    ):
        self._bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="Empty branch")

        fake_client = MagicMock()
        fake_client.ensure_branch.return_value = True
        fake_client.create_pull_request.side_effect = __import__(
            "services.github_client", fromlist=["GitHubClientError"]
        ).GitHubClientError("GitHub API error 422: No commits between main and agent/task-1", 422)

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/task-1"},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["pr_created"] is False
        assert data["reason"] == "no_commits_yet"
        assert data["branch_ready"] is True

    def test_create_pr_requires_binding(self, client, db_session, owner_auth, owned_project, project_factory, task_factory):
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="No binding")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"head_branch": "agent/task-1"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_REPO_BOUND"

    def test_sync_merged_pr_completes_task(
        self, client, db_session, owner_auth, owned_project, project_factory, task_factory
    ):
        from models import Task, TaskEvidenceRecord

        self._bind(client, owner_auth, owned_project)
        task = task_factory(
            project_id=owned_project.id,
            owner_id=owner_auth["user"].id,
            title="Merge me",
            is_ai_task=True,
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown",
            summary="PR #7", detail={"pr_number": 7}, created_by="test",
        ))
        db_session.commit()
        db_session.expire(task)

        fake_client = MagicMock()
        fake_client.get_pull_request.return_value = _pr_data(state="closed", merged=True)

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.get(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["pr"]["merged"] is True
        assert data["task_status"] == "done"

        db_session.expire(task)
        assert task.status.value == "done"
        assert task.completion_rate == 100
        evidence = TaskEvidenceRecord.query.filter_by(task_id=task.id, evidence_type="pr").first()
        assert evidence.status == "passed"

    def test_sync_without_pr_returns_404(self, client, db_session, owner_auth, owned_project, project_factory, task_factory):
        self._bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="No PR yet")
        resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_PR"


class TestGitHubClientUnit:
    def test_resolve_token_falls_back_to_env(self, monkeypatch):
        from services.github_client import resolve_token

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        binding = MagicMock()
        binding.token_encrypted = None
        assert resolve_token(binding) is None

        monkeypatch.setenv("GITHUB_TOKEN", "gh_env_token")
        assert resolve_token(binding) == "gh_env_token"

    def test_client_error_carries_status(self):
        from services.github_client import GitHubClientError

        err = GitHubClientError("boom", 422, details={"x": 1})
        assert err.status_code == 422
        assert err.details == {"x": 1}

    def test_full_name(self):
        from models import ProjectRepoBinding

        b = ProjectRepoBinding(repo_owner="acme", repo_name="widget")
        assert b.repo_full_name == "acme/widget"
