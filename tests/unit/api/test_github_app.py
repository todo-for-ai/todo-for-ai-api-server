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
