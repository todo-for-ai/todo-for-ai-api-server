from .base import BaseModel
from sqlalchemy import Column, Integer, DateTime

class UserProjectPin(BaseModel):
    __tablename__ = 'user_project_pins'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    project_id = Column(Integer, nullable=False)
