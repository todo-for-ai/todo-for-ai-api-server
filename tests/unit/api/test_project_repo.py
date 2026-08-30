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
    def _bind(self, client, owner_auth, owned_project, autonomy_level=1):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "autonomy_level": autonomy_level},
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


class TestTaskPullRequestMerge:
    """人工审批动作：合并 PR（P1.5 MVP）。"""

    def _setup_pr_task(self, client, db_session, owner_auth, owned_project, project_factory, task_factory):
        from models import TaskEvidenceRecord

        client.put(
            f"{BASE_URL}/projects/{owned_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "autonomy_level": 1},
            headers=owner_auth["headers"],
        )
        task = task_factory(
            project_id=owned_project.id,
            owner_id=owner_auth["user"].id,
            title="Merge approval task",
            is_ai_task=True,
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown",
            summary="PR #9", detail={"pr_number": 9}, created_by="test",
        ))
        db_session.commit()
        db_session.expire(task)
        return task

    def test_merge_success_completes_task_and_audits(
        self, client, db_session, owner_auth, owned_project, project_factory, task_factory
    ):
        from models import AuditLog, Task

        task = self._setup_pr_task(client, db_session, owner_auth, owned_project, project_factory, task_factory)

        fake_client = MagicMock()
        fake_client.merge_pull_request.return_value = {"sha": "mergedsha1", "merged": True}
        fake_client.get_pull_request.return_value = _pr_data(number=9, state="closed", merged=True)

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
                json={"merge_method": "squash"},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["merged"] is True
        assert data["task_status"] == "done"

        db_session.expire(task)
        assert task.status.value == "done"
        audit = AuditLog.query.filter_by(action="task.pr_merged", resource_id=task.id).first()
        assert audit is not None
        assert audit.detail["merge_method"] == "squash"

    def test_merge_requires_manage_permission(
        self, client, db_session, owner_auth, user_factory, owned_project, project_factory, task_factory
    ):
        from models import TaskEvidenceRecord

        project = project_factory(owner_id=user_factory().id)
        task = task_factory(project_id=project.id, title="Not mine")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 3}, created_by="test",
        ))
        db_session.commit()

        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 403

    def test_merge_invalid_method_rejected(
        self, client, db_session, owner_auth, owned_project, project_factory, task_factory
    ):
        task = self._setup_pr_task(client, db_session, owner_auth, owned_project, project_factory, task_factory)
        resp = client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request/merge",
            json={"merge_method": "force-push"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400


class TestAutonomyLevels:
    """自主等级 L0-L2：渐进审批（Phase 2 事件化审批队列）。"""

    @pytest.fixture
    def owned_org_project(self, db_session, owner_auth, project_factory):
        """带组织的项目（workspace 审批事件要求 organization_id）。"""
        from models import Project
        project = project_factory(owner_id=owner_auth["user"].id)
        project.organization_id = 9999  # 事件 workspace_id 引用（测试库无 FK 校验语义，直填值）
        db_session.add(project)
        db_session.commit()
        return project

    def _bind(self, client, owner_auth, project, level):
        resp = client.put(
            f"{BASE_URL}/projects/{project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "autonomy_level": level},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200

    def test_bind_rejects_invalid_level(self, client, owner_auth, owned_org_project):
        resp = client.put(
            f"{BASE_URL}/projects/{owned_org_project.id}/repo",
            json={"repo_owner": "a", "repo_name": "b", "autonomy_level": 5},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400

    def test_autonomy_level_roundtrip(self, client, owner_auth, owned_org_project):
        self._bind(client, owner_auth, owned_org_project, 2)
        resp = client.get(f"{BASE_URL}/projects/{owned_org_project.id}/repo", headers=owner_auth["headers"])
        assert resp.status_code == 200

    def test_l0_pr_create_queued_for_approval(self, client, db_session, owner_auth, owned_org_project, task_factory):
        """L0：PR 创建不入 GitHub，入 interaction 审批队列。"""
        from models import AgentTaskEvent

        self._bind(client, owner_auth, owned_org_project, 0)
        task = task_factory(project_id=owned_org_project.id, owner_id=owner_auth["user"].id, title="L0 task")

        fake_client = MagicMock()
        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request",
                json={"head_branch": "agent/x"},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["pr_created"] is False
        assert data["reason"] == "approval_required"
        assert data["interaction_id"]
        fake_client.create_pull_request.assert_not_called()

        event = AgentTaskEvent.query.filter_by(task_id=task.id, event_type="interaction_request").first()
        assert event is not None
        assert event.payload["interaction_type"] == "pr_create"
        assert event.payload["governance"]["requires_approval"] is True

    def test_l0_approve_executes_pr_creation(self, client, db_session, owner_auth, owned_org_project, task_factory):
        from models import AgentTaskEvent

        self._bind(client, owner_auth, owned_org_project, 0)
        task = task_factory(project_id=owned_org_project.id, owner_id=owner_auth["user"].id, title="L0 approve")
        client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"head_branch": "agent/x"},
            headers=owner_auth["headers"],
        )
        interaction = AgentTaskEvent.query.filter_by(
            task_id=task.id, event_type="interaction_request"
        ).first()
        interaction_id = interaction.payload["interaction_id"]

        fake_client = MagicMock()
        fake_client.create_pull_request.return_value = _pr_data(number=42)

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": interaction_id, "decision": "approved"},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["executed"] is True and data["pr_number"] == 42

        approval = AgentTaskEvent.query.filter_by(
            task_id=task.id, event_type="interaction_approval"
        ).first()
        assert approval is not None
        assert approval.payload["decision"] == "approved"

    def test_l0_reject_records_without_github(self, client, db_session, owner_auth, owned_org_project, task_factory):
        from models import AgentTaskEvent

        self._bind(client, owner_auth, owned_org_project, 0)
        task = task_factory(project_id=owned_org_project.id, owner_id=owner_auth["user"].id, title="L0 reject")
        client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"head_branch": "agent/x"},
            headers=owner_auth["headers"],
        )
        interaction = AgentTaskEvent.query.filter_by(
            task_id=task.id, event_type="interaction_request"
        ).first()
        interaction_id = interaction.payload["interaction_id"]

        fake_client = MagicMock()
        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": interaction_id, "decision": "rejected", "reason": "not ready"},
                headers=owner_auth["headers"],
            )

        assert resp.status_code == 200
        assert resp.get_json()["data"]["executed"] is False
        fake_client.create_pull_request.assert_not_called()

    def test_l2_auto_merge_when_evidence_all_passed(
        self, client, db_session, owner_auth, owned_org_project, project_factory, task_factory
    ):
        from models import Task, TaskEvidenceRecord

        self._bind(client, owner_auth, owned_org_project, 2)
        task = task_factory(
            project_id=owned_org_project.id,
            owner_id=owner_auth["user"].id,
            title="L2 auto merge",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}],
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 7}, created_by="test",
        ))
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="test", status="passed", summary="all green", created_by="agent:1",
        ))
        db_session.commit()
        db_session.expire(task)

        fake_client = MagicMock()
        fake_client.get_pull_request.return_value = _pr_data(number=7, state="open", merged=False)
        fake_client.merge_pull_request.return_value = {"sha": "autoshal", "merged": True}

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["auto_merged"]["merged"] is True
        assert data["task_status"] == "done"

        db_session.expire(task)
        assert task.status.value == "done"
        fake_client.merge_pull_request.assert_called_once()

    def test_l2_no_auto_merge_when_evidence_missing(
        self, client, db_session, owner_auth, owned_org_project, project_factory, task_factory
    ):
        from models import TaskEvidenceRecord

        self._bind(client, owner_auth, owned_org_project, 2)
        task = task_factory(
            project_id=owned_org_project.id,
            owner_id=owner_auth["user"].id,
            title="L2 missing evidence",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}],
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 8}, created_by="test",
        ))
        db_session.commit()

        fake_client = MagicMock()
        fake_client.get_pull_request.return_value = _pr_data(number=8, state="open", merged=False)

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["auto_merged"]["merged"] is False
        assert data["auto_merged"]["reason"] == "evidence_not_all_passed"
        assert data["task_status"] != "done"
        fake_client.merge_pull_request.assert_not_called()

    def test_l1_sync_does_not_auto_merge(
        self, client, db_session, owner_auth, owned_org_project, project_factory, task_factory
    ):
        from models import TaskEvidenceRecord

        self._bind(client, owner_auth, owned_org_project, 1)
        task = task_factory(
            project_id=owned_org_project.id,
            owner_id=owner_auth["user"].id,
            title="L1 manual merge",
            is_ai_task=True,
            dod=[{"type": "test", "value": "pytest -q"}],
        )
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown", detail={"pr_number": 9}, created_by="test",
        ))
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="test", status="passed", created_by="agent:1",
        ))
        db_session.commit()

        fake_client = MagicMock()
        fake_client.get_pull_request.return_value = _pr_data(number=9, state="open", merged=False)

        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.get(f"{BASE_URL}/tasks/{task.id}/pull-request", headers=owner_auth["headers"])

        data = resp.get_json()["data"]
        assert data["auto_merged"] is None
        assert data["task_status"] != "done"
        fake_client.merge_pull_request.assert_not_called()


class TestPendingPrApprovalsList:
    """L0 审批队列前端数据源：pending 列表端点。"""

    @pytest.fixture
    def owned_org_project(self, db_session, owner_auth, project_factory):
        from models import Project
        project = project_factory(owner_id=owner_auth["user"].id)
        project.organization_id = 9999
        db_session.add(project)
        db_session.commit()
        return project

    def test_pending_list_and_decide_flow(self, client, db_session, owner_auth, owned_org_project, task_factory):
        from models import AgentTaskEvent

        client.put(
            f"{BASE_URL}/projects/{owned_org_project.id}/repo",
            json={"repo_owner": "acme", "repo_name": "widget", "autonomy_level": 0},
            headers=owner_auth["headers"],
        )
        task = task_factory(project_id=owned_org_project.id, owner_id=owner_auth["user"].id, title="Queue me")

        # 记录创建前已有的 pending 交互（task_factory 手动 id 会在测试间重复，用差集定位本次）
        def _pending_ids():
            r = client.get(f"{BASE_URL}/tasks/pull-request/approvals/pending", headers=owner_auth["headers"])
            return {i["interaction_id"]: i for i in r.get_json()["data"]["items"]}

        before = _pending_ids()

        # L0 创建 → 生成 pending 请求
        client.post(
            f"{BASE_URL}/tasks/{task.id}/pull-request",
            json={"head_branch": "agent/q"},
            headers=owner_auth["headers"],
        )

        after = _pending_ids()
        new_ids = set(after) - set(before)
        assert len(new_ids) == 1
        interaction_id = new_ids.pop()
        mine = after[interaction_id]
        assert mine["interaction_type"] == "pr_create"
        assert mine["head_branch"] == "agent/q"
        fake_client = MagicMock()
        fake_client.create_pull_request.return_value = _pr_data(number=55)
        with patch("api.project_repo.GitHubClient", return_value=fake_client):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/pull-request/approve",
                json={"interaction_id": interaction_id, "decision": "approved"},
                headers=owner_auth["headers"],
            )
        assert resp.status_code == 200

        after_approval = _pending_ids()
        assert interaction_id not in after_approval
