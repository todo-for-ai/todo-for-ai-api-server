"""GitHub REST 轻量客户端（services/github_client.py）单元回归。

覆盖：请求封装（URL 拼接/204 空体/非 JSON 体/错误体映射/网络异常 502）、
分支（get/create/ensure 语义）、PR（建/查/列/合并）、resolve_token 三级
凭证优先级（App installation → 绑定 token → 环境变量，逐级静默回退）。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from services import github_client as gh_mod
from services.github_client import (
    GITHUB_API_BASE,
    GitHubClient,
    GitHubClientError,
    resolve_token,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app import create_app
    app = create_app("testing")
    app.config.update({"TESTING": True})
    ctx = app.app_context()
    ctx.push()
    yield app
    ctx.pop()


@pytest.fixture
def http(monkeypatch):
    """替换 github_client 模块命名空间里的 requests，捕获请求并 scripted 应答。"""
    responses = []
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        resp = responses.pop(0) if responses else MagicMock(status_code=200, json=lambda: {})
        return resp

    fake = SimpleNamespace(request=fake_request,
                           RequestException=requests.RequestException)
    monkeypatch.setattr(gh_mod, "requests", fake)
    holder = SimpleNamespace(responses=responses, calls=calls)
    return holder


def _json_resp(status_code, payload=None, text=""):
    return MagicMock(status_code=status_code,
                     json=MagicMock(return_value=payload if payload is not None else {}),
                     text=text)


class TestRequestCore:
    def test_headers_with_and_without_token(self):
        assert "Authorization" not in GitHubClient(None)._headers()
        headers = GitHubClient("tok-1")._headers()
        assert headers["Authorization"] == "token tok-1"
        assert headers["Accept"].startswith("application/vnd.github")

    def test_success_and_url_building(self, http):
        http.responses.append(_json_resp(200, {"ok": 1}))
        status, data = GitHubClient("t")._request("GET", "/repos/o/r")
        assert (status, data) == (200, {"ok": 1})
        assert http.calls[0]["url"] == f"{GITHUB_API_BASE}/repos/o/r"

    def test_absolute_url_passthrough(self, http):
        http.responses.append(_json_resp(200, {}))
        GitHubClient("t")._request("GET", "https://api.example.com/x")
        assert http.calls[0]["url"] == "https://api.example.com/x"

    def test_204_empty_body(self, http):
        http.responses.append(_json_resp(204))
        status, data = GitHubClient("t")._request("DELETE", "/x")
        assert status == 204 and data == {}

    def test_non_json_body_wraps_raw(self, http):
        http.responses.append(_json_resp(200, text="<html>boom"))
        http.responses[0].json.side_effect = ValueError("no json")
        status, data = GitHubClient("t")._request("GET", "/x")
        assert data == {"raw": "<html>boom"}

    def test_error_status_raises_with_details(self, http):
        http.responses.append(_json_resp(
            422, {"message": "Validation Failed", "errors": ["bad head"]}))
        with pytest.raises(GitHubClientError) as exc:
            GitHubClient("t")._request("POST", "/repos/o/r/pulls", payload={})
        assert exc.value.status_code == 422
        assert "Validation Failed" in str(exc.value)
        assert exc.value.details["errors"] == ["bad head"]

    def test_network_failure_maps_to_502(self, http, monkeypatch):
        def boom(*a, **kw):
            raise requests.ConnectionError("refused")
        monkeypatch.setattr(gh_mod, "requests", SimpleNamespace(
            request=boom, RequestException=requests.RequestException))
        with pytest.raises(GitHubClientError) as exc:
            GitHubClient("t")._request("GET", "/x")
        assert exc.value.status_code == 502


class TestBranches:
    def test_get_repo_and_branch(self, http):
        http.responses.append(_json_resp(200, {"full_name": "o/r"}))
        assert GitHubClient("t").get_repo("o", "r") == {"full_name": "o/r"}

        http.responses.append(_json_resp(200, {"name": "main"}))
        assert GitHubClient("t").get_branch("o", "r", "main") == {"name": "main"}

    def test_get_branch_404_returns_none(self, http):
        http.responses.append(_json_resp(404, {"message": "Not Found"}))
        assert GitHubClient("t").get_branch("o", "r", "nope") is None

    def test_get_branch_other_error_raises(self, http):
        http.responses.append(_json_resp(403, {"message": "forbidden"}))
        with pytest.raises(GitHubClientError) as exc:
            GitHubClient("t").get_branch("o", "r", "x")
        assert exc.value.status_code == 403

    def test_create_branch_from_ref(self, http):
        http.responses.append(_json_resp(200, {"object": {"sha": "abc123"}}))
        http.responses.append(_json_resp(201, {"ref": "refs/heads/feat"}))
        data = GitHubClient("t").create_branch("o", "r", "feat", "main")
        assert data == {"ref": "refs/heads/feat"}
        create = http.calls[1]
        assert create["method"] == "POST"
        assert create["json"] == {"ref": "refs/heads/feat", "sha": "abc123"}

    def test_create_branch_without_sha(self, http):
        http.responses.append(_json_resp(200, {"object": {}}))
        with pytest.raises(GitHubClientError) as exc:
            GitHubClient("t").create_branch("o", "r", "feat", "ghost")
        assert exc.value.status_code == 422

    def test_ensure_branch_semantics(self, http):
        client = GitHubClient("t")
        # 已存在 → False，不创建
        http.responses.append(_json_resp(200, {"name": "feat"}))
        assert client.ensure_branch("o", "r", "feat", "main") is False
        # 不存在 → 创建并返回 True
        http.responses.append(_json_resp(404, {"message": "Not Found"}))
        http.responses.append(_json_resp(200, {"object": {"sha": "s"}}))
        http.responses.append(_json_resp(201, {}))
        assert client.ensure_branch("o", "r", "feat2", "main") is True


class TestPullRequests:
    def test_create_and_get(self, http):
        http.responses.append(_json_resp(201, {"number": 7}))
        pr = GitHubClient("t").create_pull_request(
            "o", "r", head="feat", base="main", title="T", body="B", draft=True)
        assert pr["number"] == 7
        assert http.calls[0]["json"]["draft"] is True
        assert http.calls[0]["json"]["head"] == "feat"

        http.responses.append(_json_resp(200, {"number": 7, "state": "open"}))
        assert GitHubClient("t").get_pull_request("o", "r", 7)["state"] == "open"

    def test_list_with_filters(self, http):
        http.responses.append(_json_resp(200, []))
        data = GitHubClient("t").list_pull_requests("o", "r", head="feat",
                                                    base="main", state="closed")
        assert data == []
        params = http.calls[0]["params"]
        assert params == {"state": "closed", "head": "o:feat", "base": "main"}

        http.responses.append(_json_resp(200, [{"number": 1}]))
        data = GitHubClient("t").list_pull_requests("o", "r")
        assert data == [{"number": 1}]
        assert http.calls[1]["params"] == {"state": "open"}

    def test_merge(self, http):
        http.responses.append(_json_resp(200, {"merged": True}))
        data = GitHubClient("t").merge_pull_request("o", "r", 7, "squash")
        assert data["merged"] is True
        call = http.calls[0]
        assert call["method"] == "PUT"
        assert call["url"].endswith("/pulls/7/merge")
        assert call["json"] == {"merge_method": "squash"}


class TestResolveToken:
    def test_prefers_app_installation_token(self, monkeypatch):
        monkeypatch.setattr(
            "services.github_app.get_cached_installation_token_for_config",
            lambda: "app-token")
        assert resolve_token(None) == "app-token"

    def test_app_unavailable_falls_back_to_binding(self, monkeypatch):
        from services.github_app import GitHubAppError

        def unavailable():
            raise GitHubAppError("not installed")
        monkeypatch.setattr(
            "services.github_app.get_cached_installation_token_for_config",
            unavailable)
        monkeypatch.setattr("services.github_client.decrypt_str",
                            lambda v: "binding-token")
        binding = SimpleNamespace(token_encrypted="cipher")
        assert resolve_token(binding) == "binding-token"

    def test_app_crash_also_falls_back(self, monkeypatch):
        def crash():
            raise RuntimeError("unexpected")
        monkeypatch.setattr(
            "services.github_app.get_cached_installation_token_for_config",
            crash)
        binding = SimpleNamespace(token_encrypted=None)
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        assert resolve_token(binding) == "env-token"

    def test_binding_decrypt_failure_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(
            "services.github_app.get_cached_installation_token_for_config",
            lambda: (_ for _ in ()).throw(GitHubClientError("x")))
        monkeypatch.setattr("services.github_client.decrypt_str", lambda v: None)
        binding = SimpleNamespace(token_encrypted="cipher")
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        assert resolve_token(binding) == "env-token"

    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "services.github_app.get_cached_installation_token_for_config",
            lambda: (_ for _ in ()).throw(GitHubClientError("x")))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert resolve_token(None) is None

    def test_prefer_app_false_skips_app(self, monkeypatch):
        called = {"app": False}

        def app():
            called["app"] = True
            return "app-token"
        monkeypatch.setattr(
            "services.github_app.get_cached_installation_token_for_config", app)
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        assert resolve_token(None, prefer_app=False) == "env-token"
        assert called["app"] is False
