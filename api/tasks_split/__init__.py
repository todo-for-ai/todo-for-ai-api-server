"""
任务API模块 - 拆分版本
遵循MCP模块的拆分模式
"""

from flask import Blueprint
from . import list, detail, create, update, batch

tasks_bp = Blueprint('tasks', __name__)

list.register_routes(tasks_bp)
detail.register_routes(tasks_bp)
create.register_routes(tasks_bp)
update.register_routes(tasks_bp)
batch.register_routes(tasks_bp)

__all__ = ['tasks_bp']
