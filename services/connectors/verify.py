"""Webhook 验签：全部常量时间比较，fail-closed。"""

import hmac


def verify_linear_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Linear webhook 验签：HMAC-SHA256 hex，常量时间比较。"""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, 'sha256').hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_gitlab_token(token: str, secret: str) -> bool:
    """GitLab webhook 验签：X-GitLab-Token 明文令牌，常量时间比较。"""
    if not token or not secret:
        return False
    return hmac.compare_digest(str(token), str(secret))


def verify_jira_token(token: str, secret: str) -> bool:
    """Jira webhook 验签：配置令牌常量时间比较（header/query 均可携带）。"""
    if not token or not secret:
        return False
    return hmac.compare_digest(str(token), str(secret))
