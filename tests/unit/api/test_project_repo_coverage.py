"""project_repo 拆包补齐测试：参数校验、错误兜底、审批执行与列表分支（迭代 128）。

覆盖缺口来自拆分前的历史欠账；端点行为与拆分前一致，本文件补齐至 100% 行覆盖。
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(autouse=True)
def _cleanup_repo_bindings(db_session):
    from models import AgentTaskEvent, AuditLog, ProjectRepoBinding, TaskEvidenceRecord

    def _purge():
        db_session.rollback()
        db_session.query(AgentTaskEvent).delete(synchronize_session=False)
        db_session.query(ProjectRepoBinding).delete(synchronize_session=False)
        db_session.query(TaskEvidenceRecord).delete(synchronize_session=False)
        db_session.query(AuditLog).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()


@pytest.fixture
def owner_auth(app, db_session):
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
    return project_factory(owner_id=owner_auth["user"].id)


@pytest.fixture
def org_project(db_session, owner_auth, project_factory):
    """带组织 id 的项目（审批事件要求 workspace）。"""
    from models import Project
    project = project_factory(owner_id=owner_auth["user"].id)
    project.organization_id = 9999
    db_session.add(project)
    db_session.commit()
    return project


def _make_interaction(db_session, task_id, payload_overrides=None):
    from datetime import datetime

    from models import AgentTaskEvent

    payload = {
        "interaction_id": f"i-{uuid.uuid4().hex[:8]}",
        "interaction_type": "pr_create",
        "status": "pending_approval",
        "pr_number": None,
        "metadata": {"head_branch": "agent/x", "base_branch": "main", "title": "t"},
    }
    payload.update(payload_overrides or {})
    event = AgentTaskEvent(
        task_id=task_id,
        attempt_id=f"att-{uuid.uuid4().hex[:8]}",
        workspace_id=9999,
        event_type="interaction_request",
        seq=1,
        event_timestamp=datetime.utcnow(),
        payload=payload,
    )
    db_session.add(event)
    db_session.commit()
    return payload["interaction_id"]


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


def _bind(client, owner_auth, project, level=1, token=None):
    payload = {"repo_owner": "acme", "repo_name": "widget", "autonomy_level": level}
    if token:
        payload["repo_token"] = token
    resp = client.put(
        f"{BASE_URL}/projects/{project.id}/repo",
        json=payload,
        headers=owner_auth["headers"],
    )
    assert resp.status_code == 200, resp.get_json()


class TestBindingValidation:
    """binding.py：404/403/校验/异常兜底。"""

    def test_get_repo_404_project_missing(self, client, owner_auth):
        resp = client.get(f"{BASE_URL}/projects/987654/repo", headers=owner_auth["headers"])
        assert resp.status_code == 404

    def test_get_repo_403_not_owner(self, client, db_session, owner_auth, user_factory, project_factory):
        other = user_factory()
        project = project_factory(owner_id=other.id)
        resp = client.get(f"{BASE_URL}/projects/{project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code in (403, 404)

    def test_get_repo_500_when_lookup_fails(self, client, owner_auth, owned_project, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr("api.project_repo.binding._get_binding", boom)
        resp = client.get(f"{BASE_URL}/projects/{owned_project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code == 500

    def test_put_repo_404_project_missing(self, client, owner_auth):
        resp = client.put(
            f"{BASE_URL}/projects/987654/repo",
            json={"repo_owner": "a", "repo_name": "b"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_put_repo_rejects_non_int_autonomy(self, client, owner_auth, owned_project):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "a", "repo_name": "b", "autonomy_level": "abc"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_put_repo_with_token_marks_presence(self, client, owner_auth, owned_project, monkeypatch):
        from api.project_repo import binding as binding_mod

        monkeypatch.setattr(binding_mod, "_encrypt_secret", lambda v: f"enc::{v}")
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "token": "ghp_secret"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        assert "token_encrypted" not in resp.get_json()["data"]

    def test_put_repo_500_on_commit_failure(self, client, owner_auth, owned_project, monkeypatch):
        from api.project_repo import binding as binding_mod

        def boom():
            raise RuntimeError("commit fail")
        monkeypatch.setattr(binding_mod.db.session, "commit", boom)
        try:
            resp = client.put(
                f"{BASE_URL}/projects/{owned_project.id}/repo",
                json={"repo_owner": "a", "repo_name": "b"},
                headers=owner_auth["headers"],
            )
            assert resp.status_code == 500
        finally:
            # 会话级共享 session 的全局手术必须就地复原：boom 若泄漏进
            # fixture teardown，会毒化共享会话并级联污染后续所有模块
            monkeypatch.undo()
            binding_mod.db.session.rollback()

    def test_delete_repo_404_project_missing(self, client, owner_auth):
        resp = client.delete(f"{BASE_URL}/projects/987654/repo", headers=owner_auth["headers"])
        assert resp.status_code == 404

    def test_delete_repo_403_not_owner(self, client, db_session, owner_auth, user_factory, project_factory):
        other = user_factory()
        project = project_factory(owner_id=other.id)
        resp = client.delete(f"{BASE_URL}/projects/{project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code in (403, 404)

    def test_delete_repo_500_on_commit_failure(self, client, owner_auth, owned_project, monkeypatch):
        from api.project_repo import binding as binding_mod

        _bind(client, owner_auth, owned_project)
        def boom():
            raise RuntimeError("commit fail")
        monkeypatch.setattr(binding_mod.db.session, "commit", boom)
        try:
            resp = client.delete(f"{BASE_URL}/projects/{owned_project.id}/repo", headers=owner_auth["headers"])
            assert resp.status_code == 500
        finally:
            monkeypatch.undo()
            binding_mod.db.session.rollback()


class TestCreatePrValidation:
    """pull_requests.py：创建/查询的校验与兜底。"""

    def test_create_404_no_binding(self, client, owner_auth, owned_project, task_factory):
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"head_branch": "agent/x"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_REPO_BOUND"

    def test_create_head_equals_base_rejected(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"head_branch": "main", "base_branch": "main"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_create_github_error_502(self, client, owner_auth, owned_project, task_factory):
        from services.github_client import GitHubClientError

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        fake_client = MagicMock()
        fake_client.create_pull_request.side_effect = GitHubClientError("boom", 502)
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/x"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 502

    def test_create_unexpected_error_500(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        fake_client = MagicMock()
        fake_client.ensure_branch.side_effect = RuntimeError("network down")
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/x"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 500

    def test_get_pr_404_no_binding(self, client, owner_auth, owned_project, task_factory):
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])
        assert resp.status_code == 404

    def test_get_pr_sync_error_502(self, client, db_session, owner_auth, owned_project, task_factory):
        from services.github_client import GitHubClientError
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 7}, created_by="test",
        ))
        db_session.commit()
        fake_client = MagicMock()
        fake_client.get_pull_request.side_effect = GitHubClientError("gh down", 502)
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])
        assert resp.status_code == 502

    def test_get_pr_sync_unexpected_error_500(self, client, db_session, owner_auth, owned_project, task_factory):
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 7}, created_by="test",
        ))
        db_session.commit()
        fake_client = MagicMock()
        fake_client.get_pull_request.side_effect = RuntimeError("parse fail")
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])
        assert resp.status_code == 500

    def test_l2_create_pr_auto_merges_immediately(
        self, client, db_session, owner_auth, org_project, task_factory
    ):
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, org_project, level=2)
        task = task_factory(
            project_id=org_project.id,
            owner_id=owner_auth["user"].id,
            title="L2 create auto merge",
            is_ai_task=True,
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="test", status="passed", summary="green", created_by="agent:1",
        ))
        db_session.commit()
        fake_client = MagicMock()
        fake_client.ensure_branch.return_value = True
        fake_client.create_pull_request.return_value = _pr_data(number=11)
        fake_client.get_pull_request.return_value = _pr_data(number=11, state="open", merged=False)
        fake_client.merge_pull_request.return_value = {"sha": "createam", "merged": True}

        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/x"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["pr_created"] is True
        assert data["auto_merged"] and data["auto_merged"]["merged"] is True
        fake_client.merge_pull_request.assert_called_once()

    def test_l2_create_auto_merge_failure_maps_to_merge_failed(
        self, client, db_session, owner_auth, org_project, task_factory
    ):
        from services.github_client import GitHubClientError
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, org_project, level=2)
        task = task_factory(
            project_id=org_project.id,
            owner_id=owner_auth["user"].id,
            title="L2 create merge failed",
            is_ai_task=True,
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="test", status="passed", summary="green", created_by="agent:1",
        ))
        db_session.commit()
        fake_client = MagicMock()
        fake_client.ensure_branch.return_value = True
        fake_client.create_pull_request.return_value = _pr_data(number=12)
        fake_client.merge_pull_request.side_effect = GitHubClientError("merge conflict", 409)

        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/x"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200, resp.get_json()
        auto = resp.get_json()["data"]["auto_merged"]
        assert auto["merged"] is False and auto["reason"] == "merge_failed"


class TestApproveValidation:
    """lifecycle.py approve 端点：校验、执行、兜底。"""

    def _make_interaction(self, db_session, task_id, payload_overrides=None):
        return _make_interaction(db_session, task_id, payload_overrides)

    def _make_interaction_impl(self, db_session, task_id, payload_overrides=None):
        from datetime import datetime

        from models import AgentTaskEvent

        payload = {
            "interaction_id": f"i-{uuid.uuid4().hex[:8]}",
            "interaction_type": "pr_create",
            "status": "pending_approval",
            "pr_number": None,
            "metadata": {"head_branch": "agent/x", "base_branch": "main", "title": "t"},
        }
        payload.update(payload_overrides or {})
        event = AgentTaskEvent(
            task_id=task_id,
            attempt_id=f"att-{uuid.uuid4().hex[:8]}",
            workspace_id=9999,
            event_type="interaction_request",
            seq=1,
            event_timestamp=datetime.utcnow(),
            payload=payload,
        )
        db_session.add(event)
        db_session.commit()
        return payload["interaction_id"]

    def test_approve_task_missing_404(self, client, owner_auth, owned_project):
        resp = client.post(
            f"{BASE_URL}/tasks/987654/pull-request/approve",
            json={"interaction_id": "i-1", "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_approve_403_for_non_member(self, client, db_session, owner_auth, user_factory, project_factory, task_factory):
        other = user_factory()
        project = project_factory(owner_id=other.id)
        task = task_factory(project_id=project.id, owner_id=other.id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": "i-1", "decision": "approved"},
            headers=owner_auth["headers"],
        )
        print("APPROVE_403_STATUS=", resp.status_code)
        assert resp.status_code == 403

    def test_approve_404_no_repo_bound(self, client, owner_auth, owned_project, task_factory):
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": "i-1", "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_REPO_BOUND"

    def test_approve_rejects_bad_decision(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": "i-1", "decision": "maybe"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_approve_rejects_bad_merge_method(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": "i-1", "decision": "approved", "merge_method": "force"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_approve_404_unknown_interaction(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": "i-none", "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_INTERACTION"

    def test_approve_409_already_approved(self, client, db_session, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id, {"status": "approved"})
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": iid, "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 409

    def test_approve_400_missing_head_branch(self, client, db_session, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id, {"metadata": {}})
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": iid, "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_approve_pr_create_422_soft_success(self, client, db_session, owner_auth, owned_project, task_factory):
        from services.github_client import GitHubClientError

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id)
        fake_client = MagicMock()
        fake_client.create_pull_request.side_effect = GitHubClientError("No commits", 422)
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": iid, "decision": "approved"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["pr_created"] is False
        assert resp.get_json()["data"]["reason"] == "no_commits_yet"

    def test_approve_pr_create_github_error_502(self, client, db_session, owner_auth, owned_project, task_factory):
        from services.github_client import GitHubClientError

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id)
        fake_client = MagicMock()
        fake_client.create_pull_request.side_effect = GitHubClientError("boom", 502)
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": iid, "decision": "approved"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 502

    def test_approve_pr_merge_executes(self, client, db_session, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id, {
            "interaction_type": "pr_merge", "pr_number": 7, "status": "pending_approval",
        })
        fake_client = MagicMock()
        fake_client.get_pull_request.return_value = _pr_data(number=7, state="open", merged=False)
        fake_client.merge_pull_request.return_value = {"sha": "msha", "merged": True}
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": iid, "decision": "approved"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["merged"] is True and data["pr_number"] == 7

    def test_approve_pr_merge_missing_pr_number_400(self, client, db_session, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id, {"interaction_type": "pr_merge", "pr_number": None})
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": iid, "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_approve_unsupported_interaction_type_400(self, client, db_session, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id, {"interaction_type": "pr_rebase"})
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"interaction_id": iid, "decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_approve_unexpected_error_500(self, client, db_session, owner_auth, owned_project, task_factory, monkeypatch):
        from api.project_repo import lifecycle as lifecycle_mod

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        iid = self._make_interaction(db_session, task.id)
        def boom(*a, **k):
            raise RuntimeError("audit down")
        monkeypatch.setattr(lifecycle_mod.AuditLog, "record", boom)
        fake_client = MagicMock()
        fake_client.create_pull_request.return_value = _pr_data(number=12)
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": iid, "decision": "approved"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 500


class TestPendingListBranches:
    """list pending approvals：空队列/成员聚合/异常兜底。"""

    def test_pending_list_empty(self, client, owner_auth, owned_project):
        resp = client.get(f"{BASE_URL}/tasks/pull-request/approvals/pending", headers=owner_auth["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["items"] == []

    def test_pending_list_500_on_user_error(self, client, owner_auth, owned_project, monkeypatch):
        from api.project_repo import lifecycle as lifecycle_mod

        class BadUser:
            @property
            def is_admin(self):
                raise RuntimeError("auth backend down")
        monkeypatch.setattr(lifecycle_mod, "get_current_user", lambda: BadUser())
        resp = client.get(f"{BASE_URL}/tasks/pull-request/approvals/pending", headers=owner_auth["headers"])
        assert resp.status_code == 500


class TestReviewValidation:
    """review 端点：校验与证据落库。"""

    def _bind_and_task(self, client, owner_auth, project, task_factory, title="rev"):
        _bind(client, owner_auth, project)
        return task_factory(project_id=project.id, owner_id=owner_auth["user"].id, title=title)

    def test_review_404_no_repo(self, client, owner_auth, owned_project, task_factory):
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/review",
            json={"decision": "approved", "reviewer_agent_id": 1},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_review_rejects_bad_decision(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/review",
            json={"decision": "meh", "reviewer_agent_id": 1},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_review_success_defaults_pr_from_evidence(self, client, db_session, owner_auth, owned_project, task_factory):
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 21}, created_by="test",
        ))
        db_session.commit()
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/review",
            json={"decision": "approved", "reviewer_agent_id": 3, "summary": "ok"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["pr_number"] == 21 and data["status"] == "passed"

    def test_review_500_on_commit_failure(self, client, owner_auth, owned_project, task_factory, monkeypatch):
        from api.project_repo import lifecycle as lifecycle_mod

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(lifecycle_mod.db.session, "commit", boom)
        try:
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/review",
                json={"decision": "rejected", "reviewer_agent_id": 3},
                headers=owner_auth["headers"],
            )
            assert resp.status_code == 500
        finally:
            monkeypatch.undo()
            lifecycle_mod.db.session.rollback()


class TestMergeValidation:
    """merge 端点：校验与兜底。"""

    def test_merge_404_no_repo(self, client, owner_auth, owned_project, task_factory):
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_merge_404_no_pr(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_details"]["code"] == "NO_PR"

    def test_merge_success(self, client, db_session, owner_auth, owned_project, task_factory):
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 31}, created_by="test",
        ))
        db_session.commit()
        fake_client = MagicMock()
        fake_client.get_pull_request.return_value = _pr_data(number=31, state="open", merged=False)
        fake_client.merge_pull_request.return_value = {"sha": "m1", "merged": True}
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
                json={"merge_method": "squash"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["merged"] is True

    def test_merge_github_error_bubbling_409(self, client, db_session, owner_auth, owned_project, task_factory, monkeypatch):
        from services.github_client import GitHubClientError
        from api.project_repo import lifecycle as lifecycle_mod
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 31}, created_by="test",
        ))
        db_session.commit()
        def boom(*a, **k):
            raise GitHubClientError("escaped", 409)
        monkeypatch.setattr(lifecycle_mod, "_execute_merge", boom)
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={"merge_method": "merge"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 409

    def test_merge_unexpected_error_500(self, client, db_session, owner_auth, owned_project, task_factory, monkeypatch):
        from api.project_repo import lifecycle as lifecycle_mod
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 31}, created_by="test",
        ))
        db_session.commit()
        def boom(*a, **k):
            raise RuntimeError("gate down")
        monkeypatch.setattr(lifecycle_mod, "_execute_merge", boom)
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={"merge_method": "merge"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 500


class TestRemainingBranches:
    """第二轮补齐：validate 分支 / no-workspace L0 / rejected 主体 / list 聚合。"""

    def test_create_404_task_missing(self, client, owner_auth, owned_project):
        _bind(client, owner_auth, owned_project)
        resp = client.post(
            f"{BASE_URL}/tasks/987654/pull-request",
            json={"head_branch": "agent/x"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_create_400_missing_head_branch(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"title": "no branch"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_get_pr_404_task_missing(self, client, owner_auth, owned_project):
        _bind(client, owner_auth, owned_project)
        resp = client.get(f"{BASE_URL}/tasks/987654/pull-request", headers=owner_auth["headers"])
        assert resp.status_code == 404

    def test_l0_without_org_returns_manage_note(self, client, owner_auth, owned_project, task_factory):
        """L0 且项目不在组织内：事件落库为 None → 回退 manage 提示分支。"""
        _bind(client, owner_auth, owned_project, level=0)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        fake_client = MagicMock()
        with patch("api.project_repo._shared.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/x"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["note"]

    def test_review_404_task_missing(self, client, owner_auth, owned_project):
        _bind(client, owner_auth, owned_project)
        resp = client.post(
            f"{BASE_URL}/tasks/987654/pull-request/review",
            json={"decision": "approved", "reviewer_agent_id": 1},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 404

    def test_review_400_missing_fields(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/review",
            json={"decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_merge_400_invalid_json_body(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        from models import TaskEvidenceRecord
        db_session = None  # 证据经端点内查询，不预置
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={"merge_method": "merge"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code in (400, 404)

    def test_merge_400_bad_method_with_pr(self, client, db_session, owner_auth, owned_project, task_factory):
        from models import TaskEvidenceRecord

        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 41}, created_by="test",
        ))
        db_session.commit()
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={"merge_method": "force"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_pending_list_with_paging(self, client, db_session, owner_auth, owned_project, task_factory):
        from datetime import datetime

        from models import AgentTaskEvent

        _bind(client, owner_auth, owned_project, level=0)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        for i in range(2):
            db_session.add(AgentTaskEvent(
                task_id=task.id,
                attempt_id=f"att-{uuid.uuid4().hex[:6]}{i}",
                workspace_id=9999,
                event_type="interaction_request",
                seq=i + 1,
                event_timestamp=datetime.utcnow(),
                payload={
                    "interaction_id": f"i-{uuid.uuid4().hex[:6]}",
                    "interaction_type": "pr_create",
                    "status": "pending_approval",
                },
            ))
        db_session.commit()
        resp = client.get(
            f"{BASE_URL}/tasks/pull-request/approvals/pending?page=1&per_page=10",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert isinstance(resp.get_json()["data"]["items"], list)


class TestValidateTuplesAndListPaging:
    """第三轮：validate tuple 返回行（缺必填字段 → return data）与 list 分页 break。"""

    def test_binding_put_missing_required_fields_returns_tuple(self, client, owner_auth, owned_project):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"token": "x"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400
        assert "Missing required fields" in resp.get_json()["message"]

    def test_approve_missing_interaction_id_returns_tuple(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
            json={"decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_review_missing_reviewer_returns_tuple(self, client, owner_auth, owned_project, task_factory):
        _bind(client, owner_auth, owned_project)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/review",
            json={"decision": "approved"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_pending_list_breaks_at_per_page(self, client, db_session, owner_auth, owned_project, task_factory):
        """pending 事件多于 per_page：items 截断并触发 break。"""
        from datetime import datetime

        from models import AgentTaskEvent

        _bind(client, owner_auth, owned_project, level=0)
        task = task_factory(project_id=owned_project.id, owner_id=owner_auth["user"].id, title="t")
        for i in range(3):
            db_session.add(AgentTaskEvent(
                task_id=task.id,
                attempt_id=f"att-{uuid.uuid4().hex[:6]}{i}",
                workspace_id=9999,
                event_type="interaction_request",
                seq=i + 1,
                event_timestamp=datetime.utcnow(),
                payload={
                    "interaction_id": f"i-{uuid.uuid4().hex[:6]}",
                    "interaction_type": "pr_create",
                    "status": "pending_approval",
                },
            ))
        db_session.commit()
        resp = client.get(
            f"{BASE_URL}/tasks/pull-request/approvals/pending?per_page=1",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        assert len(resp.get_json()["data"]["items"]) == 1


class TestAccessAndListMember:
    """第四轮：can_access_project 403 与非 admin 的 list 聚合分支。"""

    def test_get_pr_403_for_outsider(self, client, db_session, owner_auth, user_factory, project_factory, task_factory):
        """无关用户访问 manage=False 端点 → can_access_project False → 403。"""
        other = user_factory()
        project = project_factory(owner_id=other.id)
        task = task_factory(project_id=project.id, owner_id=other.id, title="outsider")
        resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])
        assert resp.status_code == 403
        assert resp.get_json()["error_details"]["code"] == "PERMISSION_DENIED"

    def test_pending_list_as_plain_member_user(self, client, db_session, owner_auth, user_factory, project_factory, task_factory):
        """非 admin 用户：走 owner/成员聚合分支（232-243）。"""
        from models import ProjectMember, ProjectMemberRole, ProjectMemberStatus

        member = user_factory()
        project_a = project_factory(owner_id=member.id)
        resp = client.get(
            f"{BASE_URL}/tasks/pull-request/approvals/pending",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["items"] == [] and data["pagination"]["page"] == 1

        # owner_auth 作为成员加入 member 的项目后再次查询（union 成员分支）
        db_session.add(ProjectMember(
            project_id=project_a.id,
            user_id=owner_auth["user"].id,
            role=ProjectMemberRole.MAINTAINER,
            status=ProjectMemberStatus.ACTIVE,
        ))
        db_session.commit()
        resp = client.get(
            f"{BASE_URL}/tasks/pull-request/approvals/pending",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200

    def test_pending_list_admin_sees_all_and_member_scoped(self, client, db_session, owner_auth, user_factory, project_factory, task_factory):
        """admin 走全库分支；非 admin 只见自己可管理项目（越权钉子）。"""
        from models import User, UserRole
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        admin = user_factory(role=UserRole.ADMIN)
        admin_token_headers = {"Authorization": "Bearer " + __import__('flask_jwt_extended', fromlist=['create_access_token']).create_access_token(identity=str(admin.id))}
        resp = client.get(
            f"{BASE_URL}/tasks/pull-request/approvals/pending",
            headers=admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["items"] == []
