"""Shared helpers for project APIs."""

from datetime import datetime

from sqlalchemy import or_

from models import db, Project, ProjectMember, ProjectMemberStatus
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from core.cache_invalidation import invalidate_user_caches

PROJECTS_LIST_CACHE_TTL_SECONDS = 20
PROJECTS_LIST_HEAVY_CACHE_TTL_SECONDS = 120
TASK_SORT_FIELDS = {'total_tasks', 'pending_tasks', 'completed_tasks'}
projects_list_fallback_cache = {}


def _projects_cache_get(key):
    redis_key = f"projects:list:{key}"
    cached = redis_get_json(redis_key)
    if cached is not None:
        return cached

    item = projects_list_fallback_cache.get(key)
    if item:
        ttl_seconds = item.get('ttl', PROJECTS_LIST_CACHE_TTL_SECONDS)
        if datetime.utcnow().timestamp() - item['cached_at'] <= ttl_seconds:
            return item['value']
    return None


def _projects_cache_set(key, value, ttl=PROJECTS_LIST_CACHE_TTL_SECONDS):
    redis_key = f"projects:list:{key}"
    redis_set_json(redis_key, value, ttl)
    projects_list_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'ttl': ttl,
        'value': value,
    }


def _accessible_projects_query(current_user):
    member_project_ids = db.session.query(ProjectMember.project_id).filter(
        ProjectMember.user_id == current_user.id,
        ProjectMember.status == ProjectMemberStatus.ACTIVE
    ).subquery()
    return Project.query.filter(
        or_(
            Project.owner_id == current_user.id,
            Project.id.in_(member_project_ids)
        )
    )


def _project_member_user_ids(project_id):
    user_ids = set()
    project = Project.query.get(project_id)
    if not project:
        return user_ids
    user_ids.add(project.owner_id)
    member_rows = db.session.query(ProjectMember.user_id).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.status == ProjectMemberStatus.ACTIVE
    ).all()
    user_ids.update([row.user_id for row in member_rows])
    return user_ids


def _invalidate_project_users(project_id):
    for user_id in _project_member_user_ids(project_id):
        invalidate_user_caches(user_id)
