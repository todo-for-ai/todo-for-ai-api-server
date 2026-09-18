from flask import Blueprint
from .base import create_success_response, create_error_response

tasks_bp = Blueprint('tasks', __name__)

@tasks_bp.route('/', methods=['GET'])
def list_tasks():
    """获取任务列表"""
    return create_success_response([]).to_response()

@tasks_bp.route('/', methods=['POST'])
def create_task():
    """创建任务"""
    return create_success_response({}, "Task created").to_response()
