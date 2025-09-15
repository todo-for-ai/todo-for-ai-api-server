"""
交互日志模型
"""

import enum
from datetime import datetime
from sqlalchemy import Column, String, Text, Enum, BigInteger, ForeignKey, DateTime, JSON
from sqlalchemy.orm import relationship
from .base import BaseModel


class InteractionType(enum.Enum):
    """交互类型枚举"""
    AI_FEEDBACK = 'ai_feedback'
    HUMAN_RESPONSE = 'human_response'


class InteractionStatus(enum.Enum):
    """交互状态枚举"""
    PENDING = 'pending'
    COMPLETED = 'completed'
    CONTINUED = 'continued'


class InteractionLog(BaseModel):
    """交互日志模型"""

    __tablename__ = 'interaction_logs'

    # 重写id字段为BigInteger
    id = Column(BigInteger, primary_key=True, autoincrement=True, comment='主键ID')

    # 关联信息
    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, comment='关联任务ID')
    session_id = Column(String(100), nullable=False, comment='交互会话ID')

    # 交互内容
    interaction_type = Column(
        Enum(InteractionType), 
        nullable=False,
        comment='交互类型'
    )
    content = Column(Text, nullable=False, comment='交互内容')
    status = Column(
        Enum(InteractionStatus), 
        default=InteractionStatus.PENDING, 
        nullable=False,
        comment='交互状态'
    )

    # 元数据
    metadata = Column(JSON, comment='额外元数据')

    # 关系
    task = relationship('Task', back_populates='interaction_logs')

    def __repr__(self):
        return f'<InteractionLog {self.id}: {self.interaction_type.value} for Task {self.task_id}>'

    def to_dict(self, exclude=None):
        """转换为字典格式"""
        result = super().to_dict(exclude)
        
        # 处理枚举类型
        if 'interaction_type' in result and result['interaction_type']:
            result['interaction_type'] = result['interaction_type'].value if hasattr(result['interaction_type'], 'value') else result['interaction_type']
        
        if 'status' in result and result['status']:
            result['status'] = result['status'].value if hasattr(result['status'], 'value') else result['status']
        
        return result

    @classmethod
    def create_ai_feedback(cls, task_id, session_id, content, metadata=None):
        """创建AI反馈记录"""
        return cls.create(
            task_id=task_id,
            session_id=session_id,
            interaction_type=InteractionType.AI_FEEDBACK,
            content=content,
            status=InteractionStatus.PENDING,
            metadata=metadata or {},
            created_by='AI'
        )

    @classmethod
    def create_human_response(cls, task_id, session_id, content, status=InteractionStatus.COMPLETED, metadata=None, created_by='human'):
        """创建人工响应记录"""
        return cls.create(
            task_id=task_id,
            session_id=session_id,
            interaction_type=InteractionType.HUMAN_RESPONSE,
            content=content,
            status=status,
            metadata=metadata or {},
            created_by=created_by
        )

    @classmethod
    def get_session_history(cls, session_id):
        """获取会话历史"""
        return cls.query.filter_by(session_id=session_id).order_by(cls.created_at.asc()).all()

    @classmethod
    def get_task_interactions(cls, task_id):
        """获取任务的所有交互记录"""
        return cls.query.filter_by(task_id=task_id).order_by(cls.created_at.asc()).all()
