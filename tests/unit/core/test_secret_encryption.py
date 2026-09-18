"""Secret 加密管理（core/secret_encryption.py）单元回归。

覆盖：密钥管理器单例、环境加载（主键必填/格式校验/历史密钥与坏键跳过）、
加密解密（指定密钥/全密钥回退/损坏密文/密钥全丢）、密钥轮换（历史槽位
顺序填充/坏新键拒绝）、KMS 框架（Vault transit 加解密/不支持提供商/
未实现分支/无 client/各云 SDK 缺失降级）、get_secret_encryption 降级链。
"""

import base64
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from core import secret_encryption as se
from core.secret_encryption import (
    EncryptionKeyManager,
    KMSSecretEncryption,
    SecretDecryptionError,
    SecretEncryptionError,
    get_secret_encryption,
)


@pytest.fixture(autouse=True)
def _fresh_manager(monkeypatch):
    """每个用例独立的管理器实例与独立密钥环境。"""
    se.reset_encryption_manager()
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("SECRET_KMS_PROVIDER", raising=False)
    for i in range(1, 5):
        monkeypatch.delenv(f"SECRET_ENCRYPTION_KEY_V{i}", raising=False)
    yield
    se.reset_encryption_manager()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


class TestKeyManagerLoading:
    def test_missing_primary_key_raises(self, monkeypatch):
        monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
        with pytest.raises(SecretEncryptionError, match="SECRET_ENCRYPTION_KEY"):
            EncryptionKeyManager()

    def test_invalid_key_format_raises(self, monkeypatch):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "not-a-fernet-key")
        with pytest.raises(SecretEncryptionError, match="Invalid SECRET_ENCRYPTION_KEY"):
            EncryptionKeyManager()

    def test_loads_historical_keys(self, monkeypatch):
        v1, v2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY_V1", v1)
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY_V2", v2)
        manager = EncryptionKeyManager()
        info = manager.get_key_info()
        assert set(info["available_keys"]) >= {"primary", "v1", "v2"}
        assert info["current_key_id"] == "primary"
        assert info["key_count"] >= 3

    def test_invalid_historical_key_logged_and_skipped(self, monkeypatch):
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY_V1", "bad-key-format")
        manager = EncryptionKeyManager()
        assert "v1" not in manager.get_key_info()["available_keys"]
        assert manager.decrypt(
            manager.encrypt("x", key_id="primary"), key_id="primary") == "x"

    def test_singleton_returns_same_instance(self):
        assert EncryptionKeyManager() is EncryptionKeyManager()

    def test_get_key_by_id_unknown_returns_none(self):
        assert EncryptionKeyManager().get_key_by_id("v999") is None

    def test_get_current_key_without_active_raises(self):
        manager = EncryptionKeyManager.__new__(EncryptionKeyManager)
        manager._keys = {}
        manager._current_key_id = None
        with pytest.raises(SecretEncryptionError, match="No active encryption key"):
            manager.get_current_key()


class TestEncryptDecrypt:
    def test_roundtrip_with_key_id(self):
        manager = EncryptionKeyManager()
        ciphertext, key_id = manager.encrypt("hello", key_id="primary")
        assert key_id == "primary"
        assert ciphertext != "hello"
        assert manager.decrypt(ciphertext, key_id=key_id) == "hello"

    def test_encrypt_unknown_key_raises(self):
        with pytest.raises(SecretEncryptionError, match="not found"):
            EncryptionKeyManager().encrypt("x", key_id="v999")

    def test_encrypt_exception_wrapped(self, monkeypatch):
        manager = EncryptionKeyManager()
        monkeypatch.setattr(
            manager.get_current_key()["fernet"], "encrypt",
            lambda v: (_ for _ in ()).throw(RuntimeError("fernet down")))
        with pytest.raises(SecretEncryptionError, match="Encryption failed"):
            manager.encrypt("x")

    def test_decrypt_bad_base64_raises(self):
        with pytest.raises(SecretDecryptionError, match="Invalid ciphertext format"):
            EncryptionKeyManager().decrypt("!!!not-base64!!!")

    def test_decrypt_with_unknown_key_id_raises(self):
        manager = EncryptionKeyManager()
        ciphertext, _ = manager.encrypt("hello")
        with pytest.raises(SecretDecryptionError, match="Decryption key 'v999' not found"):
            manager.decrypt(ciphertext, key_id="v999")

    def test_decrypt_wrong_key_with_key_id_raises_invalid_token(self):
        manager = EncryptionKeyManager()
        ciphertext, _ = manager.encrypt("hello")
        manager.rotate_key(Fernet.generate_key().decode())
        with pytest.raises(SecretDecryptionError, match="Invalid token"):
            manager.decrypt(ciphertext, key_id="primary")

    def test_decrypt_without_key_id_tries_all_keys(self):
        manager = EncryptionKeyManager()
        ciphertext, _ = manager.encrypt("hello", key_id="primary")
        manager.rotate_key(Fernet.generate_key().decode())
        # 未指定 key_id：全密钥尝试 → 旧密钥命中
        assert manager.decrypt(ciphertext) == "hello"

    def test_decrypt_no_valid_key_raises(self):
        manager = EncryptionKeyManager()
        ciphertext, key_id = manager.encrypt("hello", key_id="primary")
        # 摘掉所有已知密钥（模拟密钥全部丢失的极端场景）
        manager._keys.clear()
        manager._current_key_id = None
        with pytest.raises(SecretDecryptionError, match="no valid key found"):
            manager.decrypt(ciphertext)


class TestRotateKey:
    def test_rotate_moves_primary_to_history_slot(self):
        manager = EncryptionKeyManager()
        old_key = manager.get_current_key()["key"].decode()
        old_id = manager.rotate_key(Fernet.generate_key().decode())
        assert old_id == "primary"
        assert manager.get_key_by_id("v1")["key"].decode() == old_key
        assert manager.get_current_key()["key"].decode() != old_key

    def test_rotate_invalid_new_key_raises(self):
        with pytest.raises(SecretEncryptionError, match="Invalid new key format"):
            EncryptionKeyManager().rotate_key("bad-key")

    def test_rotate_fills_history_slots_in_order(self):
        manager = EncryptionKeyManager()
        for _ in range(3):
            manager.rotate_key(Fernet.generate_key().decode())
        assert {"v1", "v2", "v3"} <= set(manager.get_key_info()["available_keys"])


class TestKMSSecretEncryption:
    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported KMS provider"):
            KMSSecretEncryption("alibaba", {})

    def test_vault_transit_roundtrip(self):
        kms = KMSSecretEncryption("hashicorp_vault",
                                  {"url": "http://v", "token": "t",
                                   "key_name": "k"})
        captured = {}
        transit = SimpleNamespace(
            encrypt=lambda name, plaintext, mount_point: (
                captured.update(plaintext_b64=plaintext,
                                mount_point=mount_point),
                {"data": {"ciphertext": "vault:v1:ciph"}})[1],
            decrypt=lambda name, ciphertext, mount_point: {
                "data": {"plaintext": base64.b64encode(b"plain").decode()}})
        kms._client = SimpleNamespace(secrets=SimpleNamespace(transit=transit))
        kms._key_name = "k"
        kms._mount_point = "transit"

        cipher = kms.encrypt("plain")
        assert cipher == "vault:v1:ciph"
        assert captured["plaintext_b64"] == base64.b64encode(b"plain").decode()
        assert captured["mount_point"] == "transit"
        assert kms.decrypt(cipher) == "plain"

    def test_encrypt_without_client_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hvac", None)  # hvac 导入失败
        kms = KMSSecretEncryption("hashicorp_vault", {})
        assert kms._client is None
        with pytest.raises(SecretEncryptionError, match="not initialized"):
            kms.encrypt("x")
        with pytest.raises(SecretDecryptionError, match="not initialized"):
            kms.decrypt("x")

    def test_non_vault_not_implemented(self, monkeypatch):
        fake_boto3 = SimpleNamespace(
            client=lambda service, region_name=None: None)
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
        kms = KMSSecretEncryption("aws", {"region": "us-east-1",
                                          "key_id": "kid"})
        assert kms._key_id == "kid"
        with pytest.raises(NotImplementedError, match="aws"):
            kms.encrypt("x")
        with pytest.raises(NotImplementedError, match="aws"):
            kms.decrypt("x")

    def test_azure_gcp_sdk_missing_paths(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name.startswith(("azure", "google.cloud")):
                raise ImportError(f"no {name}")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        for provider, config in (("azure", {"vault_url": "https://v"}),
                                 ("gcp", {"key_name": "gcp-k"})):
            kms = KMSSecretEncryption(provider, config)
            assert kms._client is None


class TestGetSecretEncryption:
    def test_defaults_to_local_manager(self, monkeypatch):
        monkeypatch.delenv("SECRET_KMS_PROVIDER", raising=False)
        assert isinstance(get_secret_encryption(), EncryptionKeyManager)

    def test_kms_provider_env_selects_kms(self, monkeypatch):
        monkeypatch.setenv("SECRET_KMS_PROVIDER", "hashicorp_vault")
        monkeypatch.setenv("SECRET_VAULT_URL", "http://v")
        monkeypatch.setenv("SECRET_VAULT_TOKEN", "t")
        kms = get_secret_encryption()
        assert isinstance(kms, KMSSecretEncryption)

    def test_kms_client_missing_returns_kms_instance_anyway(self, monkeypatch):
        """hvac 缺失时 KMS 实例化不抛（client=None），行为钉子。"""
        monkeypatch.setenv("SECRET_KMS_PROVIDER", "hashicorp_vault")
        monkeypatch.setenv("SECRET_VAULT_URL", "http://v")
        monkeypatch.setenv("SECRET_VAULT_TOKEN", "t")

        def fake_init(self):
            self._client = None
        monkeypatch.setattr(KMSSecretEncryption, "_init_vault", fake_client_init)
        kms = get_secret_encryption()
        assert isinstance(kms, KMSSecretEncryption)
        assert kms._client is None
