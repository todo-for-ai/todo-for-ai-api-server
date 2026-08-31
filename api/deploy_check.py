"""私有化部署自检端点（Phase 4 企业能力：私有化部署增强）

- GET /system/deploy/check  部署健康自检报告（仅管理员；版本一致性/必需配置/迁移完整性）
"""

from flask import Blueprint

from core.auth import get_current_user, unified_auth_required
from models import UserRole
from .base import ApiResponse
from services.deploy_check import run_deploy_checks

deploy_check_bp = Blueprint('deploy_check', __name__)


@deploy_check_bp.route('/system/deploy/check', methods=['GET'])
@unified_auth_required
def deploy_check():
    """部署自检报告（管理员）：版本一致性 / 必需配置项 / 迁移完整性。"""
    user = get_current_user()
    if not user or user.role != UserRole.ADMIN:
        return ApiResponse.forbidden('Admin access required').to_response()

    report = run_deploy_checks()
    return ApiResponse.success(data=report, message='Deploy check finished').to_response()
