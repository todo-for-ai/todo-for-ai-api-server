from .base import BaseModel
from sqlalchemy import Column, Integer, String, DateTime, Boolean

class ApiToken(BaseModel):
    __tablename__ = 'api_tokens'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    token = Column(String(255), unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
