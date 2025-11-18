from .base import BaseModel
from sqlalchemy import Column, Integer, String, Text

class UserSettings(BaseModel):
    __tablename__ = 'user_settings'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(Text)
