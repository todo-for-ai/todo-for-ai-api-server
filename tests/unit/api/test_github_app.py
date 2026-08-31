"""Tests for GitHub App integration (webhook HMAC, event sync, manifest flow)."""

import hashlib
import hmac as hmac_mod
import json
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))

import pytest

BASE_URL = "/todo-for-ai/api/v1"
WEBHOOK_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def app_configured(db_session, monkeypatch):
    """写入 webhook secret 环境变量（App 配置表未配置时的回退路径）。"""
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", WEBHOOK_SECRET)


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


def _webhook_post(client, payload: dict, secret: str = WEBHOOK_SECRET, *, sign=True, event="pull_request"):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return client.post(f"{BASE_URL}/github/app/webhook", data=body, headers=headers)


class TestSignatureVerification:
    def test_valid_signature_accepted(self, client, app_configured):
        resp = _webhook_post(client, {"zen": "hello"}, event="ping")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is True

    def test_invalid_signature_rejected(self, client, app_configured):
        resp = _webhook_post(client, {"zen": "hello"}, secret="wrong", event="ping")
        assert resp.status_code == 401

    def test_missing_signature_rejected(self, client, app_configured):
        resp = client.post(
            f"{BASE_URL}/github/app/webhook",
            data=json.dumps({"zen": "hi"}).encode(),
            headers={"Content-Type": "application/json", "X-GitHub-Event": "ping"},
        )
        assert resp.status_code == 401

    def test_unconfigured_secret_fails_closed(self, client, db_session, monkeypatch):
        monkeypatch.delenv("GITHUB_APP_WEBHOOK_SECRET", raising=False)
        resp = _webhook_post(client, {"zen": "hi"}, event="ping")
        assert resp.status_code == 503

    def test_unit_verify_function(self):
        from services.github_app import verify_webhook_signature

        body = b'{"a": 1}'
        good = _sign(body)
        assert verify_webhook_signature(body, good, WEBHOOK_SECRET) is True
        assert verify_webhook_signature(body, good, "other") is False
        assert verify_webhook_signature(b'{"a": 2}', good, WEBHOOK_SECRET) is False
        assert verify_webhook_signature(body, None, WEBHOOK_SECRET) is False
        assert verify_webhook_signature(body, "md5=abc", WEBHOOK_SECRET) is False


class TestWebhookEvents:
    def _seed_task_with_pr(self, db_session, project_factory, task_factory):
        from models import TaskEvidenceRecord

        project = project_factory()
        task = task_factory(project_id=project.id, owner_id=project.owner_id, title="PR webhook task")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown",
            detail={"pr_number": 77, "repo": "acme/widget"}, created_by="test",
        ))
        db_session.commit()
        db_session.expire(task)
        return task

    def test_pr_merged_event_completes_task(self, client, db_session, app_configured, project_factory, task_factory):
        task = self._seed_task_with_pr(db_session, project_factory, task_factory)

        payload = {
            "action": "closed",
            "pull_request": {
                "number": 77,
                "state": "closed",
                "merged": True,
                "title": "Merge me",
                "html_url": "https://github.com/acme/widget/pull/77",
                "head": {"ref": "agent/x"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "acme/widget"},
        }
        resp = _webhook_post(client, payload)

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["handled"] is True and data["task_id"] == task.id

        db_session.expire(task)
        assert task.status.value == "done"
        assert task.completion_rate == 100

    def test_pr_opened_event_updates_evidence_only(self, client, db_session, app_configured, project_factory, task_factory):
        from models import TaskEvidenceRecord

        task = self._seed_task_with_pr(db_session, project_factory, task_factory)

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 77, "state": "open", "merged": False, "title": "WIP",
                "html_url": "https://github.com/acme/widget/pull/77",
                "head": {"ref": "agent/x"}, "base": {"ref": "main"},
            },
            "repository": {"full_name": "acme/widget"},
        }
        resp = _webhook_post(client, payload)

        assert resp.status_code == 200
        db_session.expire(task)
        assert task.status.value != "done"

    def test_event_without_matching_task_is_ignored(self, client, app_configured):
        payload = {
            "action": "closed",
            "pull_request": {"number": 99999, "state": "closed", "merged": True},
            "repository": {"full_name": "nobody/nothing"},
        }
        resp = _webhook_post(client, payload)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is False

    def test_unknown_event_ignored(self, client, app_configured):
        resp = _webhook_post(client, {"x": 1}, event="fork")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is False


class TestIssueAndCIEvents:
    """P2.5 事件面扩容：issues.opened 建任务、workflow_run 结论入 outbox。"""

    @pytest.fixture
    def binding_factory(self, db_session):
        """创建 ProjectRepoBinding 并在 teardown 时删除（SQLite rowid 复用会撞唯一约束）。"""
        created = []

        def _create(project, repo_full_name="acme/widget"):
            from models import ProjectRepoBinding

            owner, name = repo_full_name.split("/", 1)
            binding = ProjectRepoBinding(
                project_id=project.id, repo_owner=owner, repo_name=name,
            )
            db_session.add(binding)
            db_session.commit()
            created.append(binding)
            return binding

        yield _create
        for binding in created:
            db_session.delete(binding)
        db_session.commit()

    def test_issue_opened_creates_task_and_outbox(self, client, db_session, app_configured, project_factory, task_factory, binding_factory):
        from models import Task, TaskEventOutbox

        project = project_factory()
        binding_factory(project)

        payload = {
            "action": "opened",
            "issue": {
                "number": 42,
                "title": "Fix login crash",
                "body": "Login crashes on empty password.",
                "html_url": "https://github.com/acme/widget/issues/42",
                "labels": [{"name": "bug"}, {"name": "P1"}],
            },
            "repository": {"full_name": "acme/widget"},
        }
        resp = _webhook_post(client, payload, event="issues")

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["handled"] is True

        task = db_session.get(Task, data["task_id"])
        assert task is not None and task.project_id == project.id
        assert task.title.startswith("[issue #42]")
        assert "Login crashes on empty password." in (task.content or "")
        assert task.status.value == "todo"

        outbox = (
            TaskEventOutbox.query
            .filter_by(event_type="repo.issues.opened", task_id=task.id)
            .all()
        )
        assert len(outbox) == 1
        assert outbox[0].payload.get("issue_number") == 42

        # 清理 webhook 建的任务：project_factory teardown 删项目时不能被 tasks FK 挡住
        TaskEventOutbox.query.filter_by(task_id=task.id).delete()
        Task.query.filter_by(id=task.id).delete()
        db_session.commit()

    def test_issue_closed_is_ignored(self, client, db_session, app_configured, project_factory, task_factory, binding_factory):
        from models import Task

        project = project_factory()
        binding_factory(project)
        before = Task.query.count()

        payload = {
            "action": "closed",
            "issue": {"number": 42, "title": "Done already"},
            "repository": {"full_name": "acme/widget"},
        }
        resp = _webhook_post(client, payload, event="issues")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is False
        assert Task.query.count() == before

    def test_issue_without_binding_creates_nothing(self, client, db_session, app_configured):
        from models import Task

        before = Task.query.count()
        payload = {
            "action": "opened",
            "issue": {"number": 7, "title": "Orphan"},
            "repository": {"full_name": "nobody/nothing"},
        }
        resp = _webhook_post(client, payload, event="issues")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is False
        assert Task.query.count() == before

    def _seed_task_with_pr_branch(self, db_session, project_factory, task_factory,
                                  repo_full_name="acme/widget", branch="agent/fix-1"):
        from models import TaskEvidenceRecord

        project = project_factory()
        task = task_factory(project_id=project.id, owner_id=project.owner_id, title="CI task")
        db_session.add(TaskEvidenceRecord(
            task_id=task.id, evidence_type="pr", status="unknown",
            detail={"pr_number": 9, "repo": repo_full_name, "head_branch": branch},
            created_by="test",
        ))
        db_session.commit()
        db_session.expire(task)
        return task

    def test_workflow_run_failure_emits_repo_event(self, client, db_session, app_configured, project_factory, task_factory):
        from models import TaskEventOutbox

        task = self._seed_task_with_pr_branch(db_session, project_factory, task_factory)

        payload = {
            "action": "completed",
            "workflow_run": {
                "run_number": 12,
                "conclusion": "failure",
                "head_branch": "agent/fix-1",
                "html_url": "https://github.com/acme/widget/actions/runs/9001",
                "workflow": {"name": "CI"},
            },
            "repository": {"full_name": "acme/widget"},
        }
        resp = _webhook_post(client, payload, event="workflow_run")

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["handled"] is True and data["task_id"] == task.id

        outbox = (
            TaskEventOutbox.query
            .filter_by(event_type="repo.workflow_run.failure", task_id=task.id)
            .all()
        )
        assert len(outbox) == 1
        assert outbox[0].payload.get("conclusion") == "failure"

    def test_workflow_run_no_matching_task_ignored(self, client, db_session, app_configured, project_factory, task_factory):
        self._seed_task_with_pr_branch(db_session, project_factory, task_factory, branch="other/branch")
        payload = {
            "action": "completed",
            "workflow_run": {"conclusion": "failure", "head_branch": "unknown/branch"},
            "repository": {"full_name": "acme/widget"},
        }
        resp = _webhook_post(client, payload, event="workflow_run")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["handled"] is False


class TestManifestFlow:
    def test_manifest_generated(self, client, owner_auth, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PUBLIC_BASE", "https://todo4ai.local")
        resp = client.get(f"{BASE_URL}/github/app/manifest", headers=owner_auth["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        manifest = data["manifest"]
        assert manifest["hook_attributes"]["url"].endswith("/github/app/webhook")
        assert manifest["redirect_url"].endswith("/github/app/callback")
        assert "pull_request" in manifest["default_events"]
        assert manifest["default_permissions"]["contents"] == "write"
        assert "todo4ai.local" in data["target_url"] or "github.com" in data["target_url"]

    def test_callback_exchanges_code_and_stores(self, client, db_session, monkeypatch):
        from models import GitHubAppConfig

        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30=")

        conversion = {
            "id": 12345,
            "slug": "todo-for-ai",
            "pem": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
            "webhook_secret": WEBHOOK_SECRET,
        }
        with patch("api.github_app.exchange_manifest_code", return_value=conversion):
            resp = client.get(f"{BASE_URL}/github/app/callback?code=one-time-code")

        assert resp.status_code == 200, resp.get_json()
        row = GitHubAppConfig.query.filter_by(id=1).first()
        assert row is not None
        assert row.app_id == "12345"
        assert row.slug == "todo-for-ai"
        assert row.private_key_encrypted and row.private_key_encrypted != conversion["pem"]

    def test_callback_requires_code(self, client):
        resp = client.get(f"{BASE_URL}/github/app/callback")
        assert resp.status_code == 400

    def test_status_endpoint(self, client, db_session, owner_auth, monkeypatch):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30=")
        client.get(f"{BASE_URL}/github/app/callback?code=c")  # 未 mock 会 502；此处只验证 404 分支
        # 未配置时
        resp = client.get(f"{BASE_URL}/github/app/status", headers=owner_auth["headers"])
        assert resp.status_code in (200, 404)


class TestAppService:
    def test_app_jwt_structure(self):
        """用本地生成的 RSA key 验证 App JWT 的三段结构与 header。"""
        import base64
        import json as _json

        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from services.github_app import generate_app_jwt

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        token = generate_app_jwt("999", pem)
        header_b64, payload_b64, sig_b64 = token.split(".")

        def _dec(seg):
            return _json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))

        header, payload = _dec(header_b64), _dec(payload_b64)
        assert header["alg"] == "RS256"
        assert payload["iss"] == "999"
        assert payload["exp"] - payload["iat"] == 660
        assert len(sig_b64) > 32

    def test_app_jwt_rejects_bad_key(self):
        from services.github_app import generate_app_jwt, GitHubAppError

        with pytest.raises(GitHubAppError):
            generate_app_jwt("999", "not-a-key")


class TestInstallationTokenExecution:
    """installation token 接入 repo 操作执行面（resolve_token 优先级链 + 缓存）。"""

    def _seed_app(self, db_session, monkeypatch, *, installed=True):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30=")
        from services.github_app import upsert_app_config, clear_installation_token_cache
        upsert_app_config({
            "app_id": "999",
            "slug": "todo-for-ai",
            "installation_id": "42",
            "private_key": "-----BEGIN PRIVATE KEY-----\nseed\n-----END PRIVATE KEY-----\n",
            "webhook_secret": WEBHOOK_SECRET,
            "installed": installed,
        })
        clear_installation_token_cache()
        yield
        from services.github_app import clear_installation_token_cache as _clear
        _clear()

    def test_resolve_prefers_app_installation_token(self, db_session, monkeypatch, project_factory):
        from models import ProjectRepoBinding
        from services.github_client import resolve_token
        from services import github_app

        list(self._seed_app(db_session, monkeypatch))

        binding = ProjectRepoBinding(project_id=1, repo_owner="acme", repo_name="widget")
        binding.token_encrypted = None
        monkeypatch.setenv("GITHUB_TOKEN", "env-fallback")
        monkeypatch.setattr(
            github_app, "get_cached_installation_token_for_config",
            lambda: "app-install-token",
        )

        assert resolve_token(binding) == "app-install-token"
        assert resolve_token(binding, prefer_app=False) == "env-fallback"

    def test_resolve_falls_back_when_app_not_installed(self, db_session, monkeypatch):
        from models import ProjectRepoBinding
        from services.github_client import resolve_token

        list(self._seed_app(db_session, monkeypatch, installed=False))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        binding = ProjectRepoBinding(project_id=1, repo_owner="acme", repo_name="widget")
        assert resolve_token(binding) is None  # 无静态凭证、App 未安装 → None

    def test_resolve_falls_back_to_binding_token(self, db_session, monkeypatch, project_factory):
        from models import ProjectRepoBinding
        from services.github_client import resolve_token
        from services.github_app import encrypt_str

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        binding = ProjectRepoBinding(
            project_id=1, repo_owner="acme", repo_name="widget",
            token_encrypted=encrypt_str("binding-token"),
        )

        assert resolve_token(binding) == "binding-token"

    def test_installation_token_cached_until_expiry(self, db_session, monkeypatch):
        """同一 App/安装的 token 进程内缓存：第二次调用不触发刷新请求。"""
        from services import github_app
        from services.github_app import get_cached_installation_token, clear_installation_token_cache

        calls = []
        monkeypatch.setattr(
            github_app, "_request_installation_token",
            lambda app_id, key, inst, timeout=15: (calls.append(1) or (f"tok-{len(calls)}", 9999999999)),
        )
        clear_installation_token_cache()

        assert get_cached_installation_token("999", "key", "42") == "tok-1"
        assert get_cached_installation_token("999", "key", "42") == "tok-1"  # 缓存命中
        assert len(calls) == 1
        clear_installation_token_cache()

    def test_installation_token_refreshes_near_expiry(self, db_session, monkeypatch):
        """过期余量（5 分钟）内触发刷新。"""
        import time as time_mod
        from services import github_app
        from services.github_app import get_cached_installation_token, clear_installation_token_cache

        calls = []

        def fake_request(app_id, key, inst, timeout=15):
            calls.append(1)
            # 60s < 5min 刷新余量 → 每次都视为临期
            return f"tok-{len(calls)}", time_mod.time() + 60

        monkeypatch.setattr(github_app, "_request_installation_token", fake_request)

        assert get_cached_installation_token("777", "key", "42") == "tok-1"
        assert get_cached_installation_token("777", "key", "42") == "tok-2"  # 临期刷新
        assert len(calls) == 2
        clear_installation_token_cache()

    def test_app_error_on_incomplete_config(self, db_session, monkeypatch):
        from services.github_app import (
            GitHubAppError, get_cached_installation_token_for_config, clear_installation_token_cache,
        )

        clear_installation_token_cache()
        with pytest.raises(GitHubAppError):
            get_cached_installation_token_for_config()
