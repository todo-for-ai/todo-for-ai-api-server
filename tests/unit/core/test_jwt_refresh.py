"""JWT 自动续期中间件（core/jwt_refresh.py）单元回归。

覆盖：刷新判定（新鲜/将过期/已过期/缺字段/异常）、中间件分支
（4xx 跳过、无 JWT 跳过、触发续期写 X-New-Token、身份转换、外层异常吞噬）。
"""

from types import SimpleNamespace

import pytest

from core import jwt_refresh as jr


def _jwt(exp_in, iat_ago=900, now_shift=0):
    """构造 exp/iat：iat 为 ago 秒前，exp 为 now+exp_in 秒。"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc) + timedelta(seconds=now_shift)
    iat = int((now - timedelta(seconds=iat_ago)).timestamp())
    exp = int((now + timedelta(seconds=exp_in)).timestamp())
    return {"exp": exp, "iat": iat}


class TestShouldRefresh:
    def test_missing_claims_false(self):
        assert jr.should_refresh_token({}) is False
        assert jr.should_refresh_token({"exp": 1}) is False

    def test_fresh_token_false(self):
        # 总寿命 1800s，剩余 1500s > 1/3 → 不刷新
        assert jr.should_refresh_token(_jwt(exp_in=1500)) is False

    def test_stale_token_true(self):
        # 剩余 300s < 1800/3 → 刷新
        assert jr.should_refresh_token(_jwt(exp_in=300)) is True

    def test_expired_token_false(self):
        assert jr.should_refresh_token(_jwt(exp_in=-10)) is False

    def test_exception_swallowed(self):
        assert jr.should_refresh_token(None) is False


@pytest.fixture
def app():
    from flask import Flask
    application = Flask(__name__)
    jr.setup_jwt_refresh(application)
    return application


@pytest.fixture
def after_func(app):
    return app.after_request_funcs[None][0]


def _resp(status=200):
    return SimpleNamespace(status_code=status, headers={})


class TestMiddleware:
    def test_skips_error_responses(self, after_func, monkeypatch):
        called = {"verify": False}

        def _verify(optional=True):
            called["verify"] = True
        monkeypatch.setattr(jr, "verify_jwt_in_request", _verify)
        resp = after_func(_resp(status=500))
        assert called["verify"] is False
        assert "X-New-Token" not in resp.headers

    def test_skips_on_verify_failure(self, after_func, monkeypatch):
        def boom(optional=True):
            raise RuntimeError("no jwt")
        monkeypatch.setattr(jr, "verify_jwt_in_request", boom)
        resp = after_func(_resp())
        assert "X-New-Token" not in resp.headers

    def test_skips_when_no_jwt_data(self, after_func, monkeypatch):
        monkeypatch.setattr(jr, "verify_jwt_in_request", lambda optional=True: None)
        monkeypatch.setattr(jr, "get_jwt", lambda: None)
        resp = after_func(_resp())
        assert "X-New-Token" not in resp.headers

    def test_refreshes_with_int_identity(self, after_func, monkeypatch):
        monkeypatch.setattr(jr, "verify_jwt_in_request", lambda optional=True: None)
        monkeypatch.setattr(jr, "get_jwt", lambda: {"exp": 1, "iat": 2,
                                                   "username": "u", "email": "e"})
        monkeypatch.setattr(jr, "should_refresh_token", lambda d: True)
        monkeypatch.setattr(jr, "get_jwt_identity", lambda: "42")
        monkeypatch.setattr(jr, "create_access_token",
                            lambda identity, additional_claims=None: "new-tok")
        resp = after_func(_resp())
        assert resp.headers["X-New-Token"] == "new-tok"
        assert resp.headers["Access-Control-Expose-Headers"] == "X-New-Token"

    def test_non_int_identity_kept_raw(self, after_func, monkeypatch):
        captured = {}

        def fake_create(identity, additional_claims=None):
            captured["identity"] = identity
            return "tok"
        monkeypatch.setattr(jr, "verify_jwt_in_request", lambda optional=True: None)
        monkeypatch.setattr(jr, "get_jwt", lambda: {"exp": 1, "iat": 2})
        monkeypatch.setattr(jr, "should_refresh_token", lambda d: True)
        monkeypatch.setattr(jr, "get_jwt_identity", lambda: "guest-account")
        monkeypatch.setattr(jr, "create_access_token", fake_create)
        after_func(_resp())
        assert captured["identity"] == "guest-account"

    def test_outer_exception_swallows(self, after_func, monkeypatch):
        monkeypatch.setattr(jr, "verify_jwt_in_request", lambda optional=True: None)
        monkeypatch.setattr(jr, "get_jwt", lambda: {"exp": 1, "iat": 2})
        monkeypatch.setattr(jr, "should_refresh_token",
                            lambda d: (_ for _ in ()).throw(RuntimeError("x")))
        resp = after_func(_resp())
        assert "X-New-Token" not in resp.headers
