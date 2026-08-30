"""
任务标签模型
"""

from sqlalchemy import Column, String, Integer, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


BUILTIN_TASK_LABELS = [
    {'name': 'task', 'color': '#1677ff', 'description': 'General task'},
    {'name': 'bug', 'color': '#ff4d4f', 'description': 'Bug fix'},
    {'name': 'improvement', 'color': '#722ed1', 'description': 'Improvement suggestion'},
    {'name': 'feature', 'color': '#13c2c2', 'description': 'Feature request'},
    {'name': 'urgent', 'color': '#fa541c', 'description': 'Urgent'},
    {'name': 'research', 'color': '#2f54eb', 'description': 'Research'},
    {'name': 'refactor', 'color': '#fa8c16', 'description': 'Refactor'},
    {'name': 'documentation', 'color': '#52c41a', 'description': 'Documentation'},
]


class TaskLabel(BaseModel):
    """任务标签字典（全局内置 + 用户项目自定义）"""

    __tablename__ = 'task_labels'

    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True, comment='所属用户ID，内置标签为空')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, index=True, comment='项目ID，空表示全局标签')
    name = Column(String(64), nullable=False, comment='标签名')
    color = Column(String(16), default='#1677ff', nullable=False, comment='标签颜色')
    description = Column(String(255), nullable=True, comment='标签描述')
    is_builtin = Column(Boolean, default=False, nullable=False, comment='是否内置标签')
    is_active = Column(Boolean, default=True, nullable=False, comment='是否启用')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, comment='创建者用户ID')

    owner = relationship('User', foreign_keys=[owner_id], back_populates='task_labels')
    project = relationship('Project', back_populates='task_labels')
    creator = relationship('User', foreign_keys=[created_by_user_id])

    def to_dict(self):
        result = super().to_dict()
        result['is_builtin'] = bool(self.is_builtin)
        result['is_active'] = bool(self.is_active)
        return result
