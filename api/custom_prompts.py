"""
Custom Prompts API Blueprint

提供自定义提示词的管理接口

已模块化拆分：
- crud/: 基础CRUD操作（列表、创建、获取、更新、删除）
- project/: 项目相关提示词（获取、预览）
- task_button/: 任务按钮提示词（获取、重排序）
- init/: 初始化默认提示词
"""

from flask import Blueprint

# Import all routes from submodules
from .crud.list import get_custom_prompts
from .crud.create import create_custom_prompt
from .crud.get import get_custom_prompt
from .crud.update import update_custom_prompt
from .crud.delete import delete_custom_prompt
from .project.prompts import get_project_prompts, preview_project_prompt
from .task_button.buttons import get_task_button_prompts, reorder_task_button_prompts
from .init.defaults import initialize_user_defaults

# Re-export the blueprint
from .custom_prompts_submodule import custom_prompts_bp

__all__ = [
    'custom_prompts_bp',
    'get_custom_prompts',
    'create_custom_prompt',
    'get_custom_prompt',
    'update_custom_prompt',
    'delete_custom_prompt',
    'get_project_prompts',
    'preview_project_prompt',
    'get_task_button_prompts',
    'reorder_task_button_prompts',
    'initialize_user_defaults'
]
