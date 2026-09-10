#!/usr/bin/env python3
"""系统监控 API（仅管理员）

- GET /system/setup-state      部署初始化状态（前端菜单门控：装完藏「部署引导」）
- GET /system/monitor/server   服务器指标（CPU/内存/负载/磁盘/进程）
- GET /system/monitor/agents   Agent 维度全局监控
"""

from flask import Blueprint

from core.auth import get_current_user, unified_auth_required
from models import UserRole
from .base import ApiResponse
from services.system_monitor import (
    collect_agent_metrics,
    collect_server_metrics,
    get_setup_state,
)

system_monitor_bp = Blueprint('system_monitor', __name__)


def _forbidden_if_not_admin():
    user = get_current_user()
    if not user or user.role != UserRole.ADMIN:
        return ApiResponse.forbidden('Admin access required').to_response()
    return None


@system_monitor_bp.route('/system/setup-state', methods=['GET'])
@unified_auth_required
def setup_state():
    """部署初始化状态（管理员；廉价 DB 计数，供菜单门控）。"""
    blocked = _forbidden_if_not_admin()
    if blocked:
        return blocked
    return ApiResponse.success(
        data=get_setup_state(), message='Setup state retrieved').to_response()


@system_monitor_bp.route('/system/monitor/server', methods=['GET'])
@unified_auth_required
def monitor_server():
    """服务器指标：CPU/内存/负载/磁盘/进程（管理员）。"""
    blocked = _forbidden_if_not_admin()
    if blocked:
        return blocked
    return ApiResponse.success(
        data=collect_server_metrics(), message='Server metrics retrieved').to_response()


@system_monitor_bp.route('/system/monitor/agents', methods=['GET'])
@unified_auth_required
def monitor_agents():
    """Agent 维度全局监控（管理员）。"""
    blocked = _forbidden_if_not_admin()
    if blocked:
        return blocked
    return ApiResponse.success(
        data=collect_agent_metrics(), message='Agent metrics retrieved').to_response()
