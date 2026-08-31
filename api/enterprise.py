"""工作区 SSO 与合规报告端点（Phase 4 企业能力）

SSO（配置化单点登录骨架）：
- GET  /workspaces/<id>/sso/config    查看（secret 不回显，仅 has_client_secret）
- PUT  /workspaces/<id>/sso/config    配置（client_secret 加密落库，写审计）
- POST /workspaces/<id>/sso/login     OIDC 生成授权 URL（state 签名防 CSRF）；SAML 未实现
- GET  /sso/callback/<id>             OIDC 回调：code 换 userinfo → 找/建账号 → 平台 JWT

合规报告：
- GET  /workspaces/<id>/compliance/report  时间窗汇总风险事件/审批滞留/预算超支/导出次数
"""

from datetime import datetime, timedelta

from flask import Blueprint, request
from sqlalchemy import func

from models import (
    AgentAuditEvent,
    AgentTaskEvent,
    WorkspaceSSOConfig,
    db,
)
from core.auth import get_current_user, unified_auth_required
from .agent_common import (
    ensure_workspace_manage_access,
    get_workspace_or_404,
    write_agent_audit,
)
from .base import ApiResponse, validate_json_request
from services.sso import (
    build_oidc_authorize_url,
    build_oidc_state,
    get_config,
    login_oidc,
    upsert_config,
)

enterprise_bp = Blueprint('enterprise', __name__)


def _require_manage(workspace_id: int):
    """返回 (user, workspace, error_response)；error 非 None 时直接返回。"""
    user = get_current_user()
    workspace, not_found = get_workspace_or_404(workspace_id)
    if not_found:
        return None, None, not_found
    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return None, None, access_err
    return user, workspace, None


# ── SSO 配置 ──

@enterprise_bp.route('/workspaces/<int:workspace_id>/sso/config', methods=['GET'])
@unified_auth_required
def get_sso_config(workspace_id: int):
    user, _, err = _require_manage(workspace_id)
    if err:
        return err
    config = get_config(workspace_id)
    return ApiResponse.success(data={
        'config': config.to_dict() if config else None,
    }).to_response()


@enterprise_bp.route('/workspaces/<int:workspace_id>/sso/config', methods=['PUT'])
@unified_auth_required
def put_sso_config(workspace_id: int):
    user, _, err = _require_manage(workspace_id)
    if err:
        return err
    data = validate_json_request(
        optional_fields=['provider', 'enabled', 'issuer', 'client_id', 'client_secret',
                         'authorize_url', 'token_url', 'userinfo_url', 'redirect_uri',
                         'idp_metadata_url', 'idp_entity_id', 'default_role'],
    )
    if isinstance(data, tuple):
        return data
    if 'provider' in data and data['provider'] not in WorkspaceSSOConfig.PROVIDERS:
        return ApiResponse.error(
            f"provider must be one of {list(WorkspaceSSOConfig.PROVIDERS)}", 400,
        ).to_response()

    config = upsert_config(workspace_id, data)
    write_agent_audit(
        event_type='sso.config_updated',
        actor_type='user',
        actor_id=user.id,
        target_type='workspace',
        target_id=workspace_id,
        workspace_id=workspace_id,
        payload={'provider': config.provider, 'enabled': bool(config.enabled)},
        risk_score=20,
    )
    return ApiResponse.success(data={'config': config.to_dict()}, message='SSO config saved').to_response()


# ── OIDC 登录链路（骨架）──

@enterprise_bp.route('/workspaces/<int:workspace_id>/sso/login', methods=['POST'])
def sso_login(workspace_id: int):
    """生成 IdP 授权 URL（未认证入口，无需平台 token）。"""
    config = get_config(workspace_id)
    if not config or not config.enabled:
        return ApiResponse.error('SSO not enabled for this workspace', 400).to_response()
    if config.provider == WorkspaceSSOConfig.PROVIDER_SAML:
        return ApiResponse.error('SAML login flow not implemented yet', 501).to_response()

    state = build_oidc_state(workspace_id)
    url = build_oidc_authorize_url(config, state)
    return ApiResponse.success(data={
        'authorization_url': url,
        'state': state,
    }).to_response()


@enterprise_bp.route('/sso/callback/<int:workspace_id>', methods=['GET'])
def sso_callback(workspace_id: int):
    """OIDC 回调：code+state → 平台 JWT（骨架版返回 JSON）。"""
    code = request.args.get('code') or ''
    state = request.args.get('state') or ''
    if not code or not state:
        return ApiResponse.error('missing code or state', 400).to_response()
    try:
        result = login_oidc(workspace_id, code, state)
    except Exception as e:  # noqa: BLE001 - state 校验/交换失败的统一入口
        return ApiResponse.error(f'SSO login failed: {e}', 401).to_response()
    return ApiResponse.success(data=result, message='SSO login succeeded').to_response()


# ── 合规报告 ──

def _parse_window(default_days: int = 30):
    end = datetime.utcnow()
    start = end - timedelta(days=default_days)
    start_raw = request.args.get('start_date')
    end_raw = request.args.get('end_date')
    if start_raw:
        try:
            start = datetime.fromisoformat(start_raw)
        except ValueError:
            return None, None, ApiResponse.error('invalid start_date', 400).to_response()
    if end_raw:
        try:
            end = datetime.fromisoformat(end_raw)
        except ValueError:
            return None, None, ApiResponse.error('invalid end_date', 400).to_response()
    return start, end, None


@enterprise_bp.route('/workspaces/<int:workspace_id>/compliance/report', methods=['GET'])
@unified_auth_required
def compliance_report(workspace_id: int):
    """组织级合规报告：风险事件 / 审批滞留 / 预算超支 / 导出次数（时间窗汇总）。"""
    user, _, err = _require_manage(workspace_id)
    if err:
        return err

    start, end, parse_err = _parse_window()
    if parse_err:
        return parse_err

    risk_min = request.args.get('risk_min', type=int)
    risk_threshold = risk_min if risk_min is not None else 40

    base = AgentAuditEvent.query.filter(
        AgentAuditEvent.workspace_id == workspace_id,
        AgentAuditEvent.occurred_at >= start,
        AgentAuditEvent.occurred_at <= end,
    )

    # 1) 风险事件
    risk_query = base.filter(AgentAuditEvent.risk_score >= risk_threshold)
    risk_events = risk_query.count()
    by_level_rows = (
        risk_query.with_entities(AgentAuditEvent.level, func.count(AgentAuditEvent.id))
        .group_by(AgentAuditEvent.level)
        .all()
    )
    by_level = {row[0]: row[1] for row in by_level_rows}
    by_type_rows = (
        risk_query.with_entities(AgentAuditEvent.event_type, func.count(AgentAuditEvent.id))
        .group_by(AgentAuditEvent.event_type)
        .order_by(func.count(AgentAuditEvent.id).desc())
        .limit(10)
        .all()
    )
    top_event_types = {row[0]: row[1] for row in by_type_rows}

    # 2) 预算超支 / 导出次数
    budget_overshoots = (
        base.filter(AgentAuditEvent.event_type == 'budget.exceeded').count()
    )
    audit_exports = (
        base.filter(AgentAuditEvent.event_type == 'audit.exported').count()
    )

    # 3) 审批滞留：interaction_request 与 interaction_approval 按 interaction_id 配对
    approval_rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type.in_(('interaction_request', 'interaction_approval')),
            AgentTaskEvent.event_timestamp >= start,
            AgentTaskEvent.event_timestamp <= end,
        )
        .all()
    )
    requested_at = {}
    decided_at = {}
    for row in approval_rows:
        interaction_id = (row.payload or {}).get('interaction_id')
        if not interaction_id:
            continue
        ts = row.event_timestamp
        if row.event_type == 'interaction_request':
            requested_at.setdefault(interaction_id, ts)
        else:
            decided_at.setdefault(interaction_id, ts)

    dwells = sorted(
        (decided_at[iid] - requested_at[iid]).total_seconds()
        for iid in decided_at
        if iid in requested_at and decided_at[iid] >= requested_at[iid]
    )
    pending = len([iid for iid in requested_at if iid not in decided_at])
    approvals = {
        'requested': len(requested_at),
        'decided': len(decided_at),
        'pending': pending,
        'avg_dwell_seconds': round(sum(dwells) / len(dwells), 1) if dwells else None,
        'max_dwell_seconds': max(dwells) if dwells else None,
    }

    # 4) SSO 状态
    from services.sso import get_config as get_sso_config
    sso_config = get_sso_config(workspace_id)
    sso_state = {
        'configured': bool(sso_config),
        'provider': sso_config.provider if sso_config else None,
        'enabled': bool(sso_config and sso_config.enabled),
    }

    report = {
        'workspace_id': workspace_id,
        'window': {'start': start.isoformat() + 'Z', 'end': end.isoformat() + 'Z'},
        'risk_events': {
            'threshold': risk_threshold,
            'total': risk_events,
            'by_level': by_level,
            'top_event_types': top_event_types,
        },
        'budget_overshoots': budget_overshoots,
        'approvals': approvals,
        'audit_exports': audit_exports,
        'sso': sso_state,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
    }

    write_agent_audit(
        event_type='compliance.report_generated',
        actor_type='user',
        actor_id=user.id,
        target_type='workspace',
        target_id=workspace_id,
        workspace_id=workspace_id,
        payload={'window': report['window'], 'risk_events': risk_events},
        risk_score=10,
    )
    return ApiResponse.success(data=report).to_response()
