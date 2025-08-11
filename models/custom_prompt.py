"""
自定义提示词模型
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, Enum
from sqlalchemy.orm import relationship
from models import db
import enum


class PromptType(enum.Enum):
    """提示词类型枚举"""
    PROJECT = "project"  # 项目提示词
    TASK_BUTTON = "task_button"  # 任务详情页按钮提示词


class CustomPrompt(db.Model):
    """自定义提示词模型"""
    __tablename__ = 'custom_prompts'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    prompt_type = Column(Enum(PromptType), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # 提示词名称
    content = Column(Text, nullable=False)  # 提示词内容
    description = Column(Text)  # 描述
    is_active = Column(Boolean, default=True, nullable=False)  # 是否激活
    order_index = Column(Integer, default=0)  # 排序索引（用于任务按钮排序）
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # 关联关系
    user = relationship("User", back_populates="custom_prompts")

    def __repr__(self):
        return f'<CustomPrompt {self.id}: {self.name} ({self.prompt_type.value})>'

    def to_dict(self, include_user=False):
        """转换为字典格式"""
        result = {
            'id': self.id,
            'user_id': self.user_id,
            'prompt_type': self.prompt_type.value,
            'name': self.name,
            'content': self.content,
            'description': self.description,
            'is_active': self.is_active,
            'order_index': self.order_index,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
        
        if include_user and self.user:
            result['user'] = {
                'id': self.user.id,
                'username': self.user.username,
                'display_name': self.user.display_name
            }
        
        return result

    @classmethod
    def get_user_prompts(cls, user_id, prompt_type=None, is_active=None):
        """获取用户的提示词列表"""
        query = cls.query.filter(cls.user_id == user_id)
        
        if prompt_type:
            query = query.filter(cls.prompt_type == prompt_type)
        
        if is_active is not None:
            query = query.filter(cls.is_active == is_active)
        
        return query.order_by(cls.order_index.asc(), cls.created_at.desc()).all()

    @classmethod
    def get_user_project_prompts(cls, user_id, is_active=True):
        """获取用户的项目提示词列表"""
        return cls.get_user_prompts(user_id, PromptType.PROJECT, is_active)

    @classmethod
    def get_user_task_button_prompts(cls, user_id, is_active=True):
        """获取用户的任务按钮提示词列表"""
        return cls.get_user_prompts(user_id, PromptType.TASK_BUTTON, is_active)

    @classmethod
    def create_prompt(cls, user_id, prompt_type, name, content, description=None, order_index=0):
        """创建新的提示词"""
        prompt = cls(
            user_id=user_id,
            prompt_type=prompt_type,
            name=name,
            content=content,
            description=description,
            order_index=order_index
        )
        db.session.add(prompt)
        return prompt

    def update_prompt(self, name=None, content=None, description=None, is_active=None, order_index=None):
        """更新提示词"""
        if name is not None:
            self.name = name
        if content is not None:
            self.content = content
        if description is not None:
            self.description = description
        if is_active is not None:
            self.is_active = is_active
        if order_index is not None:
            self.order_index = order_index
        
        self.updated_at = datetime.utcnow()

    @classmethod
    def reorder_task_buttons(cls, user_id, prompt_orders):
        """重新排序任务按钮提示词
        
        Args:
            user_id: 用户ID
            prompt_orders: 列表，包含 {'id': prompt_id, 'order_index': new_order} 的字典
        """
        for order_data in prompt_orders:
            prompt = cls.query.filter(
                cls.id == order_data['id'],
                cls.user_id == user_id,
                cls.prompt_type == PromptType.TASK_BUTTON
            ).first()
            
            if prompt:
                prompt.order_index = order_data['order_index']
                prompt.updated_at = datetime.utcnow()

    @classmethod
    def get_default_project_template(cls, language='zh-CN'):
        """获取默认的项目提示词模板"""
        if language == 'en':
            return """Please help me execute all pending tasks in project "${project.name}":

**Project Information**:
- Project Name: ${project.name}
- Project Description: ${project.description}
- GitHub Repository: ${project.github_repo}
- Project Context: ${project.context}

**Number of Tasks to Execute**: ${tasks.count} tasks

**Execution Guidelines**:
1. Please use MCP tools to connect to Todo system: ${system.url}
2. Use get_project_tasks_by_name tool to get project task list:
   - Project Name: "${project.name}"
   - Status Filter: ["todo", "in_progress", "review"]
3. Execute tasks in order of creation time
4. For each task, use get_task_by_id to get detailed information
5. After completing a task, use submit_task_feedback to submit feedback
6. Continue to the next task until all tasks are completed

**Task Overview**:
${tasks.list}

Please start executing the tasks in this project and submit feedback after each task is completed."""
        else:
            # 默认中文模板
            return """请帮我执行项目"${project.name}"中的所有待办任务：

**项目信息**:
- 项目名称: ${project.name}
- 项目描述: ${project.description}
- GitHub仓库: ${project.github_repo}
- 项目上下文: ${project.context}

**待执行任务数量**: ${tasks.count}个

**执行指引**:
1. 请使用MCP工具连接到Todo系统: ${system.url}
2. 使用get_project_tasks_by_name工具获取项目任务列表:
   - 项目名称: "${project.name}"
   - 状态筛选: ["todo", "in_progress", "review"]
3. 按照任务的创建时间顺序，逐个执行任务
4. 对于每个任务，使用get_task_by_id获取详细信息
5. 完成任务后，使用submit_task_feedback提交反馈
6. 继续执行下一个任务，直到所有任务完成

**任务概览**:
${tasks.list}

请开始执行这个项目的任务，并在每个任务完成后提交反馈。"""

    @classmethod
    def get_default_task_buttons(cls, language='zh-CN'):
        """获取默认的任务按钮提示词"""
        if language == 'en':
            return [
                {
                    'name': 'MCP Execution',
                    'content': 'Please use the todo-for-ai MCP tool to get detailed information for task ID ${task.id}, then execute this task and submit a task feedback report upon completion.',
                    'description': 'Execute task using MCP tools and submit feedback',
                    'order_index': 1
                },
                {
                    'name': 'Execute Task',
                    'content': 'Please help me execute the following task:\n\n**Task Information**:\n- Task ID: ${task.id}\n- Task Title: ${task.title}\n- Task Content: ${task.content}\n- Task Status: ${task.status}\n- Priority: ${task.priority}\n- Created Time: ${task.created_at}\n- Due Date: ${task.due_date}\n- Estimated Hours: ${task.estimated_hours}\n- Tags: ${task.tags}\n- Related Files: ${task.related_files}\n\n**Project Information**:\n- Project Name: ${project.name}\n- Project Description: ${project.description}\n\nPlease execute this task and submit feedback.',
                    'description': 'Execute task with detailed information and submit feedback',
                    'order_index': 2
                }
            ]
        else:
            # 默认中文按钮
            return [
                {
                    'name': 'MCP执行',
                    'content': '请使用todo-for-ai MCP工具获取任务ID为${task.id}的详细信息，然后执行这个任务，完成后提交任务反馈报告。',
                    'description': '使用MCP工具执行任务并提交反馈',
                    'order_index': 1
                },
                {
                    'name': '执行任务',
                    'content': '请帮我执行以下任务：\n\n**任务信息**:\n- 任务ID: ${task.id}\n- 任务标题: ${task.title}\n- 任务内容: ${task.content}\n- 任务状态: ${task.status}\n- 优先级: ${task.priority}\n- 创建时间: ${task.created_at}\n- 截止时间: ${task.due_date}\n- 预估工时: ${task.estimated_hours}\n- 标签: ${task.tags}\n- 相关文件: ${task.related_files}\n\n**项目信息**:\n- 项目名称: ${project.name}\n- 项目描述: ${project.description}\n\n请执行这个任务并提交反馈。',
                    'description': '执行任务并提交详细反馈',
                    'order_index': 2
                }
            ]

    @classmethod
    def initialize_user_defaults(cls, user_id, language='zh-CN'):
        """为新用户初始化默认的提示词"""
        # 根据语言设置名称和描述
        if language == 'en':
            project_name = 'Default Project Template'
            project_description = 'Default project context prompt template'
        else:
            project_name = '默认项目模板'
            project_description = '默认的项目上下文提示词模板'

        # 创建默认项目提示词
        project_prompt = cls.create_prompt(
            user_id=user_id,
            prompt_type=PromptType.PROJECT,
            name=project_name,
            content=cls.get_default_project_template(language),
            description=project_description
        )

        # 创建默认任务按钮提示词
        default_buttons = cls.get_default_task_buttons(language)
        for button_data in default_buttons:
            cls.create_prompt(
                user_id=user_id,
                prompt_type=PromptType.TASK_BUTTON,
                name=button_data['name'],
                content=button_data['content'],
                description=button_data['description'],
                order_index=button_data['order_index']
            )

        db.session.commit()
        return True
