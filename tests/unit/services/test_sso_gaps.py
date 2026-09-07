"""SSO 账号映射（services/sso.py find_or_create_user）缺口补测。

补齐：缺 email 拒绝、按 email 命中既有账号、新建用户的名字回退链。
OIDC/SAML 完整回调链路由 api 层测试覆盖。
"""

import uuid

import pytest

from types import SimpleNamespace

from models import User, WorkspaceSSOConfig, db
from services.sso import (
    build_oidc_authorize_url,
    build_oidc_state,
    upsert_config,
    exchange_code,
    find_or_create_user,
    login_oidc,
    make_saml_state,
    build_saml_login,
    login_saml,
)


def _ws_config(workspace_id=1, provider="oidc", enabled=True, **kw):
    row = WorkspaceSSOConfig(
        workspace_id=workspace_id, provider=provider, enabled=enabled,
        client_id="cid", authorize_url="https://idp/authorize",
        token_url="https://idp/token", userinfo_url="https://idp/userinfo",
        redirect_uri="https://app/cb", **kw)
    db.session.add(row)
    db.session.commit()
    return row


class _FakeHTTP:
    """httpx 兼容假客户端：post/get 返回预设 JSON。"""

    def __init__(self, token_payload, userinfo_payload):
        self.token_payload = token_payload
        self.userinfo_payload = userinfo_payload
        self.calls = []

    def post(self, url, data=None):
        self.calls.append(("post", url, data))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: self.token_payload)

    def get(self, url, headers=None):
        self.calls.append(("get", url, headers))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: self.userinfo_payload)


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


class TestFindOrCreateUser:
    def test_missing_email_raises(self):
        with pytest.raises(ValueError, match="missing email"):
            find_or_create_user({"email": "   "})

    def test_existing_user_returned_by_email(self):
        email = f"sso_{uuid.uuid4().hex[:6]}@t.io"
        existing = User(username=f"eu_{uuid.uuid4().hex[:6]}", email=email)
        db.session.add(existing)
        db.session.commit()

        user = find_or_create_user({"email": email.upper(), "name": "别的名字"})
        assert user.id == existing.id  # 大小写归一后命中，不新建
        assert User.query.filter_by(email=email).count() == 1

    def test_creates_user_with_name(self):
        user = find_or_create_user({
            "email": f"new_{uuid.uuid4().hex[:6]}@t.io", "name": "张三"})
        assert user.username.startswith("sso_张三_")  # 唯一化格式 sso_<name>_<rand>

    def test_creates_user_with_email_prefix_fallback(self):
        email = f"prefix_{uuid.uuid4().hex[:6]}@t.io"
        user = find_or_create_user({"email": email})
        assert user.username.startswith(f"sso_{email.split(chr(64))[0]}_")
        assert user.id is not None


class TestExchangeCode:
    def test_success_returns_userinfo(self):
        config = _ws_config()
        http = _FakeHTTP({"access_token": "at-1"}, {"email": "a@t.io", "name": "甲"})
        userinfo = exchange_code(config, "code-1", http_client=http)
        assert userinfo["email"] == "a@t.io"
        assert http.calls[0][1] == "https://idp/token"
        assert http.calls[1][1] == "https://idp/userinfo"
        assert http.calls[1][2]["Authorization"] == "Bearer at-1"

    def test_token_endpoint_without_access_token_raises(self):
        config = _ws_config()
        http = _FakeHTTP({"error": "bad"}, {"email": "a@t.io"})
        with pytest.raises(ValueError, match="no access_token"):
            exchange_code(config, "code-1", http_client=http)


class TestLoginOidc:
    def test_state_workspace_mismatch(self):
        state = build_oidc_state(1)
        with pytest.raises(ValueError, match="workspace mismatch"):
            login_oidc(2, "code", state)

    def test_sso_not_enabled(self):
        _ws_config(workspace_id=3, enabled=False)
        state = build_oidc_state(3)
        with pytest.raises(ValueError, match="SSO not enabled"):
            login_oidc(3, "code", state)

    def test_full_login_flow(self):
        _ws_config(workspace_id=4)
        state = build_oidc_state(4)
        http = _FakeHTTP({"access_token": "at-2"},
                         {"email": "oidc@t.io", "name": "乙"})
        result = login_oidc(4, "code-2", state, http_client=http)
        assert result["access_token"]
        assert result["user"]["username"].startswith("sso_乙_")


class TestSamlFlows:
    def test_build_saml_login_not_enabled(self):
        _ws_config(workspace_id=5, provider="saml", enabled=False)
        state = make_saml_state(5)
        with pytest.raises(ValueError, match="SAML not enabled"):
            build_saml_login(5, state)

    def test_build_saml_login_state_without_request_id(self):
        _ws_config(workspace_id=6, provider="saml")
        oidc_state = build_oidc_state(6)  # 有 ws 但无 request_id
        with pytest.raises(ValueError, match="request_id"):
            build_saml_login(6, oidc_state)

    def test_login_saml_state_mismatch(self):
        _ws_config(workspace_id=7, provider="saml")
        state = make_saml_state(7)
        with pytest.raises(ValueError, match="workspace mismatch"):
            login_saml(8, "resp", state)

    def test_login_saml_not_enabled(self):
        _ws_config(workspace_id=9, provider="saml", enabled=False)
        state = make_saml_state(9)
        with pytest.raises(ValueError, match="SAML not enabled"):
            login_saml(9, "resp", state)


class TestUpsertAndAuthorizeUrl:
    def test_upsert_writes_encrypted_secret(self):
        row = upsert_config(21, {
            "provider": "oidc", "enabled": True, "client_id": "cid",
            "client_secret": "sec-1", "issuer": "https://idp",
        })
        assert row.client_id == "cid"
        assert row.client_secret_encrypted.startswith("v1:")
        row2 = upsert_config(21, {"enabled": False})
        assert row2.id == row.id and row2.enabled is False

    def test_authorize_url_construction(self):
        config = _ws_config()
        config.authorize_url = "https://idp/authorize"
        db.session.commit()
        url = build_oidc_authorize_url(config, "state-1",
                                       redirect_uri="https://app/cb")
        assert url.startswith("https://idp/authorize?")
        assert "response_type=code" in url and "state=state-1" in url

        config.authorize_url = "https://idp/authorize?base=1"
        db.session.commit()
        url2 = build_oidc_authorize_url(config, "state-2")
        assert url2.startswith("https://idp/authorize?base=1&")

    def test_injected_client_not_closed(self):
        config = _ws_config()
        http = _FakeHTTP({"access_token": "at"}, {"email": "z@t.io"})
        exchange_code(config, "c", http_client=http)  # 注入的客户端不负责 close
        assert len(http.calls) == 2

    def test_default_client_is_closed(self, monkeypatch):
        """无注入时自建 httpx.Client，finally 中负责 close。"""
        closed = {"n": 0}

        class _FakeClient:
            def __init__(self, timeout=None):
                pass

            def post(self, url, data=None):
                return SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"access_token": "at"})

            def get(self, url, headers=None):
                return SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"email": "y@t.io"})

            def close(self):
                closed["n"] += 1

        import sys
        import types
        fake_httpx = types.SimpleNamespace(Client=_FakeClient)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        config = _ws_config()
        userinfo = exchange_code(config, "c")  # 不注入 http_client
        assert userinfo["email"] == "y@t.io"
        assert closed["n"] == 1
