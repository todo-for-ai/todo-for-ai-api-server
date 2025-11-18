from enum import Enum
from .base import BaseModel
from sqlalchemy import Column, Integer, String, Text, DateTime

class ActionType(Enum):
    CREATE = 'create'
    UPDATE = 'update'
    DELETE = 'delete'

class TaskHistory(BaseModel):
    __tablename__ = 'task_history'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, nullable=False)
    action_type = Column(String(20), nullable=False)
    description = Column(Text)
