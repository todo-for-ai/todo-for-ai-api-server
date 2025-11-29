"""
仪表盘API
"""

from flask import Blueprint, jsonify
from sqlalchemy import func, and_
from sqlalchemy.orm import joinedload
from datetime import datetime, date, timedelta

from models import db, User, Project, Task, TaskStatus, UserActivity
from .auth import require_auth, get_current_user
from .base import api_response, api_error

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')


@dashboard_bp.route('/stats', methods=['GET'])
@require_auth
def get_dashboard_stats():
    """获取仪表盘统计数据（用户隔离）"""
    try:
        current_user = get_current_user()
        
        # 使用聚合查询优化项目统计（避免加载所有项目）
        project_stats_query = db.session.query(
            func.count(Project.id).label('total'),
            func.sum(func.IF(Project.status == 'ACTIVE', 1, 0)).label('active')
        ).filter_by(owner_id=current_user.id).first()
        
        total_projects = project_stats_query.total or 0
        active_projects = project_stats_query.active or 0
        
        # 使用原生SQL优化性能（避免ORM开销）
        from sqlalchemy import text
        sql = text("""
            SELECT 
                COUNT(tasks.id) as total,
                SUM(CASE WHEN tasks.status = 'TODO' THEN 1 ELSE 0 END) as todo,
                SUM(CASE WHEN tasks.status = 'IN_PROGRESS' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN tasks.status = 'REVIEW' THEN 1 ELSE 0 END) as review,
                SUM(CASE WHEN tasks.status = 'DONE' THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN tasks.is_ai_task = 1 AND tasks.status IN ('TODO', 'IN_PROGRESS', 'REVIEW') THEN 1 ELSE 0 END) as ai_tasks
            FROM tasks
            JOIN projects ON tasks.project_id = projects.id
            WHERE projects.owner_id = :owner_id
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
        
        return api_response({
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
        })
        
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
