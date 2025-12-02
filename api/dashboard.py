"""
仪表盘API
"""

from flask import Blueprint, jsonify
from sqlalchemy import func, and_
from sqlalchemy.orm import joinedload
from datetime import datetime, date, timedelta
from functools import wraps
import time

from models import db, User, Project, Task, TaskStatus, UserActivity
from .auth import require_auth, get_current_user
from .base import api_response, api_error

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

# 简单的内存缓存（用户ID -> (数据, 过期时间)）
_dashboard_cache = {}
_CACHE_TTL = 1800  # 30分钟缓存（对于大数据量用户，延长缓存时间）
_LARGE_USER_CACHE_TTL = 3600  # 1小时缓存（超过100万任务的用户）


@dashboard_bp.route('/stats', methods=['GET'])
@require_auth
def get_dashboard_stats():
    """获取仪表盘统计数据（用户隔离）"""
    try:
        current_user = get_current_user()
        current_time = time.time()
        
        # 检查缓存
        cache_key = f"dashboard_stats_{current_user.id}"
        if cache_key in _dashboard_cache:
            cached_data, expire_time = _dashboard_cache[cache_key]
            if current_time < expire_time:
                # 缓存未过期，直接返回
                return api_response(cached_data, "Dashboard stats retrieved from cache")
        
        # 优先使用user_stats统计表（性能优化：避免实时聚合查询）
        from sqlalchemy import text
        
        stats_result = db.session.execute(
            text("SELECT * FROM user_stats WHERE user_id = :user_id"),
            {'user_id': current_user.id}
        ).first()
        
        if stats_result:
            # 使用缓存的统计数据
            total_projects = stats_result.total_projects
            active_projects = stats_result.active_projects
            total_tasks = stats_result.total_tasks
            todo_tasks = stats_result.todo_tasks
            in_progress_tasks = stats_result.in_progress_tasks
            review_tasks = stats_result.review_tasks
            done_tasks = stats_result.done_tasks
            ai_tasks = stats_result.ai_pending_tasks
        else:
            # 如果统计表中没有数据，执行实时查询（fallback）
            # 同时在后台触发统计表更新
            project_stats_query = db.session.query(
                func.count(Project.id).label('total'),
                func.sum(func.IF(Project.status == 'ACTIVE', 1, 0)).label('active')
            ).filter_by(owner_id=current_user.id).first()
            
            total_projects = project_stats_query.total or 0
            active_projects = project_stats_query.active or 0
            
            # 任务统计（直接使用owner_id字段）
            sql = text("""
                SELECT 
                    COUNT(tasks.id) as total,
                    SUM(CASE WHEN tasks.status = 'TODO' THEN 1 ELSE 0 END) as todo,
                    SUM(CASE WHEN tasks.status = 'IN_PROGRESS' THEN 1 ELSE 0 END) as in_progress,
                    SUM(CASE WHEN tasks.status = 'REVIEW' THEN 1 ELSE 0 END) as review,
                    SUM(CASE WHEN tasks.status = 'DONE' THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN tasks.is_ai_task = 1 AND tasks.status IN ('TODO', 'IN_PROGRESS', 'REVIEW') THEN 1 ELSE 0 END) as ai_tasks
                FROM tasks
                WHERE tasks.owner_id = :owner_id
            """)
            
            result = db.session.execute(sql, {'owner_id': current_user.id}).first()
            
            total_tasks = result.total or 0
            todo_tasks = result.todo or 0
            in_progress_tasks = result.in_progress or 0
            review_tasks = result.review or 0
            done_tasks = result.done or 0
            ai_tasks = result.ai_tasks or 0
        
        # 创建子查询用于后续查询
        project_id_subquery = db.session.query(Project.id).filter_by(owner_id=current_user.id).subquery()
        
        # 最近项目（优化：使用索引友好的查询）
        recent_projects = Project.query.filter_by(owner_id=current_user.id)\
            .order_by(Project.id.desc())\
            .limit(5).all()
        
        # 最近任务（暂时禁用，大数据量下排序性能问题）
        # TODO: 优化方案 - 添加复合索引或使用缓存
        recent_tasks = []
        # if total_projects > 0:
        #     recent_tasks = Task.query.options(joinedload(Task.project))\
        #         .filter(Task.project_id.in_(project_id_subquery))\
        #         .order_by(Task.updated_at.desc())\
        #         .limit(5).all()
        
        # 用户活跃度统计（最近30天）
        activity_stats = UserActivity.get_user_activity_stats(current_user.id, days=30)
        
        # 构建响应数据
        response_data = {
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
        
        # 根据用户数据量动态选择缓存TTL
        # 超过100万任务的用户使用更长的缓存时间
        cache_ttl = _LARGE_USER_CACHE_TTL if total_tasks > 1000000 else _CACHE_TTL
        
        # 更新缓存
        _dashboard_cache[cache_key] = (response_data, current_time + cache_ttl)
        
        # 清理过期缓存（保持缓存字典不会无限增长）
        expired_keys = [k for k, (_, exp_time) in _dashboard_cache.items() if current_time > exp_time]
        for k in expired_keys:
            del _dashboard_cache[k]
        
        # 在响应中添加缓存信息（用于调试）
        cache_info = {
            'cached': False,
            'cache_ttl': cache_ttl,
            'total_tasks': total_tasks
        }
        
        return api_response(response_data, f"Dashboard stats retrieved successfully (cache for {cache_ttl}s)", cache_info=cache_info)
        
    except Exception as e:
        return api_error(f"Failed to get dashboard stats: {str(e)}", 500)


@dashboard_bp.route('/activity-heatmap', methods=['GET'])
@require_auth
def get_activity_heatmap():
    """获取用户活跃度热力图数据"""
    try:
        current_user = get_current_user()
        
        # 获取最近365天的活跃度数据
        heatmap_data = UserActivity.get_user_activity_heatmap(current_user.id, days=365)
        
        return api_response({
            'heatmap_data': heatmap_data,
            'user_id': current_user.id,
            'generated_at': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        return api_error(f"Failed to get activity heatmap: {str(e)}", 500)


@dashboard_bp.route('/activity-summary', methods=['GET'])
@require_auth
def get_activity_summary():
    """获取活跃度摘要统计"""
    try:
        current_user = get_current_user()
        
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
        
        return api_response({
            'stats_7d': stats_7d,
            'stats_30d': stats_30d,
            'stats_90d': stats_90d,
            'stats_365d': stats_365d,
            'consecutive_active_days': consecutive_days,
            'most_active_day': {
                'date': most_active_day.activity_date.isoformat() if most_active_day else None,
                'count': most_active_day.total_activity_count if most_active_day else 0
            }
        })
        
    except Exception as e:
        return api_error(f"Failed to get activity summary: {str(e)}", 500)


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
