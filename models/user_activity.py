from .base import BaseModel
from sqlalchemy import Column, Integer, String, DateTime, func, and_
from datetime import datetime, timedelta

class UserActivity(BaseModel):
    __tablename__ = 'user_activities'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    activity_type = Column(String(50), nullable=False)
    description = Column(String(255))

    @classmethod
    def get_user_activity_stats(cls, user_id, days=30):
        """获取用户活跃度统计"""
        from .base import db
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            count = db.session.query(func.count(cls.id)).filter(
                cls.user_id == user_id,
                cls.created_at >= start_date
            ).scalar()
            return {
                'total_activities': count or 0,
                'days': days
            }
        except Exception:
            return {'total_activities': 0, 'days': days}

    @classmethod
    def get_user_activity_heatmap(cls, user_id, days=365):
        """获取用户活跃度热力图数据"""
        from .base import db
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            results = db.session.query(
                func.date(cls.created_at).label('date'),
                func.count(cls.id).label('count')
            ).filter(
                cls.user_id == user_id,
                cls.created_at >= start_date
            ).group_by(func.date(cls.created_at)).all()

            return [{'date': str(r.date), 'count': r.count} for r in results]
        except Exception:
            return []
