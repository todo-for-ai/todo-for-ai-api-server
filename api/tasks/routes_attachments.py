"""Task attachment routes."""

import os
import uuid

from flask import request, send_file
from werkzeug.utils import secure_filename

from models import Task, Attachment
from ..base import ApiResponse
from core.auth import unified_auth_required, get_current_user

from . import tasks_bp
from .constants import ALLOWED_ATTACHMENT_EXTENSIONS, MAX_ATTACHMENT_SIZE_BYTES

@tasks_bp.route('/<int:task_id>/attachments', methods=['GET'])
@unified_auth_required
def get_task_attachments(task_id):
    """获取任务附件列表"""
    try:
        current_user = get_current_user()

        # 验证任务是否存在
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 权限检查 - 只能访问自己项目中的任务附件
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        result = [item.to_dict() for item in Attachment.get_task_attachments(task_id)]

        return ApiResponse.success(
            result,
            "Task attachments retrieved successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve task attachments: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/attachments/<int:attachment_id>', methods=['DELETE'])
@unified_auth_required
def delete_task_attachment(task_id, attachment_id):
    """删除任务附件"""
    try:
        current_user = get_current_user()

        # 验证任务是否存在
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.error("Task not found", 404, error_details={"code": "TASK_NOT_FOUND"}).to_response()

        # 权限检查 - 只能删除自己项目中的任务附件
        if not current_user.can_access_project(task.project):
            return ApiResponse.error("Access denied", 403, error_details={"code": "PERMISSION_DENIED"}).to_response()

        attachment = Attachment.query.filter_by(id=attachment_id, task_id=task_id).first()
        if not attachment:
            return ApiResponse.not_found("Attachment not found").to_response()

        attachment.delete_file()

        return ApiResponse.success(
            None,
            f"Task attachment {attachment_id} deleted successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to delete task attachment: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/attachments', methods=['POST'])
@unified_auth_required
def upload_task_attachment(task_id):
    """上传任务附件"""
    try:
        current_user = get_current_user()
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.not_found("Task not found").to_response()

        if not current_user.can_access_project(task.project):
            return ApiResponse.forbidden("Access denied").to_response()

        uploaded_file = request.files.get('file')
        if not uploaded_file:
            return ApiResponse.error("Missing file field", 400).to_response()

        original_filename = uploaded_file.filename or ''
        if not original_filename.strip():
            return ApiResponse.error("Empty filename", 400).to_response()

        ext = os.path.splitext(original_filename)[1].lower()
        if ext and ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
            return ApiResponse.error(f"File extension {ext} is not allowed", 400).to_response()

        upload_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'uploads', 'tasks', str(task_id))
        os.makedirs(upload_root, exist_ok=True)

        safe_name = secure_filename(original_filename) or f"file{ext or ''}"
        stored_name = f"{uuid.uuid4().hex}_{safe_name}"
        abs_path = os.path.join(upload_root, stored_name)
        uploaded_file.save(abs_path)

        file_size = os.path.getsize(abs_path)
        if file_size > MAX_ATTACHMENT_SIZE_BYTES:
            os.remove(abs_path)
            return ApiResponse.error(f"File too large. Max size is {MAX_ATTACHMENT_SIZE_BYTES} bytes", 400).to_response()

        attachment = Attachment.create_attachment(
            task_id=task_id,
            filename=stored_name,
            original_filename=original_filename,
            file_path=abs_path,
            file_size=file_size,
            mime_type=uploaded_file.mimetype,
            uploaded_by=current_user.email,
        )

        return ApiResponse.created(attachment.to_dict(), "Task attachment uploaded successfully").to_response()
    except Exception as e:
        return ApiResponse.error(f"Failed to upload task attachment: {str(e)}", 500).to_response()


@tasks_bp.route('/<int:task_id>/attachments/<int:attachment_id>/download', methods=['GET'])
@unified_auth_required
def download_task_attachment(task_id, attachment_id):
    """下载任务附件"""
    try:
        current_user = get_current_user()
        task = Task.query.get(task_id)
        if not task:
            return ApiResponse.not_found("Task not found").to_response()

        if not current_user.can_access_project(task.project):
            return ApiResponse.forbidden("Access denied").to_response()

        attachment = Attachment.query.filter_by(id=attachment_id, task_id=task_id).first()
        if not attachment:
            return ApiResponse.not_found("Attachment not found").to_response()

        if not os.path.exists(attachment.file_path):
            return ApiResponse.not_found("Attachment file not found").to_response()

        return send_file(
            attachment.file_path,
            as_attachment=True,
            download_name=attachment.original_filename,
            mimetype=attachment.mime_type or 'application/octet-stream',
        )
    except Exception as e:
        return ApiResponse.error(f"Failed to download task attachment: {str(e)}", 500).to_response()
