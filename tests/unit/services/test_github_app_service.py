"""GitHub App 服务（services/github_app.py）缺口补测。

补齐：加密 legacy/空值/解密失败回退、App JWT 生成（真实 RSA）、
installation token 请求/缓存刷新边距、manifest 兑换、按配置取 token、
upsert 的 account_login 字段。
"""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import GitHubAppConfig, db
from services import github_app as gh


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
    gh.clear_installation_token_cache()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def rsa_private_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class TestEncryptDecrypt:
    def test_roundtrip_uses_v1_prefix(self):
        cipher = gh.encrypt_str("secret-value")
        assert cipher.startswith("v1:")
        assert gh.decrypt_str(cipher) == "secret-value"

    def test_encrypt_non_tuple_result(self, monkeypatch):
        manager = SimpleNamespace(encrypt=lambda v: "plain-cipher")
        monkeypatch.setattr(gh, "get_secret_encryption", lambda: manager)
        assert gh.encrypt_str("x") == "plain-cipher"

    def test_decrypt_empty_returns_none(self):
        assert gh.decrypt_str("") is None
        assert gh.decrypt_str(None) is None

    def test_decrypt_legacy_value_failure_returns_none(self):
        # 无 v1 前缀的坏密文：按无 key_id 解密，失败回退 None
        assert gh.decrypt_str("not-a-valid-cipher") is None


class TestAppJwt:
    def test_generates_three_segment_rs256(self, rsa_private_pem):
        token = gh.generate_app_jwt("99", rsa_private_pem)
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64decode(header_b64))
        payload = json.loads(_b64decode(payload_b64))
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert payload["iss"] == "99"
        assert payload["exp"] - payload["iat"] == 660

    def test_invalid_key_raises(self):
        with pytest.raises(gh.GitHubAppError, match="invalid private key"):
            gh.generate_app_jwt("99", "not-a-key")


class TestInstallationToken:
    def test_request_success_parses_expiry(self, monkeypatch, rsa_private_pem):
        expires_at = "2026-09-30T12:00:00Z"
        resp = SimpleNamespace(status_code=201, text="",
                               json=lambda: {"token": "ghs_tok",
                                             "expires_at": expires_at})
        monkeypatch.setattr(gh, "generate_app_jwt", lambda a, k: "fake-jwt")
        monkeypatch.setattr(gh.requests, "post",
                            MagicMock(return_value=resp))

        token, exp = gh._request_installation_token("99", rsa_private_pem, "42")
        assert token == "ghs_tok"
        assert exp == pytest.approx(
            __import__("datetime").datetime.fromisoformat(
                "2026-09-30T12:00:00+00:00").timestamp())

    def test_request_failure_raises(self, monkeypatch, rsa_private_pem):
        resp = SimpleNamespace(status_code=404, text="not found")
        monkeypatch.setattr(gh, "generate_app_jwt", lambda a, k: "fake-jwt")
        monkeypatch.setattr(gh.requests, "post", MagicMock(return_value=resp))
        with pytest.raises(gh.GitHubAppError, match="installation token"):
            gh._request_installation_token("99", rsa_private_pem, "42")

    def test_bad_or_missing_expiry_uses_default(self, monkeypatch, rsa_private_pem):
        resp = SimpleNamespace(status_code=201, text="",
                               json=lambda: {"token": "t", "expires_at": "junk"})
        monkeypatch.setattr(gh, "generate_app_jwt", lambda a, k: "fake-jwt")
        monkeypatch.setattr(gh.requests, "post", MagicMock(return_value=resp))
        _, exp = gh._request_installation_token("99", rsa_private_pem, "42")
        assert exp == pytest.approx(time.time() + 600, abs=30)

    def test_get_installation_token_returns_token_only(self, monkeypatch, rsa_private_pem):
        monkeypatch.setattr(gh, "_request_installation_token",
                            lambda a, k, i, t=15: ("tok-x", 123))
        assert gh.get_installation_token("99", rsa_private_pem, "42") == "tok-x"


class TestCachedToken:
    def test_cache_hit_avoids_second_request(self, monkeypatch, rsa_private_pem):
        calls = {"n": 0}

        def fake_request(app_id, key, inst, timeout=15):
            calls["n"] += 1
            return (f"tok-{calls['n']}", time.time() + 900)
        monkeypatch.setattr(gh, "_request_installation_token", fake_request)

        first = gh.get_cached_installation_token("99", rsa_private_pem, "42")
        second = gh.get_cached_installation_token("99", rsa_private_pem, "42")
        assert first == second == "tok-1"
        assert calls["n"] == 1

    def test_refresh_within_margin(self, monkeypatch, rsa_private_pem):
        calls = {"n": 0}

        def fake_request(app_id, key, inst, timeout=15):
            calls["n"] += 1
            # 剩余 100s < 5 分钟刷新边距 → 每次都重新取
            return (f"tok-{calls['n']}", time.time() + 100)
        monkeypatch.setattr(gh, "_request_installation_token", fake_request)

        gh.get_cached_installation_token("99", rsa_private_pem, "42")
        second = gh.get_cached_installation_token("99", rsa_private_pem, "42")
        assert second == "tok-2"
        assert calls["n"] == 2


class TestManifestAndConfig:
    def test_exchange_manifest_success(self, monkeypatch):
        resp = SimpleNamespace(status_code=201,
                               json=lambda: {"app_id": 88, "slug": "my-app"},
                               text="")
        monkeypatch.setattr(gh.requests, "post", MagicMock(return_value=resp))
        data = gh.exchange_manifest_code("one-time-code")
        assert data["slug"] == "my-app"

    def test_exchange_manifest_failure(self, monkeypatch):
        resp = SimpleNamespace(status_code=422, text="bad code")
        monkeypatch.setattr(gh.requests, "post", MagicMock(return_value=resp))
        with pytest.raises(gh.GitHubAppError, match="manifest conversion"):
            gh.exchange_manifest_code("bad")

    def test_for_config_success_delegates(self, monkeypatch):
        monkeypatch.setattr(gh, "get_app_config", lambda: {
            "installed": True, "app_id": "99", "slug": "s",
            "private_key": "key", "installation_id": "42",
            "webhook_secret": None, "account_login": None})
        monkeypatch.setattr(gh, "get_cached_installation_token",
                            lambda a, k, i: "cfg-token")
        assert gh.get_cached_installation_token_for_config() == "cfg-token"

    def test_for_config_incomplete_raises(self, monkeypatch):
        monkeypatch.setattr(gh, "get_app_config", lambda: {
            "installed": False, "app_id": None, "slug": None,
            "private_key": None, "installation_id": None,
            "webhook_secret": None, "account_login": None})
        with pytest.raises(gh.GitHubAppError, match="incomplete"):
            gh.get_cached_installation_token_for_config()


    def test_for_config_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(gh, "get_app_config", lambda: None)
        with pytest.raises(gh.GitHubAppError, match="not configured"):
            gh.get_cached_installation_token_for_config()


class TestWebhookSecret:
    def test_config_secret_wins(self, monkeypatch):
        monkeypatch.setattr(gh, "get_app_config", lambda: {
            "webhook_secret": "from-config"})
        monkeypatch.delenv("GITHUB_APP_WEBHOOK_SECRET", raising=False)
        assert gh.get_webhook_secret() == "from-config"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setattr(gh, "get_app_config", lambda: None)
        monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "from-env")
        assert gh.get_webhook_secret() == "from-env"

    def test_none_when_neither(self, monkeypatch):
        monkeypatch.setattr(gh, "get_app_config", lambda: None)
        monkeypatch.delenv("GITHUB_APP_WEBHOOK_SECRET", raising=False)
        assert gh.get_webhook_secret() is None


class TestUpsertConfig:
    def test_secret_fields_encrypted_and_installed_flag(self):
        row = gh.upsert_app_config({
            "app_id": 55, "private_key": "-----BEGIN KEY-----",
            "webhook_secret": "whsec", "installed": True,
        })
        assert row.private_key_encrypted.startswith("v1:")
        assert row.webhook_secret_encrypted.startswith("v1:")
        assert row.installed is True
        assert gh.decrypt_str(row.private_key_encrypted) == "-----BEGIN KEY-----"

    def test_account_login_written(self):
        row = gh.upsert_app_config({
            "app_id": 123, "slug": "todo-app", "installation_id": 42,
            "account_login": "todo-for-ai[bot]",
        })
        assert row.account_login == "todo-for-ai[bot]"
        assert row.app_id == "123"
        again = GitHubAppConfig.query.filter_by(id=1).first()
        assert again.slug == "todo-app"
