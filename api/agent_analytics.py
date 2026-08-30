"""
Agent 分析和监控 API

包含健康监控、Secret 分析、批量操作
"""

from flask import Blueprint, request

from core.auth import get_current_user, unified_auth_required
from models import Agent, AgentStatus, db
from services.agent_batch_ops import AgentBatchOperations, get_batch_operations
from services.agent_health import AgentHealthMonitor, get_health_monitor, AgentHealthStatus
from services.secret_analytics import SecretUsageAnalyzer, get_secret_analyzer

from .agent_common import ensure_agent_manage_access, ensure_workspace_access, get_workspace_or_404, ensure_workspace_manage_access
from .base import ApiResponse, validate_json_request


def _ok(data=None, message=None):
    """Return a successful JSON response."""
    return ApiResponse.success(data=data, message=message).to_response()


def _err(message, code=400):
    """Return an error JSON response."""
    return ApiResponse.error(message, code).to_response()


def _not_found(resource='Resource'):
    """Return a not found JSON response."""
    return ApiResponse.not_found(resource).to_response()

agent_analytics_bp = Blueprint('agent_analytics', __name__, url_prefix='/api/v1')


# =============================================================================
# Agent 健康监控 API
# =============================================================================

@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/<int:agent_id>/health', methods=['GET'])
@unified_auth_required
def get_agent_health(workspace_id, agent_id):
    """获取 Agent 健康状态"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    agent = Agent.query.filter_by(id=agent_id, workspace_id=workspace_id).first()
    if not agent:
        return _not_found('Agent')

    monitor = get_health_monitor()
    health = monitor.check_agent_health(agent_id)

    return _ok(data=health)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/health/summary', methods=['GET'])
@unified_auth_required
def get_workspace_health_summary(workspace_id):
    """获取工作区 Agent 健康状态汇总"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    monitor = get_health_monitor()
    summary = monitor.get_health_summary(workspace_id)

    return _ok(data=summary)


# =============================================================================
# Secret 分析 API
# =============================================================================

@agent_analytics_bp.route('/workspaces/<int:workspace_id>/secrets/<int:secret_id>/analytics/trends', methods=['GET'])
@unified_auth_required
def get_secret_usage_trends(workspace_id, secret_id):
    """获取 Secret 使用趋势"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    days = request.args.get('days', 30, type=int)

    analyzer = get_secret_analyzer()
    trends = analyzer.get_usage_trends(secret_id, days)

    return _ok(data={
        'secret_id': secret_id,
        'days': days,
        'trends': trends,
    })


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/secrets/<int:secret_id>/analytics/anomalies', methods=['GET'])
@unified_auth_required
def get_secret_anomalies(workspace_id, secret_id):
    """获取 Secret 异常使用检测"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    threshold = request.args.get('threshold', 2.0, type=float)

    analyzer = get_secret_analyzer()
    anomalies = analyzer.detect_anomalies(secret_id, threshold)

    return _ok(data={
        'secret_id': secret_id,
        'threshold': threshold,
        'anomalies': anomalies,
    })


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/secrets/<int:secret_id>/analytics/heatmap', methods=['GET'])
@unified_auth_required
def get_secret_usage_heatmap(workspace_id, secret_id):
    """获取 Secret 使用热力图"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    days = request.args.get('days', 90, type=int)

    analyzer = get_secret_analyzer()
    heatmap = analyzer.get_usage_heatmap(secret_id, days)

    return _ok(data=heatmap)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/secrets/<int:secret_id>/analytics/top-callers', methods=['GET'])
@unified_auth_required
def get_secret_top_callers(workspace_id, secret_id):
    """获取 Secret 最频繁调用者"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    limit = request.args.get('limit', 10, type=int)

    analyzer = get_secret_analyzer()
    callers = analyzer.get_top_callers(secret_id, limit)

    return _ok(data={
        'secret_id': secret_id,
        'callers': callers,
    })


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/secrets/<int:secret_id>/analytics/report', methods=['GET'])
@unified_auth_required
def get_secret_usage_report(workspace_id, secret_id):
    """获取 Secret 完整使用报告"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    days = request.args.get('days', 30, type=int)

    analyzer = get_secret_analyzer()
    report = analyzer.generate_usage_report(secret_id, days)

    if 'error' in report:
        return _not_found('Secret')

    return _ok(data=report)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/secrets/analytics/stats', methods=['GET'])
@unified_auth_required
def get_workspace_secret_stats(workspace_id):
    """获取工作区 Secret 统计"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    analyzer = get_secret_analyzer()
    stats = analyzer.get_workspace_secret_stats(workspace_id)

    return _ok(data=stats)


# =============================================================================
# 批量操作 API
# =============================================================================

@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/export/csv', methods=['GET'])
@unified_auth_required
def export_agents_csv(workspace_id):
    """导出 Agents 到 CSV"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    agent_ids = request.args.getlist('agent_ids', type=int)
    agent_ids = agent_ids if agent_ids else None

    batch_ops = get_batch_operations()
    csv_content = batch_ops.export_agents_to_csv(workspace_id, agent_ids)

    from flask import Response
    return Response(
        csv_content,
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename=agents_{workspace_id}.csv',
        }
    )


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/export/json', methods=['GET'])
@unified_auth_required
def export_agents_json(workspace_id):
    """导出 Agents 到 JSON"""
    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    agent_ids = request.args.getlist('agent_ids', type=int)
    agent_ids = agent_ids if agent_ids else None
    include_secrets = request.args.get('include_secrets', 'false').lower() == 'true'

    batch_ops = get_batch_operations()
    export_data = batch_ops.export_agents_to_json(workspace_id, agent_ids, include_secrets)

    return _ok(data=export_data)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/import', methods=['POST'])
@unified_auth_required
def import_agents(workspace_id):
    """从 JSON 导入 Agents"""
    if not request.is_json:
        return _err("Content-Type must be application/json", 400)

    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = request.get_json()
    import_mode = request.args.get('mode', 'create')

    batch_ops = get_batch_operations()
    results = batch_ops.import_agents_from_json(workspace_id, user.id, data, import_mode)

    return _ok(data=results)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/batch/rotate-secrets', methods=['POST'])
@unified_auth_required
def batch_rotate_secrets(workspace_id):
    """批量轮换 Agent Secrets"""
    if not request.is_json:
        return _err("Content-Type must be application/json", 400)

    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = request.get_json()
    agent_ids = data.get('agent_ids', [])

    if not agent_ids:
        return _err('agent_ids is required', 400)

    batch_ops = get_batch_operations()
    results = batch_ops.batch_rotate_secrets(workspace_id, agent_ids, user.id)

    return _ok(data=results)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/batch/update-status', methods=['POST'])
@unified_auth_required
def batch_update_status(workspace_id):
    """批量更新 Agent 状态"""
    if not request.is_json:
        return _err("Content-Type must be application/json", 400)

    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = request.get_json()
    agent_ids = data.get('agent_ids', [])
    status_str = data.get('status')

    if not agent_ids or not status_str:
        return _err('agent_ids and status are required', 400)

    try:
        new_status = AgentStatus(status_str)
    except ValueError:
        return _err(f'Invalid status: {status_str}', 400)

    batch_ops = get_batch_operations()
    results = batch_ops.batch_update_agent_status(workspace_id, agent_ids, new_status, user.id)

    return _ok(data=results)


@agent_analytics_bp.route('/workspaces/<int:workspace_id>/agents/batch/delete', methods=['POST'])
@unified_auth_required
def batch_delete_agents(workspace_id):
    """批量删除 Agents"""
    if not request.is_json:
        return _err("Content-Type must be application/json", 400)

    user = get_current_user()

    workspace, access_err = get_workspace_or_404(workspace_id)
    if access_err:
        return access_err

    access_err = ensure_workspace_manage_access(user, workspace)
    if access_err:
        return access_err

    data = request.get_json()
    agent_ids = data.get('agent_ids', [])
    force = data.get('force', False)

    if not agent_ids:
        return _err('agent_ids is required', 400)

    batch_ops = get_batch_operations()
    results = batch_ops.batch_delete_agents(workspace_id, agent_ids, user.id, force)

    return _ok(data=results)
