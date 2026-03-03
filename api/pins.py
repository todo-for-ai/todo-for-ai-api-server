"""
用户项目Pin API接口
"""

from datetime import datetime
from flask import Blueprint, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from models import db, UserProjectPin, Project
from core.auth import unified_auth_required, get_current_user
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from core.cache_invalidation import invalidate_user_caches
from .base import ApiResponse, handle_api_error

pins_bp = Blueprint('pins', __name__)
PINS_CACHE_TTL_SECONDS = 20
pins_fallback_cache = {}


def _pins_cache_get(key):
    redis_key = f"pins:{key}"
    cached = redis_get_json(redis_key)
    if cached is not None:
        return cached

    item = pins_fallback_cache.get(key)
    if item and (datetime.utcnow().timestamp() - item['cached_at'] <= PINS_CACHE_TTL_SECONDS):
        return item['value']
    return None


def _pins_cache_set(key, value):
    redis_key = f"pins:{key}"
    redis_set_json(redis_key, value, PINS_CACHE_TTL_SECONDS)
    pins_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'value': value,
    }


@pins_bp.route('', methods=['GET'])
@unified_auth_required
def get_user_pins():
    """获取当前用户的Pin配置"""
    try:
        user_id = get_current_user().id
        cache_key = f"user:{user_id}:list"
        cached = _pins_cache_get(cache_key)
        if cached is not None:
            return ApiResponse.success(cached, "User pins retrieved successfully").to_response()

        # 显式预加载项目关系，避免 to_dict() 触发 N+1
        pins = UserProjectPin.query.filter_by(user_id=user_id, is_active=True)\
            .options(joinedload(UserProjectPin.project))\
            .order_by(UserProjectPin.pin_order.asc(), UserProjectPin.created_at.asc())\
            .all()

        # 转换为字典并包含项目信息
        result = []
        for pin in pins:
            pin_dict = pin.to_dict()
            result.append(pin_dict)

        response_data = {
            'pins': result,
            'total': len(result)
        }
        _pins_cache_set(cache_key, response_data)
        return ApiResponse.success(response_data, "User pins retrieved successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@pins_bp.route('', methods=['POST'])
@unified_auth_required
def pin_project():
    """Pin一个项目"""
    try:
        user_id = get_current_user().id
        data = request.get_json()
        
        project_id = data.get('project_id')
        pin_order = data.get('pin_order')
        
        if not project_id:
            return ApiResponse.error('project_id is required', 400).to_response()

        # 检查项目是否存在且用户有权限访问
        project = Project.query.filter_by(id=project_id, owner_id=user_id).first()
        if not project:
            return ApiResponse.error('Project not found or access denied', 404).to_response()
        
        # 检查Pin数量限制（最多10个）
        current_pin_count = UserProjectPin.get_user_pin_count(user_id)
        if current_pin_count >= 10:
            # 检查是否已经Pin了这个项目
            if not UserProjectPin.is_project_pinned(user_id, project_id):
                return ApiResponse.error('Maximum 10 projects can be pinned', 400).to_response()
        
        # Pin项目
        pin = UserProjectPin.pin_project(user_id, project_id, pin_order)
        db.session.add(pin)
        db.session.commit()
        invalidate_user_caches(user_id)
        
        return ApiResponse.success({
            'pin': pin.to_dict()
        }, 'Project pinned successfully').to_response()
    
    except Exception as e:
        db.session.rollback()
        return handle_api_error(e)


@pins_bp.route('/<int:project_id>', methods=['DELETE'])
@unified_auth_required
def unpin_project(project_id):
    """取消Pin一个项目"""
    try:
        user_id = get_current_user().id
        
        # 取消Pin
        pin = UserProjectPin.unpin_project(user_id, project_id)
        if not pin:
            return ApiResponse.error('Pin not found', 404).to_response()

        db.session.add(pin)
        db.session.commit()
        invalidate_user_caches(user_id)

        return ApiResponse.success(None, 'Project unpinned successfully').to_response()
    
    except Exception as e:
        db.session.rollback()
        return handle_api_error(e)


@pins_bp.route('/reorder', methods=['PUT'])
@unified_auth_required
def reorder_pins():
    """重新排序Pin"""
    try:
        user_id = get_current_user().id
        data = request.get_json()
        
        pin_orders = data.get('pin_orders', [])
        if not pin_orders:
            return ApiResponse.error('pin_orders is required', 400).to_response()

        # 验证数据格式
        for item in pin_orders:
            if not isinstance(item, dict) or 'project_id' not in item or 'pin_order' not in item:
                return ApiResponse.error('Invalid pin_orders format', 400).to_response()
        
        # 批量读取后内存更新，避免循环中的 N 次查询
        target_project_ids = [item['project_id'] for item in pin_orders]
        pins = UserProjectPin.query.filter(
            UserProjectPin.user_id == user_id,
            UserProjectPin.is_active.is_(True),
            UserProjectPin.project_id.in_(target_project_ids)
        ).all()
        pin_map = {pin.project_id: pin for pin in pins}
        for item in pin_orders:
            pin = pin_map.get(item['project_id'])
            if pin:
                pin.pin_order = item['pin_order']

        db.session.commit()
        invalidate_user_caches(user_id)
        
        return ApiResponse.success(None, 'Pins reordered successfully').to_response()
    
    except Exception as e:
        db.session.rollback()
        return handle_api_error(e)


@pins_bp.route('/check/<int:project_id>', methods=['GET'])
@unified_auth_required
def check_pin_status(project_id):
    """检查项目的Pin状态"""
    try:
        user_id = get_current_user().id
        is_pinned = UserProjectPin.is_project_pinned(user_id, project_id)
        
        return ApiResponse.success({
            'project_id': project_id,
            'is_pinned': is_pinned
        }, "Pin status retrieved successfully").to_response()
    
    except Exception as e:
        return handle_api_error(e)


@pins_bp.route('/stats', methods=['GET'])
@unified_auth_required
def get_pin_stats():
    """获取Pin统计信息"""
    try:
        user_id = get_current_user().id
        cache_key = f"user:{user_id}:stats"
        cached = _pins_cache_get(cache_key)
        if cached is not None:
            return ApiResponse.success(cached, "Pin statistics retrieved successfully").to_response()

        pin_count = UserProjectPin.get_user_pin_count(user_id)

        response_data = {
            'pin_count': pin_count,
            'max_pins': 10,
            'remaining': max(0, 10 - pin_count)
        }
        _pins_cache_set(cache_key, response_data)
        return ApiResponse.success(response_data, "Pin statistics retrieved successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@pins_bp.route('/task-counts', methods=['GET'])
@unified_auth_required
def get_pinned_projects_task_counts():
    """获取Pin项目的待执行任务数量"""
    try:
        user_id = get_current_user().id
        cache_key = f"user:{user_id}:task-counts"
        cached = _pins_cache_get(cache_key)
        if cached is not None:
            return ApiResponse.success(cached, "Task counts retrieved successfully").to_response()

        # 获取用户的Pin项目
        pins = UserProjectPin.query.filter_by(user_id=user_id, is_active=True)\
            .options(joinedload(UserProjectPin.project))\
            .order_by(UserProjectPin.pin_order.asc(), UserProjectPin.created_at.asc())\
            .all()

        if not pins:
            response_data = {
                'task_counts': [],
                'total_pins': 0
            }
            _pins_cache_set(cache_key, response_data)
            return ApiResponse.success(response_data, "Task counts retrieved successfully").to_response()

        from models.task import Task, TaskStatus
        pending_statuses = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]
        project_ids = [pin.project_id for pin in pins]

        # 单次聚合查询待执行任务数量，避免 N+1 count
        pending_rows = db.session.query(
            Task.project_id.label('project_id'),
            func.count(Task.id).label('pending_tasks')
        ).filter(
            Task.project_id.in_(project_ids),
            Task.status.in_(pending_statuses)
        ).group_by(
            Task.project_id
        ).all()

        pending_map = {
            row.project_id: int(row.pending_tasks or 0)
            for row in pending_rows
        }

        result = []
        for pin in pins:
            project = pin.project
            if project:
                result.append({
                    'project_id': project.id,
                    'project_name': project.name,
                    'project_color': project.color,
                    'pending_tasks': pending_map.get(project.id, 0),
                    'pin_order': pin.pin_order
                })

        response_data = {
            'task_counts': result,
            'total_pins': len(result)
        }
        _pins_cache_set(cache_key, response_data)
        return ApiResponse.success(response_data, "Task counts retrieved successfully").to_response()

    except Exception as e:
        return handle_api_error(e)
