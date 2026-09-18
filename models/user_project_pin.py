from .base import BaseModel
from . import db
from sqlalchemy import Column, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

class UserProjectPin(BaseModel):
    __tablename__ = 'user_project_pins'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    pin_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship to Project
    project = relationship("Project", lazy='joined')

    def to_dict(self):
        """Convert to dictionary"""
        result = {
            'id': self.id,
            'user_id': self.user_id,
            'project_id': self.project_id,
            'is_active': self.is_active,
            'pin_order': self.pin_order,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        # Include project info if available
        if self.project:
            result['project'] = {
                'id': self.project.id,
                'name': self.project.name,
                'color': getattr(self.project, 'color', None),
                'status': getattr(self.project, 'status', None),
            }
        return result

    @classmethod
    def get_user_pin_count(cls, user_id):
        """Get the number of active pins for a user"""
        return cls.query.filter_by(user_id=user_id, is_active=True).count()

    @classmethod
    def is_project_pinned(cls, user_id, project_id):
        """Check if a project is pinned by a user"""
        return cls.query.filter_by(
            user_id=user_id,
            project_id=project_id,
            is_active=True
        ).first() is not None

    @classmethod
    def pin_project(cls, user_id, project_id, pin_order=None):
        """Pin a project for a user"""
        # Check if already pinned
        existing = cls.query.filter_by(user_id=user_id, project_id=project_id).first()
        if existing:
            # Reactivate if inactive
            if not existing.is_active:
                existing.is_active = True
                if pin_order is not None:
                    existing.pin_order = pin_order
                existing.updated_at = datetime.utcnow()
                return existing
            return existing

        # Create new pin
        if pin_order is None:
            # Get next available order
            max_order = db.session.query(db.func.max(cls.pin_order)).filter_by(
                user_id=user_id, is_active=True
            ).scalar() or 0
            pin_order = max_order + 1

        pin = cls(
            user_id=user_id,
            project_id=project_id,
            is_active=True,
            pin_order=pin_order
        )
        return pin

    @classmethod
    def unpin_project(cls, user_id, project_id):
        """Unpin a project for a user"""
        pin = cls.query.filter_by(
            user_id=user_id,
            project_id=project_id,
            is_active=True
        ).first()
        if pin:
            pin.is_active = False
            pin.updated_at = datetime.utcnow()
        return pin

    @classmethod
    def reorder_pins(cls, user_id, pin_orders):
        """Reorder pins for a user
        pin_orders: list of dicts with 'project_id' and 'pin_order'
        """
        for item in pin_orders:
            project_id = item.get('project_id')
            new_order = item.get('pin_order')
            if project_id is not None and new_order is not None:
                pin = cls.query.filter_by(
                    user_id=user_id,
                    project_id=project_id,
                    is_active=True
                ).first()
                if pin:
                    pin.pin_order = new_order
                    pin.updated_at = datetime.utcnow()
