"""Shared helper utilities for tasks APIs."""

from datetime import datetime

from sqlalchemy import and_, or_

from models import (
    db,
    Project,
    ProjectMember,
    ProjectMemberStatus,
    TaskLabel,
    BUILTIN_TASK_LABELS,
)
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from core.cache_invalidation import invalidate_user_caches

from .constants import TASKS_LIST_CACHE_TTL_SECONDS, tasks_list_fallback_cache

def _normalize_tags(raw_tags):
    if not raw_tags:
        return []
    normalized = []
    seen = set()
    for raw in raw_tags:
        tag = str(raw).strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def _ensure_builtin_labels():
    for item in BUILTIN_TASK_LABELS:
        exists = TaskLabel.query.filter(
            TaskLabel.is_builtin.is_(True),
            TaskLabel.name == item['name'],
            TaskLabel.owner_id.is_(None),
            TaskLabel.project_id.is_(None)
        ).first()
        if exists:
            # 如果内置标签被误禁用或属性漂移，自动修复
            exists.is_active = True
            exists.color = item['color']
            exists.description = item['description']
            continue
        TaskLabel.create(
            owner_id=None,
            project_id=None,
            name=item['name'],
            color=item['color'],
            description=item['description'],
            is_builtin=True,
            is_active=True,
            created_by='system',
            created_by_user_id=None,
        )
    db.session.flush()


def _ensure_labels_for_tags(current_user, project_id, tags):
    if not tags:
        return
    _ensure_builtin_labels()
    for tag in tags:
        exists = TaskLabel.query.filter(
            or_(
                and_(TaskLabel.is_builtin.is_(True), TaskLabel.name == tag),
                and_(TaskLabel.owner_id == current_user.id, TaskLabel.project_id == project_id, TaskLabel.name == tag),
                and_(TaskLabel.owner_id == current_user.id, TaskLabel.project_id.is_(None), TaskLabel.name == tag),
            )
        ).first()
        if exists:
            # 软删除标签在任务输入中再次出现时，自动恢复可见性
            if not exists.is_active:
                exists.is_active = True
            continue
        TaskLabel.create(
            owner_id=current_user.id,
            project_id=project_id,
            name=tag,
            color='#1677ff',
            description='',
            is_builtin=False,
            is_active=True,
            created_by=current_user.email,
            created_by_user_id=current_user.id,
        )
    db.session.flush()


def _normalize_participants(raw_items):
    if not raw_items:
        return []

    normalized = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        ptype = str(item.get('type') or '').strip().lower()
        if ptype not in {'human', 'agent'}:
            continue

        try:
            pid = int(item.get('id'))
        except Exception:
            continue

        key = f"{ptype}:{pid}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append({'type': ptype, 'id': pid})
    return normalized


def _invalidate_project_users(project_id):
    project = Project.query.get(project_id)
    if not project:
        return
    user_ids = {project.owner_id}
    member_rows = db.session.query(ProjectMember.user_id).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.status == ProjectMemberStatus.ACTIVE
    ).all()
    user_ids.update([row.user_id for row in member_rows])
    for user_id in user_ids:
        invalidate_user_caches(user_id)


def _tasks_cache_get(key):
    redis_key = f"tasks:list:{key}"
    cached = redis_get_json(redis_key)
    if cached is not None:
        return cached

    item = tasks_list_fallback_cache.get(key)
    if item and (datetime.utcnow().timestamp() - item['cached_at'] <= TASKS_LIST_CACHE_TTL_SECONDS):
        return item['value']
    return None


def _tasks_cache_set(key, value):
    redis_key = f"tasks:list:{key}"
    redis_set_json(redis_key, value, TASKS_LIST_CACHE_TTL_SECONDS)
    tasks_list_fallback_cache[key] = {
        'cached_at': datetime.utcnow().timestamp(),
        'value': value,
    }
