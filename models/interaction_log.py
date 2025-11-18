from enum import Enum
from .base import BaseModel
from sqlalchemy import Column, Integer, String, Text, DateTime

class InteractionType(Enum):
    MCP = 'mcp'
    API = 'api'

class InteractionStatus(Enum):
    SUCCESS = 'success'
    ERROR = 'error'

class InteractionLog(BaseModel):
    __tablename__ = 'interaction_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    interaction_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)
    details = Column(Text)
