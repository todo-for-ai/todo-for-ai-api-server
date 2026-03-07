"""
仪表盘API
"""

from flask import Blueprint, current_app
from datetime import datetime, date, timedelta
from sqlalchemy import func, case
from sqlalchemy.orm import joinedload
import threading

from models import (
    db,
    Project,
    ProjectStatus,
    ProjectMember,
    ProjectMemberStatus,
    Organization,
    OrganizationMember,
    OrganizationMemberStatus,
    OrganizationRole,
    OrganizationMemberRole,
    OrganizationRoleDefinition,
    Agent,
    AgentStatus,
    OrganizationAgentMember,
    OrganizationAgentMemberStatus,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    Task,
    TaskStatus,
    UserActivity,
)
from core.auth import unified_auth_required, get_current_user
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from .base import ApiResponse


dashboard_bp = Blueprint('dashboard', __name__)

DASHBOARD_STATS_CACHE_TTL_SECONDS = 120
DASHBOARD_STATS_STALE_TTL_SECONDS = 3600
DASHBOARD_HEATMAP_CACHE_TTL_SECONDS = 300
DASHBOARD_ACTIVITY_SUMMARY_CACHE_TTL_SECONDS = 120
LARGE_DATASET_THRESHOLD = 200000
PENDING_TASK_STATUSES = [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]

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


def _empty_project_stats():
    return {
        'total': 0,
        'active': 0,
    }


def _empty_task_stats():
    return {
        'total': 0,
        'todo': 0,
        'in_progress': 0,
        'review': 0,
        'done': 0,
        'ai_executing': 0,
    }


def _build_scope_stats(project_query, task_query):
    project_stats_row = project_query.with_entities(
        func.count(Project.id).label('total_projects'),
        func.sum(case((Project.status == ProjectStatus.ACTIVE, 1), else_=0)).label('active_projects')
    ).first()

    task_stats_row = task_query.with_entities(
        func.count(Task.id).label('total_tasks'),
        func.sum(case((Task.status == TaskStatus.TODO, 1), else_=0)).label('todo_tasks'),
        func.sum(case((Task.status == TaskStatus.IN_PROGRESS, 1), else_=0)).label('in_progress_tasks'),
        func.sum(case((Task.status == TaskStatus.REVIEW, 1), else_=0)).label('review_tasks'),
        func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('done_tasks')
    ).first()

    total_tasks = int((task_stats_row.total_tasks if task_stats_row else 0) or 0)
    ai_tasks = 0
    if total_tasks <= LARGE_DATASET_THRESHOLD:
        ai_tasks = task_query.filter(
            Task.is_ai_task.is_(True),
            Task.status.in_(PENDING_TASK_STATUSES)
        ).count()

    return {
        'projects': {
            'total': int((project_stats_row.total_projects if project_stats_row else 0) or 0),
            'active': int((project_stats_row.active_projects if project_stats_row else 0) or 0),
        },
        'tasks': {
            'total': total_tasks,
            'todo': int((task_stats_row.todo_tasks if task_stats_row else 0) or 0),
            'in_progress': int((task_stats_row.in_progress_tasks if task_stats_row else 0) or 0),
            'review': int((task_stats_row.review_tasks if task_stats_row else 0) or 0),
            'done': int((task_stats_row.done_tasks if task_stats_row else 0) or 0),
            'ai_executing': int(ai_tasks or 0),
        },
    }


def _get_participated_project_ids(user_id):
    owned_project_ids = [
        int(row.id)
        for row in Project.query.with_entities(Project.id).filter(Project.owner_id == user_id).all()
    ]
    member_project_ids = [
        int(row.project_id)
        for row in ProjectMember.query.with_entities(ProjectMember.project_id).filter(
            ProjectMember.user_id == user_id,
            ProjectMember.status == ProjectMemberStatus.ACTIVE,
        ).all()
    ]
    return sorted(set(owned_project_ids + member_project_ids))


def _get_accessible_organizations(user_id):
    owned_rows = Organization.query.with_entities(
        Organization.id,
        Organization.name,
        Organization.updated_at,
    ).filter(
        Organization.owner_id == user_id
    ).all()

    member_rows = db.session.query(
        Organization.id,
        Organization.name,
        Organization.updated_at,
    ).join(
        OrganizationMember, OrganizationMember.organization_id == Organization.id
    ).filter(
        OrganizationMember.user_id == user_id,
        OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
    ).all()

    member_role_rows = db.session.query(
        OrganizationMember.organization_id,
        OrganizationRoleDefinition.key,
    ).join(
        OrganizationMemberRole, OrganizationMemberRole.member_id == OrganizationMember.id
    ).join(
        OrganizationRoleDefinition, OrganizationRoleDefinition.id == OrganizationMemberRole.role_id
    ).filter(
        OrganizationMember.user_id == user_id,
        OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        OrganizationRoleDefinition.is_active.is_(True),
    ).all()

    role_map = {}
    for row in member_role_rows:
        org_id = int(row.organization_id)
        role_key = str(row.key).strip().lower() if row.key else ''
        if not role_key:
            continue
        role_map.setdefault(org_id, set()).add(role_key)

    org_map = {}
    for row in owned_rows:
        org_map[int(row.id)] = {
            'id': int(row.id),
            'name': row.name,
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            'my_role': OrganizationRole.OWNER.value,
        }

    for row in member_rows:
        role_keys = role_map.get(int(row.id), set())
        row_role = None
        for candidate in ['owner', 'admin', 'member', 'viewer']:
            if candidate in role_keys:
                row_role = candidate
                break
        if not row_role and role_keys:
            row_role = list(role_keys)[0]
        if int(row.id) in org_map:
            continue
        org_map[int(row.id)] = {
            'id': int(row.id),
            'name': row.name,
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            'my_role': row_role,
        }

    return org_map


def _build_organization_agent_stats(org_map):
    org_ids = sorted(org_map.keys())
    if not org_ids:
        return {
            'summary': {
                'total': 0,
                'total_agents': 0,
                'active_agents_7d': 0,
            },
            'top_organizations': [],
        }

    cutoff = datetime.utcnow() - timedelta(days=7)

    total_rows = db.session.query(
        OrganizationAgentMember.organization_id,
        func.count(func.distinct(OrganizationAgentMember.agent_id)).label('total_agents'),
    ).filter(
        OrganizationAgentMember.organization_id.in_(org_ids),
        OrganizationAgentMember.status != OrganizationAgentMemberStatus.REMOVED,
    ).group_by(
        OrganizationAgentMember.organization_id
    ).all()
    total_by_org = {int(row.organization_id): int(row.total_agents or 0) for row in total_rows}

    active_rows = db.session.query(
        OrganizationAgentMember.organization_id,
        func.count(func.distinct(OrganizationAgentMember.agent_id)).label('active_agents_7d'),
    ).join(
        Agent, Agent.id == OrganizationAgentMember.agent_id
    ).join(
        AgentTaskAttempt, AgentTaskAttempt.agent_id == Agent.id
    ).filter(
        OrganizationAgentMember.organization_id.in_(org_ids),
        OrganizationAgentMember.status != OrganizationAgentMemberStatus.REMOVED,
        Agent.status == AgentStatus.ACTIVE,
        AgentTaskAttempt.started_at >= cutoff,
        AgentTaskAttempt.state.in_([AgentTaskAttemptState.ACTIVE, AgentTaskAttemptState.COMMITTED]),
    ).group_by(
        OrganizationAgentMember.organization_id
    ).all()
    active_by_org = {int(row.organization_id): int(row.active_agents_7d or 0) for row in active_rows}

    last_rows = db.session.query(
        OrganizationAgentMember.organization_id,
        func.max(AgentTaskAttempt.started_at).label('last_agent_activity_at'),
    ).join(
        Agent, Agent.id == OrganizationAgentMember.agent_id
    ).join(
        AgentTaskAttempt, AgentTaskAttempt.agent_id == Agent.id
    ).filter(
        OrganizationAgentMember.organization_id.in_(org_ids),
        OrganizationAgentMember.status != OrganizationAgentMemberStatus.REMOVED,
    ).group_by(
        OrganizationAgentMember.organization_id
    ).all()
    last_by_org = {int(row.organization_id): row.last_agent_activity_at for row in last_rows}

    items = []
    for org_id in org_ids:
        org = org_map[org_id]
        items.append(
            {
                'organization_id': org_id,
                'organization_name': org['name'],
                'my_role': org['my_role'],
                'total_agents': int(total_by_org.get(org_id, 0)),
                'active_agents_7d': int(active_by_org.get(org_id, 0)),
                'last_agent_activity_at': last_by_org.get(org_id).isoformat() if last_by_org.get(org_id) else None,
                'organization_updated_at': org['updated_at'],
            }
        )

    items.sort(
        key=lambda item: (
            item['active_agents_7d'],
            item['last_agent_activity_at'] or '',
            item['organization_updated_at'] or '',
        ),
        reverse=True,
    )

    top_items = []
    for item in items[:5]:
        top_items.append(
            {
                'organization_id': item['organization_id'],
                'organization_name': item['organization_name'],
                'my_role': item['my_role'],
                'total_agents': item['total_agents'],
                'active_agents_7d': item['active_agents_7d'],
                'last_agent_activity_at': item['last_agent_activity_at'],
            }
        )

    return {
        'summary': {
            'total': len(org_ids),
            'total_agents': sum(total_by_org.values()),
            'active_agents_7d': sum(active_by_org.values()),
        },
        'top_organizations': top_items,
    }


def _build_dashboard_stats(user_id):
    owned_project_query = Project.query.filter(Project.owner_id == user_id)
    owned_task_query = Task.query.filter(Task.owner_id == user_id)
    owned_scope = _build_scope_stats(owned_project_query, owned_task_query)

    participated_project_ids = _get_participated_project_ids(user_id)
    if participated_project_ids:
        participated_project_query = Project.query.filter(Project.id.in_(participated_project_ids))
        participated_task_query = Task.query.filter(Task.project_id.in_(participated_project_ids))
        participated_scope = _build_scope_stats(participated_project_query, participated_task_query)
    else:
        participated_scope = {
            'projects': _empty_project_stats(),
            'tasks': _empty_task_stats(),
        }

    if owned_scope['tasks']['total'] > LARGE_DATASET_THRESHOLD:
        recent_tasks = []
    else:
        recent_tasks = owned_task_query.options(joinedload(Task.project)).order_by(Task.updated_at.desc()).limit(5).all()

    recent_projects = owned_project_query.order_by(Project.updated_at.desc()).limit(5).all()
    activity_stats = UserActivity.get_user_activity_stats(user_id, days=30)
    organization_agent_stats = _build_organization_agent_stats(_get_accessible_organizations(user_id))

    return {
        # backward compatible fields (mapped to owned scope)
        'projects': owned_scope['projects'],
        'tasks': owned_scope['tasks'],
        'scopes': {
            'owned': owned_scope,
            'participated': participated_scope,
        },
        'organizations': organization_agent_stats,
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
        cache_key = f"user:{current_user.id}:stats:v2"
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
