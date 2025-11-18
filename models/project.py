from enum import Enum
from .base import Base
from sqlalchemy import Column, Integer, String, Text

class ProjectStatus(Enum):
    ACTIVE = 'active'
    ARCHIVED = 'archived'

class Project(Base):
    __tablename__ = 'projects'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    status = Column(String(20), default='active')
