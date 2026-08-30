"""
GitHub App 服务（GitHub App 化代码侧准备）

- Webhook HMAC SHA256 签名校验（X-Hub-Signature-256）
- App JWT（RS256，cryptography 签名，10 分钟有效期）
- Installation token 获取（替代单一 token 绑定的短期授权凭证）
"""

import base64
import hashlib
import hmac
import time
from typing import Optional

import requests

from core.secret_encryption import get_secret_encryption

GITHUB_API_BASE = "https://api.github.com"


class GitHubAppError(Exception):
    """GitHub App 流程错误。"""


def encrypt_str(plaintext: str) -> str:
    """加密为单字符串：key_id 编码进 v1 前缀，密钥轮换后仍可解密。"""
    result = get_secret_encryption().encrypt(plaintext)
    if isinstance(result, tuple):
        ciphertext, key_id = result
        return f"v1:{key_id}:{ciphertext}" if key_id else ciphertext
    return str(result)


def decrypt_str(value: str) -> Optional[str]:
    """解密 encrypt_str 产物；无前缀时按无 key_id 解密（兼容）。失败返回 None。"""
    if not value:
        return None
    manager = get_secret_encryption()
    if value.startswith("v1:"):
        _, key_id, ciphertext = value.split(":", 2)
        return manager.decrypt(ciphertext, key_id)
    try:
        return manager.decrypt(value)
    except Exception:
        return None


def verify_webhook_signature(payload_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    """校验 GitHub webhook 的 X-Hub-Signature-256（sha256=<hex>）。

    常量时间比较防时序攻击；header 缺失或格式非法一律 False。
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.split("=", 1)[1].strip().lower()
    return hmac.compare_digest(expected, provided)


def generate_app_jwt(app_id: str, private_key_pem: str) -> str:
    """生成 GitHub App JWT（RS256，iat-60s 起算，10 分钟过期）。

    直接用 cryptography 签名，避免引入额外 JWT 依赖。
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
    except Exception as e:
        raise GitHubAppError(f"invalid private key: {e}")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 600, "iss": str(app_id)}

    def _b64(segment: bytes) -> str:
        return base64.urlsafe_b64encode(segment).rstrip(b"=").decode()

    import json

    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


def get_installation_token(app_id: str, private_key_pem: str, installation_id: str,
                           timeout: int = 15) -> str:
    """用 App JWT 换取 installation token（约 1 小时有效）。"""
    jwt_token = generate_app_jwt(app_id, private_key_pem)
    resp = requests.post(
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Todo-for-AI-Server",
        },
        timeout=timeout,
    )
    if resp.status_code != 201:
        raise GitHubAppError(
            f"installation token request failed: {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()["token"]


def exchange_manifest_code(code: str, timeout: int = 15) -> dict:
    """GitHub App Manifest 流程：用一次性 code 换取 App 凭据配置。"""
    resp = requests.post(
        f"{GITHUB_API_BASE}/app-manifests/{code}/conversions",
        headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Todo-for-AI-Server",
        },
        timeout=timeout,
    )
    if resp.status_code != 201:
        raise GitHubAppError(
            f"manifest conversion failed: {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()


def get_app_config():
    """读取平台 GitHub App 配置（单例行），解密 secret 字段。

    返回 dict：{app_id, slug, installation_id, private_key, webhook_secret, installed}
    未配置返回 None。secret 解密失败按缺失处理（不抛出）。
    """
    from models import GitHubAppConfig

    row = GitHubAppConfig.query.filter_by(id=1).first()
    if not row:
        return None

    def _decrypt(cipher_text):
        return decrypt_str(cipher_text)

    return {
        "app_id": row.app_id,
        "slug": row.slug,
        "installation_id": row.installation_id,
        "account_login": row.account_login,
        "installed": bool(row.installed),
        "private_key": _decrypt(row.private_key_encrypted),
        "webhook_secret": _decrypt(row.webhook_secret_encrypted),
    }


def get_webhook_secret() -> Optional[str]:
    """webhook secret：App 配置优先，回退环境变量 GITHUB_APP_WEBHOOK_SECRET。"""
    import os

    config = get_app_config()
    if config and config.get("webhook_secret"):
        return config["webhook_secret"]
    return os.environ.get("GITHUB_APP_WEBHOOK_SECRET") or None


def upsert_app_config(credentials: dict) -> "GitHubAppConfig":
    """写入/更新 App 凭据（manifest conversion 或手动配置），secret 字段加密存储。"""
    from models import GitHubAppConfig, db

    row = GitHubAppConfig.query.filter_by(id=1).first()
    if not row:
        row = GitHubAppConfig(id=1, created_by="system:github-app")
        db.session.add(row)

    if credentials.get("app_id"):
        row.app_id = str(credentials["app_id"])
    if credentials.get("slug"):
        row.slug = credentials["slug"]
    if credentials.get("installation_id"):
        row.installation_id = str(credentials["installation_id"])
    if credentials.get("account_login"):
        row.account_login = credentials["account_login"]
    if credentials.get("private_key"):
        row.private_key_encrypted = encrypt_str(credentials["private_key"])
    if credentials.get("webhook_secret"):
        row.webhook_secret_encrypted = encrypt_str(credentials["webhook_secret"])
    if "installed" in credentials:
        row.installed = bool(credentials["installed"])

    db.session.commit()
    return row
