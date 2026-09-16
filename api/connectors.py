"""外部连接器端点（Phase 4 互操作写回侧）

- GET  /workspaces/<ws>/connectors                    连接器列表（secret 不回显）
- PUT  /workspaces/<ws>/connectors/<provider>         配置（secret 加密落库，写审计）
- POST /connectors/linear/<ws>/ingest                 Linear webhook 入站（HMAC 验签 fail-closed）
- POST /connectors/lark/<ws>/ingest                   飞书事件订阅（url_verification 握手 + im.message.receive_v1）
- GET  /connectors/wecom/<ws>/callback                企业微信回调验证（echostr 解密回明文）
- POST /connectors/wecom/<ws>/callback                企业微信消息回调（SHA1+AES 官方协议）
- POST /connectors/generic/<ws>/ingest                通用 Webhook 入站（X-Todo4AI-Token + 字段映射）
"""

from flask import Blueprint, request

from models import ExternalConnectorConfig, db
from core.auth import get_current_user, unified_auth_required
from .agent_common import ensure_workspace_manage_access, get_workspace_or_404, write_agent_audit
from .base import ApiResponse, validate_json_request
from services.connectors import (
    get_connector,
    ingest_generic,
    ingest_gitlab,
    ingest_jira,
    ingest_lark,
    ingest_linear,
    ingest_wecom_message,
    list_connectors,
    upsert_connector,
    verify_generic_token,
    verify_gitlab_token,
    verify_jira_token,
    verify_lark_token,
    verify_linear_signature,
    verify_wecom_callback,
)
from services import wecom_crypto

connectors_bp = Blueprint('connectors', __name__)


@connectors_bp.route('/workspaces/<int:workspace_id>/connectors', methods=['GET'])
@unified_auth_required
def list_connectors_view(workspace_id: int):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    items = list_connectors(workspace_id)
    return ApiResponse.success(data={
        'connectors': [item.to_dict() for item in items],
    }).to_response()


@connectors_bp.route('/workspaces/<int:workspace_id>/connectors/<provider>', methods=['PUT'])
@unified_auth_required
def configure_connector(workspace_id: int, provider: str):
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err
    if provider not in ExternalConnectorConfig.PROVIDERS:
        return ApiResponse.error(
            f"provider must be one of {list(ExternalConnectorConfig.PROVIDERS)}", 400,
        ).to_response()

    data = validate_json_request(
        optional_fields=['enabled', 'secret', 'default_project_id', 'config_json'],
    )
    if isinstance(data, tuple):
        return data
    if 'default_project_id' in data and data['default_project_id']:
        from models import Project
        project = db.session.get(Project, int(data['default_project_id']))
        if not project or project.organization_id != workspace_id:
            return ApiResponse.error('default_project_id not in this workspace', 400).to_response()

    config = upsert_connector(workspace_id, provider, data)
    write_agent_audit(
        event_type='connector.configured',
        actor_type='user',
        actor_id=user.id,
        target_type='connector',
        target_id=config.id,
        workspace_id=workspace_id,
        payload={'provider': provider, 'enabled': bool(config.enabled)},
        risk_score=20,
    )
    return ApiResponse.success(
        data={'connector': config.to_dict()},
        message='Connector configured',
    ).to_response()


@connectors_bp.route('/connectors/linear/<int:workspace_id>/ingest', methods=['POST'])
def linear_ingest(workspace_id: int):
    """Linear webhook 入站：验签（fail-closed）→ 任务/评论导入。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_LINEAR)
    if not config or not config.enabled:
        return ApiResponse.error('linear connector not enabled', 400).to_response()

    from services.github_app import decrypt_str
    secret = decrypt_str(config.secret_encrypted) or ''
    signature = request.headers.get('Linear-Signature', '')
    if not verify_linear_signature(request.get_data(), signature, secret):
        return ApiResponse.error('invalid signature', 401).to_response()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ApiResponse.error('invalid JSON payload', 400).to_response()

    try:
        result = ingest_linear(workspace_id, payload)
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()

    return ApiResponse.success(data=result, message='Ingest processed').to_response()


@connectors_bp.route('/connectors/gitlab/<int:workspace_id>/ingest', methods=['POST'])
def gitlab_ingest(workspace_id: int):
    """GitLab webhook 入站：X-GitLab-Token 常量时间校验（fail-closed）→ 任务/评论导入。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_GITLAB)
    if not config or not config.enabled:
        return ApiResponse.error('gitlab connector not enabled', 400).to_response()

    from services.github_app import decrypt_str
    secret = decrypt_str(config.secret_encrypted) or ''
    token = request.headers.get('X-GitLab-Token', '')
    if not verify_gitlab_token(token, secret):
        return ApiResponse.error('invalid token', 401).to_response()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ApiResponse.error('invalid JSON payload', 400).to_response()

    try:
        result = ingest_gitlab(workspace_id, payload)
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()

    return ApiResponse.success(data=result, message='Ingest processed').to_response()


@connectors_bp.route('/connectors/jira/<int:workspace_id>/ingest', methods=['POST'])
def jira_ingest(workspace_id: int):
    """Jira webhook 入站：配置令牌常量时间校验（X-Todo4AI-Token 头或 ?token= query）→ 任务/评论导入。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_JIRA)
    if not config or not config.enabled:
        return ApiResponse.error('jira connector not enabled', 400).to_response()

    from services.github_app import decrypt_str
    secret = decrypt_str(config.secret_encrypted) or ''
    token = request.headers.get('X-Todo4AI-Token') or request.args.get('token', '')
    if not verify_jira_token(token, secret):
        return ApiResponse.error('invalid token', 401).to_response()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ApiResponse.error('invalid JSON payload', 400).to_response()

    try:
        result = ingest_jira(workspace_id, payload)
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()

    return ApiResponse.success(data=result, message='Ingest processed').to_response()


# ---------------------------------------------------------------------------
# 飞书（Lark）：事件订阅入站
# ---------------------------------------------------------------------------

@connectors_bp.route('/connectors/lark/<int:workspace_id>/ingest', methods=['POST'])
def lark_ingest(workspace_id: int):
    """飞书事件订阅入口：url_verification 握手回 challenge；
    im.message.receive_v1 文本消息 → 建任务（群路由）+ 群卡片回执。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_LARK)
    if not config or not config.enabled:
        return ApiResponse.error('lark connector not enabled', 400).to_response()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ApiResponse.error('invalid JSON payload', 400).to_response()

    # url_verification 不带事件头时也允许 challenge 握手（验 token 可选：
    # 官方握手体带 token，业务事件必须验）
    if payload.get('type') != 'url_verification':
        header = payload.get('header') or {}
        if not verify_lark_token(config, header.get('token')):
            return ApiResponse.error('invalid token', 401).to_response()
    else:
        token = payload.get('token') or ''
        if token and not verify_lark_token(config, token):
            return ApiResponse.error('invalid token', 401).to_response()

    try:
        result = ingest_lark(workspace_id, config, payload)
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()
    db.session.commit()

    if result.get('action') == 'challenge':
        return ApiResponse.success(data={'challenge': result.get('challenge')},
                                   message='challenge accepted').to_response()
    return ApiResponse.success(data=result, message='Ingest processed').to_response()


# ---------------------------------------------------------------------------
# 企业微信（WeCom）：回调验证 + 消息入站
# ---------------------------------------------------------------------------

@connectors_bp.route('/connectors/wecom/<int:workspace_id>/callback', methods=['GET'])
def wecom_verify(workspace_id: int):
    """企业微信回调 URL 验证（GET）：验签 + 解密 echostr，回发明文。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_WECOM)
    if not config or not config.enabled:
        return ApiResponse.error('wecom connector not enabled', 400).to_response()
    try:
        result = verify_wecom_callback(config, {
            'msg_signature': request.args.get('msg_signature', ''),
            'timestamp': request.args.get('timestamp', ''),
            'nonce': request.args.get('nonce', ''),
            'echostr': request.args.get('echostr', ''),
        })
    except wecom_crypto.WeComCryptoError as e:
        return ApiResponse.error(str(e), 401).to_response()
    if result.get('action') != 'echo':
        return ApiResponse.error('expected echo verification', 400).to_response()
    return result['plain'], 200, {'Content-Type': 'text/plain; charset=utf-8'}


@connectors_bp.route('/connectors/wecom/<int:workspace_id>/callback', methods=['POST'])
def wecom_ingest(workspace_id: int):
    """企业微信消息回调（POST）：验签解密 → 文本消息建任务 + 应用消息回执。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_WECOM)
    if not config or not config.enabled:
        return ApiResponse.error('wecom connector not enabled', 400).to_response()
    body = request.get_data(as_text=True)
    try:
        result = verify_wecom_callback(config, {
            'msg_signature': request.args.get('msg_signature', ''),
            'timestamp': request.args.get('timestamp', ''),
            'nonce': request.args.get('nonce', ''),
        }, body_xml=body)
    except wecom_crypto.WeComCryptoError as e:
        return ApiResponse.error(str(e), 401).to_response()
    if result.get('action') != 'message':
        return ApiResponse.error('expected message callback', 400).to_response()

    try:
        processed = ingest_wecom_message(workspace_id, config, result['fields'])
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()
    db.session.commit()
    return ApiResponse.success(data=processed, message='Ingest processed').to_response()


# ---------------------------------------------------------------------------
# 通用 Webhook：任意内部系统接入
# ---------------------------------------------------------------------------

@connectors_bp.route('/connectors/generic/<int:workspace_id>/ingest', methods=['POST'])
def generic_ingest(workspace_id: int):
    """通用 Webhook 入站：X-Todo4AI-Token 常量时间校验 + mapping 字段映射 → 建任务。"""
    config = get_connector(workspace_id, ExternalConnectorConfig.PROVIDER_GENERIC)
    if not config or not config.enabled:
        return ApiResponse.error('generic connector not enabled', 400).to_response()

    token = request.headers.get('X-Todo4AI-Token') or request.args.get('token', '')
    if not verify_generic_token(config, token):
        return ApiResponse.error('invalid token', 401).to_response()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ApiResponse.error('invalid JSON payload', 400).to_response()

    try:
        result = ingest_generic(workspace_id, config, payload,
                                request_id=request.headers.get('X-Request-ID', ''))
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()
    db.session.commit()
    return ApiResponse.success(data=result, message='Ingest processed').to_response()
