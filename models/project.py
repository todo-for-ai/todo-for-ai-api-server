"""
项目模型
"""

import enum
from sqlalchemy import Column, String, Text, Enum, DateTime, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel, db


class ProjectStatus(enum.Enum):
    """项目状态枚举"""
    ACTIVE = 'active'
    ARCHIVED = 'archived'
    DELETED = 'deleted'


class Project(BaseModel):
    """项目模型"""
    
    __tablename__ = 'projects'

    # 用户关联
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='项目所有者ID')
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True, comment='所属组织ID')

    # 基本信息
    name = Column(String(255), nullable=False, comment='项目名称')
    description = Column(Text, comment='项目描述')
    color = Column(String(7), default='#1890ff', comment='项目颜色 (HEX)')
    status = Column(
        Enum(ProjectStatus),
        default=ProjectStatus.ACTIVE,
        nullable=False,
        comment='项目状态'
    )

    # 扩展信息
    github_url = Column(String(500), comment='GitHub仓库链接')
    local_url = Column(String(500), comment='本地开发链接')
    production_url = Column(String(500), comment='生产环境链接')
    project_context = Column(Text, comment='项目级别的上下文信息')
    last_activity_at = Column(DateTime, comment='最后活动时间')
    
    # 关系
    owner = relationship('User', back_populates='projects')
    organization = relationship('Organization', back_populates='projects')
    members = relationship(
        'ProjectMember',
        back_populates='project',
        cascade='all, delete-orphan',
        lazy='dynamic'
    )
    task_labels = relationship(
        'TaskLabel',
        back_populates='project',
        lazy='dynamic'
    )
    tasks = relationship(
        'Task',
        back_populates='project',
        cascade='all, delete-orphan',
        lazy='dynamic'
    )
    context_rules = relationship(
        'ContextRule',
        back_populates='project',
        cascade='all, delete-orphan',
        lazy='dynamic'
    )
    
    def __repr__(self):
        return f'<Project {self.id}: {self.name}>'
    
    def to_dict(self, include_stats=False):
        """转换为字典，可选包含统计信息"""
        result = super().to_dict()
        result['status'] = self.status.value if self.status else None
        
        if include_stats:
            from sqlalchemy import func, case
            from .task import TaskStatus
            from .task import Task
            from .context_rule import ContextRule

            task_stats_row = db.session.query(
                func.count(Task.id).label('total_tasks'),
                func.sum(case((Task.status == TaskStatus.TODO, 1), else_=0)).label('todo_tasks'),
                func.sum(case((Task.status == TaskStatus.IN_PROGRESS, 1), else_=0)).label('in_progress_tasks'),
                func.sum(case((Task.status == TaskStatus.DONE, 1), else_=0)).label('done_tasks')
            ).filter(
                Task.project_id == self.id
            ).first()

            context_rules_count = db.session.query(
                func.count(ContextRule.id)
            ).filter(
                ContextRule.project_id == self.id,
                ContextRule.is_active.is_(True)
            ).scalar() or 0

            result['stats'] = {
                'total_tasks': int((task_stats_row.total_tasks if task_stats_row else 0) or 0),
                'todo_tasks': int((task_stats_row.todo_tasks if task_stats_row else 0) or 0),
                'in_progress_tasks': int((task_stats_row.in_progress_tasks if task_stats_row else 0) or 0),
                'done_tasks': int((task_stats_row.done_tasks if task_stats_row else 0) or 0),
                'context_rules_count': int(context_rules_count),
            }
        
        return result
    
    @classmethod
    def get_active_projects(cls):
        """获取所有活跃项目"""
        return cls.query.filter_by(status=ProjectStatus.ACTIVE).all()
    
    @classmethod
    def search_projects(cls, keyword, status=None):
        """搜索项目"""
        query = cls.query
        
        if status:
            query = query.filter_by(status=status)
        
        if keyword:
            query = query.filter(
                cls.name.contains(keyword) | 
                cls.description.contains(keyword)
            )
        
        return query.all()
    
    def get_active_context_rules(self):
        """获取项目的活跃上下文规则"""
        return self.context_rules.filter_by(is_active=True).order_by('priority DESC').all()
    
    def archive(self):
        """归档项目"""
        self.status = ProjectStatus.ARCHIVED
        self.save()
    
    def restore(self):
        """恢复项目"""
        self.status = ProjectStatus.ACTIVE
        self.save()
    
    def soft_delete(self):
        """软删除项目"""
        self.status = ProjectStatus.DELETED
        self.save()

    @property
    def is_active(self):
        """检查项目是否活跃"""
        return self.status == ProjectStatus.ACTIVE
