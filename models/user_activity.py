from .base import BaseModel
from sqlalchemy import Column, Integer, String, DateTime

class UserActivity(BaseModel):
    __tablename__ = 'user_activities'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    activity_type = Column(String(50), nullable=False)
    description = Column(String(255))
