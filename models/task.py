from enum import Enum
from .base import BaseModel
from sqlalchemy import Column, Integer, String, Text, DateTime

class TaskStatus(Enum):
    TODO = 'todo'
    IN_PROGRESS = 'in_progress'
    REVIEW = 'review'
    DONE = 'done'
    CANCELLED = 'cancelled'

class TaskPriority(Enum):
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'
    URGENT = 'urgent'

class Task(BaseModel):
    __tablename__ = 'tasks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    content = Column(Text)
    status = Column(String(20), default='todo')
    priority = Column(String(20), default='medium')
    project_id = Column(Integer, nullable=False)
