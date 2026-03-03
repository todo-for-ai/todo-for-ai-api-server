"""
仪表盘API
"""

from flask import Blueprint, current_app
from datetime import datetime, date, timedelta
from sqlalchemy import func, case
from sqlalchemy.orm import joinedload
import threading

from models import Project, ProjectStatus, Task, TaskStatus, UserActivity
from core.auth import unified_auth_required, get_current_user
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from .base import ApiResponse

dashboard_bp = Blueprint('dashboard', __name__)

DASHBOARD_STATS_CACHE_TTL_SECONDS = 120
DASHBOARD_STATS_STALE_TTL_SECONDS = 3600
DASHBOARD_HEATMAP_CACHE_TTL_SECONDS = 300
DASHBOARD_ACTIVITY_SUMMARY_CACHE_TTL_SECONDS = 120
dashboard_fallback_cache = {}
_dashboard_stats_refreshing_users = set()
_dashboard_stats_refresh_lock = threading.Lock()


def _dashboard_cache_get(key, ttl_seconds, stale_ttl_seconds=None):
    redis_key = f"dashboard:{key}"
    cached = redis_get_json(redis_key)
    if cached is not None:
        return cached, False

    item = dashboard_fallback_cache.get(key)
    if item and (datetime.utcnow().timestamp() - item['cached_at'] <= ttl_seconds):
        return item['value'], False

    if stale_ttl_seconds:
        stale_redis_key = f"{redis_key}:stale"
        stale_cached = redis_get_json(stale_redis_key)
        if stale_cached is not None:
            return stale_cached, True

        stale_item = dashboard_fallback_cache.get(f"{key}:stale")
        if stale_item and (datetime.utcnow().timestamp() - stale_item['cached_at'] <= stale_ttl_seconds):
            return stale_item['value'], True

    return None, False


def _dashboard_cache_set(key, value, ttl_seconds, stale_ttl_seconds=None):
    redis_key = f"dashboard:{key}"
    redis_set_json(redis_key, value, ttl_seconds)
    dashboard_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'value': value,
    }
    if stale_ttl_seconds:
        redis_set_json(f"{redis_key}:stale", value, stale_ttl_seconds)
        dashboard_fallback_cache[f"{key}:stale"] = {
            'cached_at': datetime.utcnow().timestamp(),
            'value': value,
        }


def _build_dashboard_stats(user_id):
    # 项目统计：条件聚合，单次扫描返回所需字段
    project_stats_row = Project.query.with_entities(
        func.count(Project.id).label('total_projects'),
        func.sum(case((Project.status == ProjectStatus.ACTIVE, 1), else_=0)).label('active_projects')
    ).filter(
        Project.owner_id == user_id
    ).first()
    total_projects = int((project_stats_row.total_projects if project_stats_row else 0) or 0)
    active_projects = int((project_stats_row.active_projects if project_stats_row else 0) or 0)

    # 任务统计：条件聚合，避免多次全量 count
    task_stats_row = Task.query.with_entities(
        func.count(Task.id).label('total_tasks'),
        func.sum(case((Task.status == TaskStatus.TODO, 1), else_=0)).label('todo_tasks'),
        func.sum(case((Task.status == TaskStatus.IN_PROGRESS, 1), else_=0)).label('in_progress_tasks'),
        func.sum(case((Task.status == TaskStatus.REVIEW, 1), else_=0)).label('review_tasks'),
        func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('done_tasks')
    ).filter(
        Task.owner_id == user_id
    ).first()
    total_tasks = int((task_stats_row.total_tasks if task_stats_row else 0) or 0)
    todo_tasks = int((task_stats_row.todo_tasks if task_stats_row else 0) or 0)
    in_progress_tasks = int((task_stats_row.in_progress_tasks if task_stats_row else 0) or 0)
    review_tasks = int((task_stats_row.review_tasks if task_stats_row else 0) or 0)
    done_tasks = int((task_stats_row.done_tasks if task_stats_row else 0) or 0)

    pending_statuses = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]
    large_dataset_threshold = 200000
    task_base = Task.query.filter(Task.owner_id == user_id)
    if total_tasks > large_dataset_threshold:
        # 大数据量下跳过高成本统计，保证接口可用性
        ai_tasks = 0
        recent_tasks = []
    else:
        ai_tasks = task_base.filter(
            Task.is_ai_task.is_(True),
            Task.status.in_(pending_statuses)
        ).count()
        recent_tasks = task_base.options(
            joinedload(Task.project)
        ).order_by(
            Task.updated_at.desc()
        ).limit(5).all()

    # 最近项目（最近更新的5个项目）
    recent_projects = Project.query.filter_by(owner_id=user_id)\
        .order_by(Project.updated_at.desc())\
        .limit(5).all()

    # 用户活跃度统计（最近30天）
    activity_stats = UserActivity.get_user_activity_stats(user_id, days=30)

    return {
        'projects': {
            'total': total_projects,
            'active': active_projects,
        },
        'tasks': {
            'total': total_tasks,
            'todo': todo_tasks,
            'in_progress': in_progress_tasks,
            'review': review_tasks,
            'done': done_tasks,
            'ai_executing': ai_tasks,
        },
        'recent_projects': [p.to_dict() for p in recent_projects],
        'recent_tasks': [t.to_dict(include_project=True) for t in recent_tasks],
        'activity_stats': activity_stats,
    }


def _refresh_dashboard_stats_in_background(app, user_id, cache_key):
    try:
        with app.app_context():
            response_data = _build_dashboard_stats(user_id)
            _dashboard_cache_set(
                cache_key,
                response_data,
                DASHBOARD_STATS_CACHE_TTL_SECONDS,
                DASHBOARD_STATS_STALE_TTL_SECONDS
            )
    except Exception as e:
        app.logger.warning(f"Background refresh dashboard stats failed for user {user_id}: {e}")
    finally:
        with _dashboard_stats_refresh_lock:
            _dashboard_stats_refreshing_users.discard(user_id)


def _trigger_dashboard_stats_async_refresh(user_id, cache_key):
    with _dashboard_stats_refresh_lock:
        if user_id in _dashboard_stats_refreshing_users:
            return
        _dashboard_stats_refreshing_users.add(user_id)

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_refresh_dashboard_stats_in_background,
        args=(app, user_id, cache_key),
        daemon=True
    )
    thread.start()


@dashboard_bp.route('/stats', methods=['GET'])
@unified_auth_required
def get_dashboard_stats():
    """获取仪表盘统计数据（用户隔离）"""
    try:
        current_user = get_current_user()
        cache_key = f"user:{current_user.id}:stats"
        cached_data, is_stale = _dashboard_cache_get(
            cache_key,
            DASHBOARD_STATS_CACHE_TTL_SECONDS,
            DASHBOARD_STATS_STALE_TTL_SECONDS
        )
        if cached_data is not None:
            if is_stale:
                _trigger_dashboard_stats_async_refresh(current_user.id, cache_key)
            return ApiResponse.success(cached_data, "Dashboard stats retrieved successfully").to_response()

        response_data = _build_dashboard_stats(current_user.id)
        _dashboard_cache_set(
            cache_key,
            response_data,
            DASHBOARD_STATS_CACHE_TTL_SECONDS,
            DASHBOARD_STATS_STALE_TTL_SECONDS
        )

        return ApiResponse.success(response_data, "Dashboard stats retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to get dashboard stats: {str(e)}", 500).to_response()


@dashboard_bp.route('/activity-heatmap', methods=['GET'])
@unified_auth_required
def get_activity_heatmap():
    """获取用户活跃度热力图数据"""
    try:
        current_user = get_current_user()
        cache_key = f"user:{current_user.id}:heatmap"
        cached_data, _ = _dashboard_cache_get(cache_key, DASHBOARD_HEATMAP_CACHE_TTL_SECONDS)
        if cached_data is not None:
            return ApiResponse.success(cached_data, "Activity heatmap retrieved successfully").to_response()
        
        # 获取最近365天的活跃度数据
        heatmap_data = UserActivity.get_user_activity_heatmap(current_user.id, days=365)
        
        response_data = {
            'heatmap_data': heatmap_data,
            'user_id': current_user.id,
            'generated_at': datetime.utcnow().isoformat()
        }
        _dashboard_cache_set(cache_key, response_data, DASHBOARD_HEATMAP_CACHE_TTL_SECONDS)

        return ApiResponse.success(response_data, "Activity heatmap retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to get activity heatmap: {str(e)}", 500).to_response()


@dashboard_bp.route('/activity-summary', methods=['GET'])
@unified_auth_required
def get_activity_summary():
    """获取活跃度摘要统计"""
    try:
        current_user = get_current_user()
        cache_key = f"user:{current_user.id}:activity_summary"
        cached_data, _ = _dashboard_cache_get(cache_key, DASHBOARD_ACTIVITY_SUMMARY_CACHE_TTL_SECONDS)
        if cached_data is not None:
            return ApiResponse.success(cached_data, "Activity summary retrieved successfully").to_response()
        
        # 获取不同时间段的统计
        stats_7d = UserActivity.get_user_activity_stats(current_user.id, days=7)
        stats_30d = UserActivity.get_user_activity_stats(current_user.id, days=30)
        stats_90d = UserActivity.get_user_activity_stats(current_user.id, days=90)
        stats_365d = UserActivity.get_user_activity_stats(current_user.id, days=365)
        
        # 计算连续活跃天数
        consecutive_days = _get_consecutive_active_days(current_user.id)
        
        # 获取最活跃的一天
        most_active_day = UserActivity.query.filter_by(user_id=current_user.id)\
            .order_by(UserActivity.total_activity_count.desc())\
            .first()
        
        response_data = {
            'stats_7d': stats_7d,
            'stats_30d': stats_30d,
            'stats_90d': stats_90d,
            'stats_365d': stats_365d,
            'consecutive_active_days': consecutive_days,
            'most_active_day': {
                'date': most_active_day.activity_date.isoformat() if most_active_day else None,
                'count': most_active_day.total_activity_count if most_active_day else 0
            }
        }
        _dashboard_cache_set(cache_key, response_data, DASHBOARD_ACTIVITY_SUMMARY_CACHE_TTL_SECONDS)

        return ApiResponse.success(response_data, "Activity summary retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to get activity summary: {str(e)}", 500).to_response()


def _get_consecutive_active_days(user_id):
    """计算连续活跃天数"""
    try:
        today = date.today()
        consecutive_days = 0
        current_date = today
        
        # 从今天开始往前查找连续活跃的天数
        while True:
            activity = UserActivity.query.filter_by(
                user_id=user_id,
                activity_date=current_date
            ).first()
            
            if activity and activity.total_activity_count > 0:
                consecutive_days += 1
                current_date -= timedelta(days=1)
            else:
                break
            
            # 防止无限循环，最多查找365天
            if consecutive_days >= 365:
                break
        
        return consecutive_days
        
    except Exception:
        return 0
