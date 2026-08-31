"""Tests for Phase 4 private-deploy enhancements (deploy check endpoint + service)."""

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
def admin_auth(_isolated_app, db_session):
    import uuid as _uuid
    from flask_jwt_extended import create_access_token
    from models import User, UserRole
    from werkzeug.security import generate_password_hash

    unique_id = str(_uuid.uuid4())[:8]
    admin = User(username=f"admin_{unique_id}", email=f"admin_{unique_id}@example.com",
                 role=UserRole.ADMIN)
    admin.password_hash = generate_password_hash("password123")
    db_session.add(admin)
    db_session.commit()

    with _isolated_app.app_context():
        token = create_access_token(identity=str(admin.id))
    return {"user": admin, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def normal_auth(_isolated_app, db_session):
    import uuid as _uuid
    from flask_jwt_extended import create_access_token
    from models import User
    from werkzeug.security import generate_password_hash

    unique_id = str(_uuid.uuid4())[:8]
    user = User(username=f"norm_{unique_id}", email=f"norm_{unique_id}@example.com")
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    with _isolated_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


class TestDeployCheckService:
    def test_all_checks_pass_in_clean_env(self, _isolated_app, db_session):
        import os

        from services.deploy_check import run_deploy_checks

        os.environ.setdefault("SECRET_KEY", "test-secret-key-value-32chars")
        os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32chars")
        os.environ.setdefault("SECRET_ENCRYPTION_KEY", "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30=")

        report = run_deploy_checks()
        assert report["ok"] is True
        assert report["summary"]["error"] == 0
        categories = {check["category"] for check in report["checks"]}
        assert {"version", "env", "database", "migration"} <= categories

        migration_checks = [c for c in report["checks"] if c["category"] == "migration"]
        assert migration_checks
        assert all(c["status"] == "pass" for c in migration_checks)

    def test_missing_required_env_reports_error(self, _isolated_app, db_session, monkeypatch):
        from services.deploy_check import run_deploy_checks

        for key in ("SECRET_KEY", "JWT_SECRET_KEY", "SECRET_ENCRYPTION_KEY"):
            monkeypatch.delenv(key, raising=False)
        _isolated_app.config.pop("SECRET_KEY", None)
        _isolated_app.config.pop("JWT_SECRET_KEY", None)

        report = run_deploy_checks()
        assert report["ok"] is False
        assert "env.SECRET_KEY" in report["errors"]
        assert "env.JWT_SECRET_KEY" in report["errors"]

    def test_placeholder_env_reports_error(self, _isolated_app, db_session, monkeypatch):
        from services.deploy_check import run_deploy_checks

        monkeypatch.setenv("SECRET_KEY", "your_secret_key_32_chars_or_more_here")
        report = run_deploy_checks()
        assert "env.SECRET_KEY" in report["errors"]


class TestDeployCheckEndpoint:
    def test_admin_can_fetch_report(self, client, admin_auth):
        import os
        os.environ.setdefault("SECRET_ENCRYPTION_KEY",
                              "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30=")

        resp = client.get(f"{BASE_URL}/system/deploy/check", headers=admin_auth["headers"])
        assert resp.status_code == 200
        report = resp.get_json()["data"]
        assert report["ok"] is True
        assert report["schema_version"] >= 13

    def test_non_admin_forbidden(self, client, normal_auth):
        resp = client.get(f"{BASE_URL}/system/deploy/check", headers=normal_auth["headers"])
        assert resp.status_code == 403
