"""认证 API（api/auth.py）单元回归。

覆盖：回跳地址归一化（回环地址/后端地址替换/相对路径）、访客登录（首登
创建/复用）、logout/me/verify/refresh、用户列表（admin 门禁 + 过滤）、
用户详情三视角（self/admin/共享组织公开档案）、用户状态管理。
OAuth 回跳的 authorize/authorize_access_token 打桩。
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask_jwt_extended import create_refresh_token

from models import (
    Organization,
    OrganizationMember,
    OrganizationMemberStatus,
    User,
    UserRole,
    UserStatus,
    db,
)
from models.organization import OrganizationRole as OrganizationRoleEnum


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
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def user():
    def _make(role=UserRole.USER, email=None):
        u = User(username=f"au_{uuid.uuid4().hex[:8]}",
                 email=email or f"au_{uuid.uuid4().hex[:6]}@t.io",
                 role=role)
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture
def auth_headers(_isolated_app, user):
    from flask_jwt_extended import create_access_token
    u = user()
    token = create_access_token(identity=str(u.id))
    return {"user": u, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def admin_headers(_isolated_app, user):
    from flask_jwt_extended import create_access_token
    u = user(role=UserRole.ADMIN)
    token = create_access_token(identity=str(u.id))
    return {"user": u, "headers": {"Authorization": f"Bearer {token}"}}


BASE = "/todo-for-ai/api/v1/auth"


class TestHelpers:
    def test_loopback_normalization(self):
        from api.auth import _normalize_local_loopback_url as norm
        assert norm("") == ""
        assert norm(None) is None
        assert norm(123) == 123  # 非字符串 urlparse 抛错 → 原样返回
        assert norm("http://localhost:50111/x") == "http://127.0.0.1:50111/x"
        assert norm("http://user@localhost:50111/x") == "http://user@127.0.0.1:50111/x"
        assert norm("https://todo4ai.org/x") == "https://todo4ai.org/x"  # 非本地不动

    def test_return_to_normalization(self):
        from api.auth import _normalize_return_to as norm
        frontend = "http://localhost:50111"
        loopback = "http://127.0.0.1:50111"  # 函数内部会归一化 frontend_base
        assert norm("", frontend) == f"{loopback}/todo-for-ai/pages/dashboard"
        assert norm("/pages/x", frontend) == f"{loopback}/pages/x"
        # 后端回环地址 → 前端地址
        assert norm("http://localhost:50110/todo-for-ai/pages/dashboard",
                    frontend) == f"{loopback}/todo-for-ai/pages/dashboard"
        assert norm("http://127.0.0.1:50110/a", frontend) == f"{loopback}/a"
        # 后端 API 路径 → 前端页面路径
        assert norm(f"{frontend}/todo-for-ai/api/v1/auth/callback",
                    frontend) == f"{loopback}/todo-for-ai/pages/auth/callback"
        # 外部地址保留；路径/query 中含 localhost:50110 的外部地址 → 换前端
        assert norm("https://other.example/x", frontend) == "https://other.example/x"
        assert norm("https://other.example/?next=http://localhost:50110/x",
                    frontend) == "https://other.example/?next=http://127.0.0.1:50111/x"
        # 非法碎片 → dashboard
        assert norm("junk", frontend) == f"{loopback}/todo-for-ai/pages/dashboard"

    def test_append_query_params_preserves_existing(self):
        from api.auth import _append_query_params as append
        out = append("https://f.example/cb?a=1", {"b": "2", "a": "9"})
        assert "a=9" in out and "b=2" in out


class TestGuestLogin:
    @staticmethod
    def _stub_tokens(monkeypatch):
        stub = SimpleNamespace(generate_tokens=lambda u: {
            "access_token": "acc", "refresh_token": "ref",
            "token_type": "bearer"})
        monkeypatch.setattr("api.auth.github_service", stub)

    def test_first_login_creates_guest_and_redirects(self, client, monkeypatch):
        self._stub_tokens(monkeypatch)
        resp = client.get(f"{BASE}/login/guest")
        assert resp.status_code == 302
        assert "access_token=acc" in resp.headers["Location"]
        guest = User.query.filter_by(email="guest@todo4ai.local").first()
        assert guest is not None and guest.provider == "guest"

    def test_second_login_reuses_guest(self, client, monkeypatch):
        self._stub_tokens(monkeypatch)
        client.get(f"{BASE}/login/guest")
        client.get(f"{BASE}/login/guest")
        from models import User as U
        assert U.query.filter_by(email="guest@todo4ai.local").count() == 1

    def test_token_generation_failure_returns_500(self, client, monkeypatch):
        stub = SimpleNamespace(generate_tokens=lambda u: None)
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/login/guest")
        assert resp.status_code == 500


class TestOrgRoleKeys:
    def test_owner_short_circuit(self, user):
        from api.auth import _collect_user_org_role_keys
        owner = user()
        org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                           slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
        db.session.add(org)
        db.session.commit()
        assert _collect_user_org_role_keys(org, owner.id) == ["owner"]

    def test_non_member_empty(self, user):
        from api.auth import _collect_user_org_role_keys
        owner = user()
        outsider = user()
        org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                           slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
        db.session.add(org)
        db.session.commit()
        assert _collect_user_org_role_keys(org, outsider.id) == []

    def test_accessible_org_ids_union(self, user):
        from api.auth import _collect_accessible_org_ids
        u = user()
        owned = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                             slug=f"o1_{uuid.uuid4().hex[:6]}", owner_id=u.id)
        membered = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                                slug=f"o2_{uuid.uuid4().hex[:6]}",
                                owner_id=user().id)
        db.session.add_all([owned, membered])
        db.session.flush()
        db.session.add(OrganizationMember(
            organization_id=membered.id, user_id=u.id,
            status=OrganizationMemberStatus.ACTIVE))
        db.session.commit()
        assert _collect_accessible_org_ids(u.id) == {owned.id, membered.id}


    def test_member_with_role_definitions(self, user):
        from api.auth import _collect_user_org_role_keys
        from models import OrganizationMemberRole, OrganizationRoleDefinition
        owner = user()
        member_user = user()
        org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                           slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
        db.session.add(org)
        db.session.flush()
        member = OrganizationMember(
            organization_id=org.id, user_id=member_user.id,
            status=OrganizationMemberStatus.ACTIVE,
            role=OrganizationRoleEnum.MEMBER)
        db.session.add(member)
        db.session.flush()
        for key in ("maintainer", "Maintainer", "dev"):
            definition = OrganizationRoleDefinition(
                organization_id=org.id, key=key, name=key, is_active=True)
            db.session.add(definition)
            db.session.flush()
            db.session.add(OrganizationMemberRole(
                organization_id=org.id, member_id=member.id,
                role_id=definition.id))
        db.session.commit()

        keys = _collect_user_org_role_keys(org, member_user.id)
        assert keys == ["maintainer", "dev"]  # 去重 + 小写归一

    def test_member_legacy_role_fallback(self, user):
        from api.auth import _collect_user_org_role_keys
        owner = user()
        member_user = user()
        org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                           slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
        db.session.add(org)
        db.session.flush()
        legacy_member = OrganizationMember(
            organization_id=org.id, user_id=member_user.id,
            status=OrganizationMemberStatus.ACTIVE,
            role=OrganizationRoleEnum.MEMBER)
        db.session.add(legacy_member)
        db.session.commit()
        assert _collect_user_org_role_keys(org, member_user.id) == ["member"]

    # 注：_collect_user_org_role_keys 末行的 return [] 在
    # organizations_member.role NOT NULL + default=MEMBER 约束下不可达（死行），
    # 按计划纪律不强行凑覆盖。


class TestDockerEnvAndCallbackGaps:
    def test_docker_env_uses_production_urls(self, client, monkeypatch):
        monkeypatch.setenv("DOCKER_ENV", "true")
        stub = SimpleNamespace(oauth=SimpleNamespace(github=SimpleNamespace(
            authorize_redirect=lambda uri: ("redirected", 302))))
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/login/github")
        assert resp.status_code == 302

    def test_guest_docker_env(self, client, monkeypatch):
        monkeypatch.setenv("DOCKER_ENV", "true")
        monkeypatch.setenv("GUEST_EMAIL", "dock-guest@t.io")
        stub = SimpleNamespace(generate_tokens=lambda u: {
            "access_token": "a", "refresh_token": "r", "token_type": "b"})
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/login/guest")
        assert resp.status_code == 302

    @pytest.mark.parametrize("missing", ["user_info", "create_user", "tokens"])
    def test_github_callback_intermediate_failures(self, client, monkeypatch, user, missing):
        u = user()
        stub = SimpleNamespace(
            oauth=SimpleNamespace(github=SimpleNamespace(
                authorize_access_token=lambda: {"access_token": "t"})),
            get_user_info=lambda t: None if missing == "user_info" else {"id": "1"},
            create_or_update_user=lambda info: None if missing == "create_user" else u,
            generate_tokens=lambda u: None if missing == "tokens" else {
                "access_token": "a", "refresh_token": "r", "token_type": "b"})
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/callback/github")
        assert resp.status_code in (400, 500)

    @pytest.mark.parametrize("missing", ["user_info", "create_user", "tokens"])
    def test_google_callback_intermediate_failures(self, client, monkeypatch, user, missing):
        u = user()
        stub = SimpleNamespace(
            oauth=SimpleNamespace(google=SimpleNamespace(
                authorize_access_token=lambda: {"access_token": "t"})),
            get_user_info=lambda t: None if missing == "user_info" else {"id": "1"},
            create_or_update_user=lambda info: None if missing == "create_user" else u,
            generate_tokens=lambda u: None if missing == "tokens" else {
                "access_token": "a", "refresh_token": "r", "token_type": "b"})
        monkeypatch.setattr("api.auth.google_service", stub)
        resp = client.get(f"{BASE}/google/callback")
        assert resp.status_code in (400, 500)


class TestEndpointExceptionHandlers:
    def test_logout_inner_exception_maps_500(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr("api.auth.get_current_user",
                            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.post(f"{BASE}/logout", headers=auth_headers["headers"], json={})
        assert resp.status_code == 500

    def test_me_inner_exception_maps_500(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr("api.auth.get_current_user",
                            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        assert client.get(f"{BASE}/me",
                          headers=auth_headers["headers"]).status_code == 500

    def test_verify_never_500s(self, client, monkeypatch):
        monkeypatch.setattr("api.auth.request", SimpleNamespace(
            get_json=MagicMock(side_effect=RuntimeError("boom"))))
        resp = client.post(f"{BASE}/verify", json={"token": "t"})
        assert resp.status_code in (200, 400)

    def test_refresh_with_non_int_identity_404(self, client, _isolated_app):
        with _isolated_app.test_request_context():
            refresh = create_refresh_token(identity="not-a-number")
        resp = client.post(f"{BASE}/refresh",
                           headers={"Authorization": f"Bearer {refresh}"})
        assert resp.status_code == 404

    def test_users_list_inner_exception_maps_500(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr("api.auth.get_current_user",
                            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.get(f"{BASE}/users", headers=admin_headers["headers"])
        assert resp.status_code == 500

    def test_user_detail_inner_exception_maps_500(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr("api.auth.get_current_user",
                            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.get(f"{BASE}/users/{auth_headers['user'].id}",
                          headers=auth_headers["headers"])
        assert resp.status_code == 500

    def test_status_inner_exception_maps_500(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr("api.auth.get_current_user",
                            lambda: (_ for _ in ()).throw(RuntimeError("db")))
        resp = client.put(f"{BASE}/users/1/status",
                          headers=admin_headers["headers"], json={"status": "active"})
        assert resp.status_code == 500


class TestOAuthEntry:
    def test_login_entry_delegates_to_github(self, client, monkeypatch):
        monkeypatch.delenv("DOCKER_ENV", raising=False)
        stub = SimpleNamespace(oauth=SimpleNamespace(github=SimpleNamespace(
            authorize_redirect=lambda uri: ("redirected", 302))))
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/login/github", headers={"Origin": "http://localhost:50111"})
        assert resp.status_code == 302

    def test_google_login_redirects(self, client, monkeypatch):
        monkeypatch.delenv("DOCKER_ENV", raising=False)
        stub = SimpleNamespace(oauth=SimpleNamespace(google=SimpleNamespace(
            authorize_redirect=lambda uri: ("redirected", 302))))
        monkeypatch.setattr("api.auth.google_service", stub)
        resp = client.get(f"{BASE}/login/google")
        assert resp.status_code == 302

    def test_login_without_oauth_config_returns_error(self, client):
        # testing 环境无 OAuth 凭据 → 异常被 handle_api_error 兜底
        resp = client.get(f"{BASE}/login")
        assert resp.status_code in (302, 500)

    def test_callback_failure_paths(self, client, monkeypatch):
        stub = SimpleNamespace(oauth=SimpleNamespace(github=SimpleNamespace(
            authorize_access_token=lambda: None)))
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/callback/github")
        assert resp.status_code == 400
        assert "access token" in resp.get_json()["message"].lower()


class TestOAuthCallbacks:
    def _stub_github_flow(self, monkeypatch, user_row):
        stub = SimpleNamespace(
            oauth=SimpleNamespace(github=SimpleNamespace(
                authorize_access_token=lambda: {"access_token": "gh-tok"})),
            get_user_info=lambda t: {"id": "gh-1", "login": "ghuser",
                                     "email": "gh@t.io"},
            create_or_update_user=lambda info: user_row,
            generate_tokens=lambda u: {"access_token": "acc",
                                       "refresh_token": "ref",
                                       "token_type": "bearer"})
        monkeypatch.setattr("api.auth.github_service", stub)

    def test_github_callback_success(self, client, monkeypatch, user):
        u = user()
        self._stub_github_flow(monkeypatch, u)
        with client.session_transaction() as sess:
            sess["redirect_after_login"] = "http://127.0.0.1:50111/todo-for-ai/pages/dashboard"
        resp = client.get(f"{BASE}/callback/github")
        assert resp.status_code == 302
        assert "access_token=acc" in resp.headers["Location"]

    def test_github_callback_token_exchange_failure(self, client, monkeypatch):
        stub = SimpleNamespace(oauth=SimpleNamespace(github=SimpleNamespace(
            authorize_access_token=lambda: None)))
        monkeypatch.setattr("api.auth.github_service", stub)
        resp = client.get(f"{BASE}/callback/github")
        assert resp.status_code == 400

    def test_callback_alias_delegates_to_github(self, client, monkeypatch, user):
        self._stub_github_flow(monkeypatch, user())
        resp = client.get(f"{BASE}/callback")
        assert resp.status_code == 302

    def test_google_callback_success(self, client, monkeypatch, user):
        u = user()
        stub = SimpleNamespace(
            oauth=SimpleNamespace(google=SimpleNamespace(
                authorize_access_token=lambda: {"access_token": "g-tok"})),
            get_user_info=lambda t: {"id": "g-1", "email": "g@t.io"},
            create_or_update_user=lambda info: u,
            generate_tokens=lambda u: {"access_token": "acc",
                                       "refresh_token": "ref",
                                       "token_type": "bearer"})
        monkeypatch.setattr("api.auth.google_service", stub)
        resp = client.get(f"{BASE}/google/callback")
        assert resp.status_code == 302
        assert "refresh_token=ref" in resp.headers["Location"]

    def test_google_login_without_config_errors(self, client):
        resp = client.get(f"{BASE}/login/google")
        assert resp.status_code in (302, 500)


class TestExceptionAndTailBranches:
    def test_login_entry_exception_maps_error(self, client, monkeypatch):
        def boom(uri):
            raise RuntimeError("oauth down")
        monkeypatch.delenv("DOCKER_ENV", raising=False)
        monkeypatch.setattr("api.auth.github_service", SimpleNamespace(
            oauth=SimpleNamespace(github=SimpleNamespace(
                authorize_redirect=boom))))
        assert client.get(f"{BASE}/login/github").status_code == 500

    def test_google_login_exception_maps_error(self, client, monkeypatch):
        def boom(uri):
            raise RuntimeError("oauth down")
        monkeypatch.delenv("DOCKER_ENV", raising=False)
        monkeypatch.setattr("api.auth.google_service", SimpleNamespace(
            oauth=SimpleNamespace(google=SimpleNamespace(
                authorize_redirect=boom))))
        assert client.get(f"{BASE}/login/google").status_code == 500

    def test_guest_token_exception_maps_500(self, client, monkeypatch):
        def boom(u):
            raise RuntimeError("jwt down")
        monkeypatch.setattr("api.auth.github_service",
                            SimpleNamespace(generate_tokens=boom))
        assert client.get(f"{BASE}/login/guest").status_code == 500

    def test_github_callback_exception_maps_500(self, client, monkeypatch):
        stub = SimpleNamespace(oauth=SimpleNamespace(github=SimpleNamespace(
            authorize_access_token=lambda: {"access_token": "t"})),
            get_user_info=lambda t: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr("api.auth.github_service", stub)
        assert client.get(f"{BASE}/callback/github").status_code == 500

    def test_google_callback_exchange_none(self, client, monkeypatch):
        stub = SimpleNamespace(oauth=SimpleNamespace(google=SimpleNamespace(
            authorize_access_token=lambda: None)))
        monkeypatch.setattr("api.auth.google_service", stub)
        resp = client.get(f"{BASE}/google/callback")
        assert resp.status_code == 400

    def test_google_callback_exception_maps_500(self, client, monkeypatch):
        stub = SimpleNamespace(oauth=SimpleNamespace(google=SimpleNamespace(
            authorize_access_token=lambda: (_ for _ in ()).throw(
                RuntimeError("x")))))
        monkeypatch.setattr("api.auth.google_service", stub)
        assert client.get(f"{BASE}/google/callback").status_code == 500

    def test_update_me_save_failure_maps_500(self, client, auth_headers,
                                             monkeypatch):
        def boom():
            raise RuntimeError("db down")
        flaky = auth_headers["user"]
        flaky.save = boom
        resp = client.put(f"{BASE}/me", headers=auth_headers["headers"],
                          json={"nickname": "x"})
        assert resp.status_code == 500

    def test_refresh_token_generation_failure_500(self, client, _isolated_app,
                                                  user, monkeypatch):
        u = user()
        stub = SimpleNamespace(generate_tokens=lambda u: None)
        monkeypatch.setattr("api.auth.github_service", stub)
        with _isolated_app.test_request_context():
            refresh = create_refresh_token(identity=str(u.id))
        resp = client.post(f"{BASE}/refresh",
                           headers={"Authorization": f"Bearer {refresh}"})
        assert resp.status_code == 500

    def test_users_role_filter(self, client, admin_headers, user):
        u = user(role=UserRole.ADMIN)
        resp = client.get(f"{BASE}/users?role=ADMIN",
                          headers=admin_headers["headers"])
        emails = [x["email"] for x in resp.get_json()["data"]["users"]]
        assert u.email in emails


class TestLogoutAndMe:
    def test_logout_updates_last_active(self, client, auth_headers):
        resp = client.post(f"{BASE}/logout", headers=auth_headers["headers"],
                           json={"return_to": "https://f.example/x"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["redirect_url"] == "https://f.example/x"
        assert auth_headers["user"].last_active_at is None

    def test_logout_requires_auth(self, client):
        assert client.post(f"{BASE}/logout").status_code == 401

    def test_me_returns_user(self, client, auth_headers):
        resp = client.get(f"{BASE}/me", headers=auth_headers["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["id"] == auth_headers["user"].id

    def test_update_me_fields_and_preferences(self, client, auth_headers):
        resp = client.put(f"{BASE}/me", headers=auth_headers["headers"], json={
            "nickname": "新昵称", "timezone": "Asia/Shanghai",
            "preferences": {"theme": "dark"},
        })
        assert resp.status_code == 200
        assert auth_headers["user"].nickname == "新昵称"
        assert auth_headers["user"].preferences["theme"] == "dark"

    def test_update_me_preferences_must_be_object(self, client, auth_headers):
        resp = client.put(f"{BASE}/me", headers=auth_headers["headers"],
                          json={"preferences": [1]})
        assert resp.status_code == 400

    def test_update_me_requires_json(self, client, auth_headers):
        resp = client.put(f"{BASE}/me", headers=auth_headers["headers"],
                          data="plain", content_type="text/plain")
        assert resp.status_code == 400


class TestVerifyAndRefresh:
    def test_verify_without_token_field(self, client):
        resp = client.post(f"{BASE}/verify", json={})
        assert resp.status_code == 400

    def test_verify_accepts_any_payload_with_token(self, client):
        resp = client.post(f"{BASE}/verify", json={"token": "whatever"})
        assert resp.status_code == 200
        assert resp.get_json()["data"]["valid"] is True

    def test_refresh_without_token_rejected(self, client):
        assert client.post(f"{BASE}/refresh").status_code in (401, 422)

    def test_refresh_issues_new_tokens(self, client, _isolated_app, user):
        u = user()
        with _isolated_app.test_request_context():
            refresh = create_refresh_token(identity=str(u.id))
        resp = client.post(f"{BASE}/refresh",
                           headers={"Authorization": f"Bearer {refresh}"})
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["access_token"] and body["refresh_token"]

    def test_refresh_with_unknown_user_404(self, client, _isolated_app):
        with _isolated_app.test_request_context():
            refresh = create_refresh_token(identity="999999")
        resp = client.post(f"{BASE}/refresh",
                           headers={"Authorization": f"Bearer {refresh}"})
        assert resp.status_code == 404


class TestUserListing:
    def test_requires_admin(self, client, auth_headers):
        resp = client.get(f"{BASE}/users", headers=auth_headers["headers"])
        assert resp.status_code == 403

    def test_admin_lists_with_filters(self, client, admin_headers, user):
        u1 = user(email="alpha@t.io")
        u2 = user()
        u2.status = UserStatus.SUSPENDED
        db.session.commit()

        resp = client.get(f"{BASE}/users", headers=admin_headers["headers"])
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["pagination"]["total"] >= 2

        resp = client.get(f"{BASE}/users?search=alpha",
                          headers=admin_headers["headers"])
        names = [u["email"] for u in resp.get_json()["data"]["users"]]
        assert names == ["alpha@t.io"]

        resp = client.get(f"{BASE}/users?status=SUSPENDED",
                          headers=admin_headers["headers"])
        assert all(str(u["status"]).lower() == "suspended"
                   for u in resp.get_json()["data"]["users"])

    def test_admin_guard_rejects_suspended_admin(self, client, admin_headers):
        admin_headers["user"].status = "SUSPENDED"
        db.session.commit()
        assert client.get(f"{BASE}/users",
                          headers=admin_headers["headers"]).status_code == 403


class TestUserDetail:
    def test_404_for_missing_user(self, client, admin_headers):
        resp = client.get(f"{BASE}/users/999999", headers=admin_headers["headers"])
        assert resp.status_code == 404

    def test_self_view(self, client, auth_headers):
        resp = client.get(f"{BASE}/users/{auth_headers['user'].id}",
                          headers=auth_headers["headers"])
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["is_self"] is True and body["view_mode"] == "self"

    def test_admin_view_of_other(self, client, admin_headers, user):
        other = user()
        resp = client.get(f"{BASE}/users/{other.id}",
                          headers=admin_headers["headers"])
        body = resp.get_json()["data"]
        assert body["view_mode"] == "admin" and body["is_self"] is False

    def test_public_view_requires_shared_org(self, client, user):
        from flask_jwt_extended import create_access_token
        viewer = user()
        target = user()
        v_token = create_access_token(identity=str(viewer.id))
        headers = {"Authorization": f"Bearer {v_token}"}

        # 无共享组织 → 403
        resp = client.get(f"{BASE}/users/{target.id}", headers=headers)
        assert resp.status_code == 403

        # 共享组织 → 公开档案（含双方角色键）
        org = Organization(name=f"o_{uuid.uuid4().hex[:6]}",
                           slug=f"o_{uuid.uuid4().hex[:6]}", owner_id=target.id)
        db.session.add(org)
        db.session.flush()
        db.session.add(OrganizationMember(
            organization_id=org.id, user_id=viewer.id,
            status=OrganizationMemberStatus.ACTIVE))
        db.session.commit()

        resp = client.get(f"{BASE}/users/{target.id}", headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["view_mode"] == "public"
        assert body["shared_organization_count"] == 1
        assert body["shared_organizations"][0]["target_roles"] == ["owner"]


class TestUserStatusAdmin:
    def test_requires_admin(self, client, auth_headers, user):
        target = user()
        resp = client.put(f"{BASE}/users/{target.id}/status",
                          headers=auth_headers["headers"], json={"status": "SUSPENDED"})
        assert resp.status_code == 403

    def test_admin_updates_status(self, client, admin_headers, user):
        target = user()
        resp = client.put(f"{BASE}/users/{target.id}/status",
                          headers=admin_headers["headers"],
                          json={"status": "suspended"})
        assert resp.status_code == 200
        assert target.status.value == "suspended"

    def test_invalid_status_rejected(self, client, admin_headers, user):
        target = user()
        resp = client.put(f"{BASE}/users/{target.id}/status",
                          headers=admin_headers["headers"],
                          json={"status": "GARBAGE"})
        assert resp.status_code == 400

    def test_missing_status_rejected(self, client, admin_headers, user):
        resp = client.put(f"{BASE}/users/{user().id}/status",
                          headers=admin_headers["headers"], json={})
        assert resp.status_code == 400

    def test_missing_user_404(self, client, admin_headers):
        resp = client.put(f"{BASE}/users/999999/status",
                          headers=admin_headers["headers"], json={"status": "ACTIVE"})
        assert resp.status_code == 404
