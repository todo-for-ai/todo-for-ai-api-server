"""MCP 共享工具（api/mcp/_shared.py）单元回归。

覆盖：内存频率限制（超限 429/按用户分桶/过期清理）、API Token 认证装饰器
（缺头 401/坏方案 401/无效 token 401/成功注入 g）、XSS 清洗、整数校验。
"""

from types import SimpleNamespace

import pytest
from flask import Flask, g, jsonify

from api.mcp import _shared as shared_mod
from api.mcp._shared import (
    rate_limit,
    require_api_token_auth,
    sanitize_input,
    validate_integer,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    app = create_app("testing")
    app.config.update({"TESTING": True})
    ctx = app.app_context()
    ctx.push()
    yield app
    ctx.pop()


@pytest.fixture(autouse=True)
def _clean_limiter():
    shared_mod.rate_limiter.clear()
    yield
    shared_mod.rate_limiter.clear()


class TestSanitizeInput:
    def test_non_string_passthrough(self):
        assert sanitize_input(123) == 123
        assert sanitize_input(None) is None

    def test_html_escaped(self):
        assert sanitize_input("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"

    def test_script_tag_escaped_not_stripped(self):
        # 行为钉子：html.escape 先执行，script 标签被转义而非删除
        cleaned = sanitize_input("a<script>alert(1)</script>b")
        assert cleaned == "a&lt;script&gt;alert(1)&lt;/script&gt;b"

    def test_javascript_uri_removed(self):
        assert "javascript:" not in sanitize_input("x javascript:alert(1)")

    def test_onhandler_removed(self):
        cleaned = sanitize_input("<img onerror= alert(1)>")
        assert "onerror" not in cleaned.lower()


class TestValidateInteger:
    def test_int_passthrough(self):
        assert validate_integer(7, "f") == 7

    def test_digit_string(self):
        assert validate_integer("42", "f") == 42

    @pytest.mark.parametrize("bad", ["abc", "1.5", "", None, [1]])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError, match="must be a valid integer"):
            validate_integer(bad, "page")


class TestRateLimit:
    def _endpoint(self, app, max_requests=2, window_seconds=60):
        @app.route("/limited")
        @rate_limit(max_requests=max_requests, window_seconds=window_seconds)
        def limited():
            return jsonify(ok=True)

        return app.test_client()

    def test_allows_under_limit_then_429(self):
        app = Flask(__name__)
        client = self._endpoint(app)
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200
        resp = client.get("/limited")
        assert resp.status_code == 429
        assert resp.get_json()["error"] == "Rate limit exceeded"

    def test_expired_entries_pruned(self, monkeypatch):
        app = Flask(__name__)
        client = self._endpoint(app)
        now = {"t": 1000.0}
        monkeypatch.setattr(shared_mod, "time",
                            SimpleNamespace(time=lambda: now["t"]))
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200
        now["t"] += 61  # 窗口外 → 记录全部过期
        assert client.get("/limited").status_code == 200

    def test_user_identity_buckets(self):
        app = Flask(__name__)

        @app.route("/who")
        @rate_limit(max_requests=1, window_seconds=60)
        def who():
            return jsonify(user=g.current_user.id)

        with app.test_request_context("/who"):
            g.current_user = SimpleNamespace(id=7)
            assert who().status_code == 200
        with app.test_request_context("/who"):
            g.current_user = SimpleNamespace(id=8)  # 不同用户独立计数
            assert who().status_code == 200
        assert "user_7" in shared_mod.rate_limiter


def _resp_status(resp):
    """装饰器 401 路径返回 (jsonify, 401) 元组，成功路径返回 Response。"""
    return resp[1] if isinstance(resp, tuple) else resp.status_code


def _resp_json(resp):
    return (resp[0] if isinstance(resp, tuple) else resp).get_json()


class TestRequireApiTokenAuth:
    def _decorated(self):
        @require_api_token_auth
        def endpoint():
            return jsonify(ok=True, user=g.current_user.id)

        return endpoint

    def _request_ctx(self, auth_header=None):
        app = Flask(__name__)
        headers = {"Authorization": auth_header} if auth_header else {}
        return app.test_request_context("/mcp", headers=headers)

    def test_missing_header_401(self):
        with self._request_ctx(None):
            resp = self._decorated()()
            assert _resp_status(resp) == 401
            assert _resp_json(resp)["error"] == "Missing or invalid authorization header"

    def test_bad_scheme_401(self):
        with self._request_ctx("Basic abc"):
            assert _resp_status(self._decorated()()) == 401

    def test_invalid_token_401(self, monkeypatch):
        monkeypatch.setattr(shared_mod.ApiToken, "verify_token",
                            staticmethod(lambda t: None))
        with self._request_ctx("Bearer bad-token"):
            resp = self._decorated()()
            assert _resp_status(resp) == 401
            assert _resp_json(resp)["error"] == "Invalid or expired token"

    def test_valid_token_injects_context(self, monkeypatch):
        stub_user = SimpleNamespace(id=3, email="u@t.io")
        stub_token = SimpleNamespace(id=11, name="ci", user=stub_user)
        monkeypatch.setattr(shared_mod.ApiToken, "verify_token",
                            staticmethod(lambda t: stub_token))

        with self._request_ctx("Bearer good-token-123"):
            resp = self._decorated()()
            assert resp.status_code == 200
            assert resp.get_json()["user"] == 3
            assert g.api_token is stub_token
            assert g.current_user is stub_user
