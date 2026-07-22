"""
Task template API routes.

CRUD operations for task templates and template instantiation.
Extracted from api/agents/_core.py for better organization.
"""

from datetime import datetime

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
    TaskTemplate,
    Task,
    TaskStatus,
    TaskPriority,
    TaskHistory,
    ActionType,
    Project,
)


@agents_bp.route("/task-templates", methods=["GET"])
@unified_auth_required
def list_task_templates():
    """List the current user's task templates."""
    try:
        current_user = get_current_user()
        templates = TaskTemplate.query.filter_by(owner_id=current_user.id).order_by(TaskTemplate.name.asc()).all()
        return ApiResponse.success(
            [t.to_dict() for t in templates],
            "Task templates retrieved",
        ).to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to list task templates: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates", methods=["POST"])
@unified_auth_required
def create_task_template():
    """Create a new task template.

    Body:
        name (required), description, title_template, content_template,
        priority, tags, is_ai_task, capabilities
    """
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=["name"],
            optional_fields=[
                "description", "title_template", "content_template",
                "priority", "tags", "is_ai_task", "capabilities",
            ],
        )
        if isinstance(data, tuple):
            return data

        template = TaskTemplate(
            owner_id=current_user.id,
            name=data["name"],
            description=data.get("description", ""),
            title_template=data.get("title_template", ""),
            content_template=data.get("content_template", ""),
            priority=data.get("priority", "medium"),
            tags=data.get("tags", []),
            is_ai_task=data.get("is_ai_task", False),
            capabilities=data.get("capabilities", []),
        )
        db.session.add(template)
        db.session.commit()
        return ApiResponse.created(template.to_dict(), "Task template created").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create task template: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates/<int:template_id>", methods=["PUT"])
@unified_auth_required
def update_task_template(template_id):
    """Update a task template."""
    try:
        current_user = get_current_user()
        template = TaskTemplate.query.filter_by(id=template_id, owner_id=current_user.id).first()
        if not template:
            return ApiResponse.error("Task template not found", 404).to_response()

        data = validate_json_request(
            optional_fields=[
                "name", "description", "title_template", "content_template",
                "priority", "tags", "is_ai_task", "capabilities",
            ],
        )
        if isinstance(data, tuple):
            return data

        for field in ["name", "description", "title_template", "content_template", "priority", "is_ai_task"]:
            if field in data:
                setattr(template, field, data[field])
        if "tags" in data:
            template.tags = data["tags"]
        if "capabilities" in data:
            template.capabilities = data["capabilities"]

        db.session.commit()
        return ApiResponse.success(template.to_dict(), "Task template updated").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update task template: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates/<int:template_id>", methods=["DELETE"])
@unified_auth_required
def delete_task_template(template_id):
    """Delete a task template."""
    try:
        current_user = get_current_user()
        template = TaskTemplate.query.filter_by(id=template_id, owner_id=current_user.id).first()
        if not template:
            return ApiResponse.error("Task template not found", 404).to_response()

        db.session.delete(template)
        db.session.commit()
        return ApiResponse.success(None, "Task template deleted").to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete task template: {str(e)}", 500).to_response()


@agents_bp.route("/task-templates/<int:template_id>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_task_template(template_id):
    """Create a new task from a template.

    Body:
        project_id (required) — the project to create the task in.
        title — override template title (optional)
        content — override template content (optional)
    """
    try:
        current_user = get_current_user()
        template = TaskTemplate.query.filter_by(id=template_id, owner_id=current_user.id).first()
        if not template:
            return ApiResponse.error("Task template not found", 404).to_response()

        data = validate_json_request(
            required_fields=["project_id"],
            optional_fields=["title", "content"],
        )
        if isinstance(data, tuple):
            return data

        project = Project.query.get(data["project_id"])
        if not project or project.owner_id != current_user.id:
            return ApiResponse.error("Project not found or access denied", 404).to_response()

        title = data.get("title") or template.title_template or template.name
        content = data.get("content") or template.content_template or ""

        try:
            priority = TaskPriority(template.priority)
        except ValueError:
            priority = TaskPriority.MEDIUM

        task = Task.create(
            project_id=project.id,
            title=title,
            content=content,
            status=TaskStatus.TODO,
            priority=priority,
            tags=template.tags or [],
            is_ai_task=template.is_ai_task,
            creator_id=current_user.id,
            created_by=current_user.email,
        )
        project.last_activity_at = datetime.utcnow()
        db.session.commit()

        TaskHistory.log_action(
            task_id=task.id,
            action=ActionType.CREATED,
            changed_by='api',
            comment=f'Task created from template "{template.name}"',
        )

        return ApiResponse.created(
            task.to_dict(include_project=True, include_stats=True),
            f'Task created from template "{template.name}"',
        ).to_response()
    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to instantiate task template: {str(e)}", 500).to_response()
