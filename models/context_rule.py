from enum import Enum
from .base import Base
from sqlalchemy import Column, Integer, String, Text, Boolean

class RuleType(Enum):
    SYSTEM = 'system'
    USER = 'user'

class ContextRule(Base):
    __tablename__ = 'context_rules'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    rule_type = Column(String(20), default='user')
    is_active = Column(Boolean, default=True)
