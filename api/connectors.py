"""外部连接器端点（Phase 4 互操作写回侧）

- GET  /workspaces/<ws>/connectors                    连接器列表（secret 不回显）
- PUT  /workspaces/<ws>/connectors/<provider>         配置（secret 加密落库，写审计）
- POST /connectors/linear/<ws>/ingest                 Linear webhook 入站（HMAC 验签 fail-closed）
"""

from flask import Blueprint, request

from models import ExternalConnectorConfig, db
from core.auth import get_current_user, unified_auth_required
from .agent_common import ensure_workspace_manage_access, get_workspace_or_404, write_agent_audit
from .base import ApiResponse, validate_json_request
from services.connectors import (
    get_connector,
    ingest_gitlab,
    ingest_linear,
    list_connectors,
    upsert_connector,
    verify_gitlab_token,
    verify_linear_signature,
)

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
        optional_fields=['enabled', 'secret', 'default_project_id'],
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
