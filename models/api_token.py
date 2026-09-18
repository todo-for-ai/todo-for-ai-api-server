from datetime import datetime
from .base import BaseModel, db
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.orm import relationship

class ApiToken(BaseModel):
    __tablename__ = 'api_tokens'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    token_hash = Column(String(64), nullable=False)
    token_encrypted = Column(Text, nullable=True)
    prefix = Column(String(10), nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    usage_count = Column(Integer, default=0)
    created_by = Column(String(100), nullable=True)

    # Relationship to user
    user = relationship('User', foreign_keys=[user_id], primaryjoin='ApiToken.user_id == User.id')

    @classmethod
    def verify_token(cls, token_string):
        """验证API Token是否有效"""
        if not token_string:
            return None

        # For now, use token_hash column for lookup
        # In a real implementation, you'd hash the input and compare
        api_token = cls.query.filter_by(token_hash=token_string, is_active=True).first()
        if not api_token:
            return None

        # Check if token has expired
        if api_token.expires_at and api_token.expires_at < datetime.utcnow():
            return None

        return api_token
