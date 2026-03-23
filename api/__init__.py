"""
API 蓝图包

包含所有 API 相关的蓝图和工具函数
"""

from .base import ApiResponse
from .projects import projects_bp
from .tasks import tasks_bp
from .context_rules import context_rules_bp
from .organizations import organizations_bp
from .task_labels import task_labels_bp
from .ai_task_assistant import ai_task_assistant_bp
from .ai_task_split import ai_task_split_bp
from .ai_summarize import ai_summarize_bp

__all__ = [
    'ApiResponse',
    'projects_bp',
    'tasks_bp',
    'context_rules_bp',
    'organizations_bp',
    'task_labels_bp',
    'ai_task_assistant_bp',
    'ai_task_split_bp',
    'ai_summarize_bp',
]
