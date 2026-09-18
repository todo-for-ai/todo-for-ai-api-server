from .base import BaseModel
from . import db
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime

class UserSettings(BaseModel):
    __tablename__ = 'user_settings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, unique=True)
    language = Column(String(10), default='en')
    settings_data = Column(Text)  # JSON string for additional settings
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        """Convert to dictionary"""
        import json
        settings_dict = {}
        if self.settings_data:
            try:
                settings_dict = json.loads(self.settings_data)
            except:
                pass
        return {
            'id': self.id,
            'user_id': self.user_id,
            'language': self.language,
            'settings_data': settings_dict,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def save(self):
        """Save to database"""
        db.session.add(self)
        db.session.commit()

    @classmethod
    def get_or_create_for_user(cls, user_id, default_language='en'):
        """Get or create user settings"""
        settings = cls.query.filter_by(user_id=user_id).first()
        if not settings:
            settings = cls(
                user_id=user_id,
                language=default_language
            )
            db.session.add(settings)
            db.session.commit()
        return settings
