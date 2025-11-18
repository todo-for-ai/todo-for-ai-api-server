from .base import BaseModel
from sqlalchemy import Column, Integer, String, DateTime

class Attachment(BaseModel):
    __tablename__ = 'attachments'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, nullable=False)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
