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
    def get_default_project_template(cls):
        """获取默认的项目提示词模板"""
        return """# 项目上下文

## 项目信息
- **项目名称**: {{project.name}}
- **项目描述**: {{project.description}}
- **创建时间**: {{project.created_at}}
- **任务总数**: {{tasks.length}}

## 任务列表
{{#each tasks}}
### 任务 {{this.id}}: {{this.title}}
- **状态**: {{this.status}}
- **优先级**: {{this.priority}}
- **创建时间**: {{this.created_at}}
- **描述**: {{this.content}}
{{#if this.tags}}
- **标签**: {{join this.tags ", "}}
{{/if}}

{{/each}}

## 上下文规则
{{context_rules}}

请基于以上项目信息和任务列表，为我提供相关的帮助和建议。"""

    @classmethod
    def get_default_task_buttons(cls):
        """获取默认的任务按钮提示词"""
        return [
            {
                'name': '分析任务',
                'content': '请分析这个任务：\n\n**任务标题**: {{task.title}}\n**任务描述**: {{task.content}}\n**当前状态**: {{task.status}}\n**优先级**: {{task.priority}}\n\n请提供：\n1. 任务分析和理解\n2. 实施建议\n3. 可能的风险和注意事项',
                'description': '分析当前任务的详细信息和实施建议',
                'order_index': 1
            },
            {
                'name': '生成子任务',
                'content': '基于这个任务，请帮我生成详细的子任务列表：\n\n**主任务**: {{task.title}}\n**任务描述**: {{task.content}}\n\n请生成：\n1. 具体的执行步骤\n2. 每个步骤的预估时间\n3. 依赖关系和优先级',
                'description': '为当前任务生成详细的子任务分解',
                'order_index': 2
            },
            {
                'name': '代码实现',
                'content': '请帮我实现这个任务的代码：\n\n**任务**: {{task.title}}\n**需求**: {{task.content}}\n**项目**: {{project.name}}\n\n请提供：\n1. 代码实现方案\n2. 关键代码片段\n3. 测试建议',
                'description': '为编程任务提供代码实现建议',
                'order_index': 3
            }
        ]

    @classmethod
    def initialize_user_defaults(cls, user_id):
        """为新用户初始化默认的提示词"""
        # 创建默认项目提示词
        project_prompt = cls.create_prompt(
            user_id=user_id,
            prompt_type=PromptType.PROJECT,
            name='默认项目模板',
            content=cls.get_default_project_template(),
            description='默认的项目上下文提示词模板'
        )
        
        # 创建默认任务按钮提示词
        default_buttons = cls.get_default_task_buttons()
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
