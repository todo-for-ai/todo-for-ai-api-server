"""
任务标签 API 蓝图
"""

from flask import Blueprint, request
from sqlalchemy import or_
from models import db, TaskLabel, BUILTIN_TASK_LABELS, Project
from .base import ApiResponse, validate_json_request
from core.auth import unified_auth_required, get_current_user
from core.cache_invalidation import invalidate_user_caches

task_labels_bp = Blueprint('task_labels', __name__)


def ensure_builtin_labels():
    for item in BUILTIN_TASK_LABELS:
        exists = TaskLabel.query.filter(
            TaskLabel.is_builtin.is_(True),
            TaskLabel.name == item['name'],
            TaskLabel.owner_id.is_(None),
            TaskLabel.project_id.is_(None)
        ).first()
        if exists:
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


@task_labels_bp.route('', methods=['GET'])
@task_labels_bp.route('/', methods=['GET'])
@unified_auth_required
def list_task_labels():
    try:
        current_user = get_current_user()
        project_id = request.args.get('project_id', type=int)
        include_inactive = request.args.get('include_inactive', 'false').lower() == 'true'

        ensure_builtin_labels()

        query = TaskLabel.query.filter(
            or_(
                TaskLabel.is_builtin.is_(True),
                TaskLabel.owner_id == current_user.id
            )
        )
        if project_id is not None:
            project = Project.query.get(project_id)
            if not project:
                return ApiResponse.not_found("Project not found").to_response()
            if not current_user.can_access_project(project):
                return ApiResponse.forbidden("Access denied").to_response()
            query = query.filter(
                or_(TaskLabel.project_id == project_id, TaskLabel.project_id.is_(None))
            )
        if not include_inactive:
            query = query.filter(TaskLabel.is_active.is_(True))

        labels = query.order_by(
            TaskLabel.is_builtin.desc(),
            TaskLabel.name.asc()
        ).all()

        return ApiResponse.success(
            {'items': [label.to_dict() for label in labels]},
            "Task labels retrieved successfully"
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve task labels: {str(e)}", 500).to_response()


@task_labels_bp.route('', methods=['POST'])
@task_labels_bp.route('/', methods=['POST'])
@unified_auth_required
def create_task_label():
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=['name'],
            optional_fields=['project_id', 'color', 'description']
        )
        if isinstance(data, tuple):
            return data

        ensure_builtin_labels()

        name = data['name'].strip().lower()
        if not name:
            return ApiResponse.error("Label name cannot be empty", 400).to_response()

        project_id = data.get('project_id')
        if project_id is not None:
            project = Project.query.get(project_id)
            if not project:
                return ApiResponse.not_found("Project not found").to_response()
            if not current_user.can_access_project(project):
                return ApiResponse.forbidden("Access denied").to_response()

        existing = TaskLabel.query.filter(
            TaskLabel.owner_id == current_user.id,
            TaskLabel.project_id == project_id,
            TaskLabel.name == name
        ).first()
        if existing:
            if not existing.is_active:
                existing.is_active = True
                existing.color = data.get('color') or existing.color
                existing.description = data.get('description') or existing.description
                db.session.commit()
                invalidate_user_caches(current_user.id)
                return ApiResponse.success(
                    existing.to_dict(),
                    "Task label reactivated successfully"
                ).to_response()
            return ApiResponse.error("Label already exists", 409).to_response()

        label = TaskLabel.create(
            owner_id=current_user.id,
            project_id=project_id,
            name=name,
            color=data.get('color') or '#1677ff',
            description=data.get('description') or '',
            is_builtin=False,
            is_active=True,
            created_by=current_user.email,
            created_by_user_id=current_user.id,
        )
        db.session.commit()
        invalidate_user_caches(current_user.id)

        return ApiResponse.created(label.to_dict(), "Task label created successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create task label: {str(e)}", 500).to_response()


@task_labels_bp.route('/<int:label_id>', methods=['PUT'])
@unified_auth_required
def update_task_label(label_id):
    try:
        current_user = get_current_user()
        label = TaskLabel.query.get(label_id)
        if not label:
            return ApiResponse.not_found("Task label not found").to_response()
        if label.is_builtin:
            return ApiResponse.error("Builtin labels cannot be modified", 400).to_response()
        if label.owner_id != current_user.id:
            return ApiResponse.forbidden("Access denied").to_response()

        data = validate_json_request(optional_fields=['name', 'color', 'description', 'is_active'])
        if isinstance(data, tuple):
            return data

        if 'name' in data and data['name']:
            new_name = data['name'].strip().lower()
            if new_name != label.name:
                dup = TaskLabel.query.filter(
                    TaskLabel.owner_id == current_user.id,
                    TaskLabel.project_id == label.project_id,
                    TaskLabel.name == new_name,
                    TaskLabel.id != label.id
                ).first()
                if dup:
                    return ApiResponse.error("Label already exists", 409).to_response()
                label.name = new_name

        for key in ['color', 'description', 'is_active']:
            if key in data:
                setattr(label, key, data[key])

        db.session.commit()
        invalidate_user_caches(current_user.id)
        return ApiResponse.success(label.to_dict(), "Task label updated successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update task label: {str(e)}", 500).to_response()


@task_labels_bp.route('/<int:label_id>', methods=['DELETE'])
@unified_auth_required
def delete_task_label(label_id):
    try:
        current_user = get_current_user()
        label = TaskLabel.query.get(label_id)
        if not label:
            return ApiResponse.not_found("Task label not found").to_response()
        if label.is_builtin:
            return ApiResponse.error("Builtin labels cannot be deleted", 400).to_response()
        if label.owner_id != current_user.id:
            return ApiResponse.forbidden("Access denied").to_response()

        # 软删除：保留历史任务上的 tags 字符串
        label.is_active = False
        db.session.commit()
        invalidate_user_caches(current_user.id)
        return ApiResponse.success(None, "Task label deleted successfully").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete task label: {str(e)}", 500).to_response()
