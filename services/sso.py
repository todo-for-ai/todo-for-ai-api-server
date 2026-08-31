"""工作区 SSO 服务（Phase 4 企业能力）

配置化单点登录骨架：
- OIDC：授权 URL 构造（state 经 itsdangerous 签名防 CSRF）→ code 换 token
  → userinfo → 平台账号 find-or-create → 平台 JWT。token/userinfo 的 HTTP
  调用可注入（exchange_code 的 http_client 参数），测试无需真实 IdP。
- SAML：配置字段已预留，登录链路返回未实现（SAMLNotImplemented）。

client_secret 经既有 secret 加密机制存储（services.github_app.encrypt_str）。
"""

import secrets
from typing import Any, Dict, Optional

import structlog

from models import User, WorkspaceSSOConfig, db
from services.github_app import decrypt_str, encrypt_str

logger = structlog.get_logger()

STATE_SALT = 'sso-state-v1'
STATE_MAX_AGE_SECONDS = 300


class SAMLNotImplemented(NotImplementedError):
    """兼容占位：SAML 登录链路已实现，不再抛出（保留异常名以防外部引用）。"""


def get_config(workspace_id: int) -> Optional[WorkspaceSSOConfig]:
    return WorkspaceSSOConfig.query.filter_by(workspace_id=workspace_id).first()


def upsert_config(workspace_id: int, data: Dict[str, Any]) -> WorkspaceSSOConfig:
    """创建/更新 SSO 配置；client_secret 只写密文。"""
    config = get_config(workspace_id)
    if not config:
        config = WorkspaceSSOConfig(workspace_id=workspace_id)
        db.session.add(config)

    for field in ('provider', 'issuer', 'client_id', 'authorize_url',
                  'token_url', 'userinfo_url', 'redirect_uri',
                  'idp_metadata_url', 'idp_entity_id', 'default_role'):
        if field in data and data[field] is not None:
            setattr(config, field, str(data[field]).strip())
    if 'enabled' in data:
        config.enabled = bool(data['enabled'])
    secret = data.get('client_secret')
    if secret:
        config.client_secret_encrypted = encrypt_str(str(secret))

    db.session.commit()
    logger.info("sso.config_upserted", workspace_id=workspace_id, provider=config.provider)
    return config


# ── OIDC 登录链路 ──

def _state_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer

    secret = current_app.config.get('SECRET_KEY') or 'todo4ai-sso-dev-secret'
    return URLSafeTimedSerializer(secret_key=secret, salt=STATE_SALT)


def build_oidc_state(workspace_id: int) -> str:
    return _state_serializer().dumps({'ws': workspace_id, 'nonce': secrets.token_urlsafe(8)})


def verify_oidc_state(state: str) -> int:
    """校验 state 签名与时效，返回 workspace_id；非法/过期抛 itsdangerous 异常。"""
    payload = _state_serializer().loads(state, max_age=STATE_MAX_AGE_SECONDS)
    return int(payload['ws'])


def build_oidc_authorize_url(config: WorkspaceSSOConfig, state: str,
                             redirect_uri: Optional[str] = None) -> str:
    from urllib.parse import urlencode

    query = urlencode({
        'response_type': 'code',
        'client_id': config.client_id or '',
        'redirect_uri': redirect_uri or config.redirect_uri or '',
        'scope': 'openid email profile',
        'state': state,
    })
    separator = '&' if '?' in (config.authorize_url or '') else '?'
    return f"{config.authorize_url}{separator}{query}"


def exchange_code(config: WorkspaceSSOConfig, code: str,
                  http_client=None) -> Dict[str, Any]:
    """OIDC code 换 token + userinfo。

    http_client 需实现 post(url, data=...) 与 get(url, headers=...)（httpx 兼容），
    测试可注入假客户端。
    """
    import httpx as _httpx

    client = http_client or _httpx.Client(timeout=10)
    try:
        client_secret = decrypt_str(config.client_secret_encrypted) or ''
        token_resp = client.post(config.token_url, data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': config.redirect_uri or '',
            'client_id': config.client_id or '',
            'client_secret': client_secret,
        })
        token_resp.raise_for_status()
        access_token = (token_resp.json() or {}).get('access_token')
        if not access_token:
            raise ValueError('token endpoint returned no access_token')

        userinfo_resp = client.get(
            config.userinfo_url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json() or {}
    finally:
        if http_client is None:
            client.close()

    return userinfo


def find_or_create_user(userinfo: Dict[str, Any]) -> User:
    """按 email 找/建平台账号（SSO 用户无平台密码）。"""
    import uuid

    email = str(userinfo.get('email') or '').strip().lower()
    if not email:
        raise ValueError('userinfo missing email')

    user = User.query.filter_by(email=email).first()
    if user:
        return user

    name = str(userinfo.get('name') or email.split('@')[0])[:64]
    user = User(
        username=f"sso_{name}_{uuid.uuid4().hex[:6]}",
        email=email,
    )
    user.password_hash = secrets.token_urlsafe(32)  # 不可用密码占位
    db.session.add(user)
    db.session.commit()
    logger.info("sso.user_created", user_id=user.id)
    return user


def login_oidc(workspace_id: int, code: str, state: str,
               http_client=None) -> Dict[str, Any]:
    """回调链路：验证 state → 换 userinfo → find-or-create → 返回账号信息。

    平台 JWT 签发由 API 层完成（需 app 上下文的 create_access_token）。
    """
    verified_ws = verify_oidc_state(state)
    if verified_ws != workspace_id:
        raise ValueError('state workspace mismatch')

    config = get_config(workspace_id)
    if not config or config.provider != WorkspaceSSOConfig.PROVIDER_OIDC or not config.enabled:
        raise ValueError('SSO not enabled for this workspace')

    userinfo = exchange_code(config, code, http_client=http_client)
    user = find_or_create_user(userinfo)

    from flask_jwt_extended import create_access_token
    token = create_access_token(identity=str(user.id))
    return {
        'user': user.to_public_dict(),
        'access_token': token,
    }


# ── SAML 登录链路（Phase 4 企业能力）──

def build_saml_login(workspace_id: int, state: str) -> Dict[str, Any]:
    """SP-initiated：构造 AuthnRequest + Redirect Binding 跳转地址。

    state 应为 verify_oidc_state 可校验的签名串（含 ws 与 request_id）。
    """
    from services.saml import build_authn_request, build_saml_redirect, fetch_idp_metadata

    config = get_config(workspace_id)
    if not config or config.provider != WorkspaceSSOConfig.PROVIDER_SAML or not config.enabled:
        raise ValueError('SAML not enabled for this workspace')

    payload = _state_serializer().loads(state, max_age=STATE_MAX_AGE_SECONDS)
    request_id = payload.get('request_id')
    if not request_id:
        raise ValueError('state missing request_id')

    metadata = fetch_idp_metadata(config.idp_metadata_url)
    authn_request = build_authn_request(
        sp_entity_id=config.issuer or '',
        acs_url=config.redirect_uri or '',
        request_id=request_id,
    )
    redirect_url = build_saml_redirect(metadata['sso_url'], authn_request, state)
    return {'redirect_url': redirect_url, 'request_id': request_id}


def make_saml_state(workspace_id: int) -> str:
    """SAML state：签名携带工作区与本次 AuthnRequest 的 request_id。"""
    from services.saml import new_request_id

    request_id = new_request_id()
    return _state_serializer().dumps({
        'ws': workspace_id, 'request_id': request_id, 'nonce': secrets.token_urlsafe(8),
    })


def login_saml(workspace_id: int, saml_response_b64: str, relay_state: str,
               http_client=None) -> Dict[str, Any]:
    """SAML 回调链路：state 校验 → 断言校验（签名/时间窗/audience）→ JWT。"""
    from services.saml import fetch_idp_metadata, verify_saml_response

    payload = _state_serializer().loads(relay_state, max_age=STATE_MAX_AGE_SECONDS)
    if int(payload.get('ws', -1)) != workspace_id:
        raise ValueError('state workspace mismatch')
    request_id = payload.get('request_id')

    config = get_config(workspace_id)
    if not config or config.provider != WorkspaceSSOConfig.PROVIDER_SAML or not config.enabled:
        raise ValueError('SAML not enabled for this workspace')

    metadata = fetch_idp_metadata(config.idp_metadata_url, http_client=http_client)
    userinfo = verify_saml_response(
        saml_response_b64,
        idp_entity_id=config.idp_entity_id,
        sp_entity_id=config.issuer or '',
        certificate_pem=metadata['certificate_pem'],
        request_id=request_id,
    )
    user = find_or_create_user({
        'email': userinfo['email'],
        'name': userinfo.get('name') or userinfo['email'].split('@')[0],
    })

    from flask_jwt_extended import create_access_token
    token = create_access_token(identity=str(user.id))
    return {
        'user': user.to_public_dict(),
        'access_token': token,
    }
