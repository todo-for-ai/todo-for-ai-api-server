"""Tests for Phase 4 enterprise: SSO config/login skeleton + compliance report."""

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

import pytest

BASE_URL = "/todo-for-ai/api/v1"

# 与 test_github_app.py 保持一致：进程内加密管理器单例复用同一测试密钥
SECRET_ENCRYPTION_KEY = "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30="


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    os.environ["SECRET_ENCRYPTION_KEY"] = SECRET_ENCRYPTION_KEY
    from app import create_app
    from models import db

    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
        "SECRET_KEY": "test-sso-secret",
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


def _seed_event(db_session, ws, event_type, occurred_at, risk=0):
    from models import AgentAuditEvent

    db_session.add(AgentAuditEvent(
        workspace_id=ws, event_type=event_type,
        actor_type="user", actor_id="1", target_type="task", target_id="1",
        risk_score=risk, occurred_at=occurred_at,
    ))


class TestSSOConfig:
    def test_put_get_masks_secret(self, client, owner_auth):
        ws = owner_auth["org"].id
        put = client.put(
            f"{BASE_URL}/workspaces/{ws}/sso/config",
            json={
                "provider": "oidc", "enabled": True,
                "issuer": "https://idp.example.com",
                "client_id": "client-123", "client_secret": "super-secret",
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "userinfo_url": "https://idp.example.com/userinfo",
                "redirect_uri": "https://app.example.com/sso/callback",
            },
            headers=owner_auth["headers"],
        )
        assert put.status_code == 200, put.get_json()
        saved = put.get_json()["data"]["config"]
        assert saved["enabled"] is True
        assert "super-secret" not in (str(saved))
        assert saved["has_client_secret"] is True

        got = client.get(
            f"{BASE_URL}/workspaces/{ws}/sso/config",
            headers=owner_auth["headers"],
        )
        assert got.status_code == 200
        config = got.get_json()["data"]["config"]
        assert config["client_id"] == "client-123"
        assert "client_secret_encrypted" not in config

    def test_invalid_provider_400(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.put(
            f"{BASE_URL}/workspaces/{ws}/sso/config",
            json={"provider": "kerberos"},
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 400


class TestSSOLogin:
    def _configure(self, client, owner_auth):
        ws = owner_auth["org"].id
        resp = client.put(
            f"{BASE_URL}/workspaces/{ws}/sso/config",
            json={
                "provider": "oidc", "enabled": True,
                "client_id": "client-123", "client_secret": "s",
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "userinfo_url": "https://idp.example.com/userinfo",
                "redirect_uri": "https://app.example.com/sso/callback",
            },
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        return ws

    def test_login_returns_signed_authorize_url(self, client, owner_auth):
        from services.sso import verify_oidc_state

        ws = self._configure(client, owner_auth)
        resp = client.post(f"{BASE_URL}/workspaces/{ws}/sso/login")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["authorization_url"].startswith("https://idp.example.com/authorize")
        assert "client_id=client-123" in data["authorization_url"]
        # state 可被服务端校验并还原 workspace
        assert verify_oidc_state(data["state"]) == ws

    def test_saml_login_without_metadata_400(self, client, db_session, owner_auth):
        """SAML 已实现（见 test_saml_login.py）；缺 IdP 元数据地址属配置错误 400。"""
        ws = owner_auth["org"].id
        from models import WorkspaceSSOConfig
        db_session.add(WorkspaceSSOConfig(
            workspace_id=ws, provider="saml", enabled=True,
        ))
        db_session.commit()

        resp = client.post(f"{BASE_URL}/workspaces/{ws}/sso/login")
        assert resp.status_code == 400

    def test_callback_via_injected_client(self, client, owner_auth):
        """回调链路端到端：注入假 IdP 客户端完成 code→JWT 链路。"""
        from services import sso as sso_service

        ws = self._configure(client, owner_auth)
        state = sso_service.build_oidc_state(ws)

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeHTTPClient:
            def post(self, url, data=None):
                return FakeResponse({"access_token": "idp-token"})

            def get(self, url, headers=None):
                return FakeResponse({"email": "someone@example.com", "name": "S"})

        result = sso_service.login_oidc(ws, "one-time", state, http_client=FakeHTTPClient())
        assert result["access_token"]
        from models import User
        user = User.query.filter_by(email="someone@example.com").first()
        assert user is not None

    def test_bad_state_rejected(self, client, owner_auth):
        self._configure(client, owner_auth)
        resp = client.get(f"{BASE_URL}/sso/callback/{owner_auth['org'].id}?code=c&state=tampered")
        assert resp.status_code == 401


class TestComplianceReport:
    def test_report_metrics(self, client, db_session, owner_auth):
        from models import AgentTaskEvent, AgentAuditEvent

        ws = owner_auth["org"].id
        now = datetime.utcnow()
        _seed_event(db_session, ws, "budget.exceeded", now - timedelta(days=1), risk=80)
        _seed_event(db_session, ws, "sso.config_updated", now - timedelta(days=2), risk=20)
        _seed_event(db_session, ws, "audit.exported", now - timedelta(days=3))
        db_session.commit()

        def approval_event(event_type, interaction_id, minutes_ago):
            db_session.add(AgentTaskEvent(
                task_id=1, attempt_id="", agent_id=None, workspace_id=ws,
                event_type=event_type, seq=1,
                event_timestamp=now - timedelta(minutes=minutes_ago),
                payload={"interaction_id": interaction_id},
                message="x", created_by="test",
            ))

        approval_event("interaction_request", "itpr-1", 600)     # 10 小时前请求
        approval_event("interaction_approval", "itpr-1", 60)     # 1 小时前批准
        approval_event("interaction_request", "itpr-2", 30)      # 仍未决
        db_session.commit()

        start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/compliance/report?start_date={start}",
            headers=owner_auth["headers"],
        )
        assert resp.status_code == 200
        report = resp.get_json()["data"]

        assert report["risk_events"]["total"] == 1  # risk=80 那条
        assert report["budget_overshoots"] == 1
        assert report["audit_exports"] == 1
        approvals = report["approvals"]
        assert approvals["requested"] == 2
        assert approvals["decided"] == 1
        assert approvals["pending"] == 1
        assert approvals["max_dwell_seconds"] == pytest.approx(540 * 60, rel=0.01)

        # 报告生成写审计
        generated = AgentAuditEvent.query.filter_by(
            workspace_id=ws, event_type="compliance.report_generated",
        ).all()
        assert len(generated) == 1

    def test_requires_manage(self, client, db_session, _isolated_app, owner_auth, user_factory):
        import uuid as _uuid
        from flask_jwt_extended import create_access_token
        from models import User
        from werkzeug.security import generate_password_hash

        ws = owner_auth["org"].id
        outsider = User(username=f"out_{str(_uuid.uuid4())[:6]}", email=f"out_{str(_uuid.uuid4())[:6]}@x.com")
        outsider.password_hash = generate_password_hash("password123")
        db_session.add(outsider)
        db_session.commit()
        with _isolated_app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.get(
            f"{BASE_URL}/workspaces/{ws}/compliance/report",
            headers=headers,
        )
        assert resp.status_code == 403
