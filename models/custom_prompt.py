from enum import Enum
from .base import Base
from sqlalchemy import Column, Integer, String, Text

class PromptType(Enum):
    TASK = 'task'
    PROJECT = 'project'

class CustomPrompt(Base):
    __tablename__ = 'custom_prompts'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    prompt_type = Column(String(20), nullable=False)
