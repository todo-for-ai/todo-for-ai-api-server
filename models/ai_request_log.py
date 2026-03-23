"""
AI 请求日志模型
用于审计和监控 AI 服务使用情况
"""

from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, Boolean, Text, DateTime, Index
from .base import BaseModel


class AIRequestLog(BaseModel):
    """AI 请求日志"""
    __tablename__ = 'ai_request_logs'

    request_id = Column(String(64), nullable=False, index=True, comment='请求唯一ID')
    user_id = Column(Integer, nullable=False, index=True, comment='用户ID')
    user_email = Column(String(255), comment='用户邮箱')
    feature = Column(String(64), nullable=False, index=True, comment='功能标识')

    # Token 使用量
    prompt_tokens = Column(Integer, default=0, comment='Prompt token 数')
    completion_tokens = Column(Integer, default=0, comment='Completion token 数')
    total_tokens = Column(Integer, default=0, comment='总 token 数')

    # 性能指标
    latency_ms = Column(Float, comment='响应延迟(毫秒)')
    cache_hit = Column(Boolean, default=False, comment='是否命中缓存')

    # 错误信息
    error_code = Column(Integer, default=0, comment='错误码')
    error_message = Column(Text, comment='错误信息')

    # 创建索引
    __table_args__ = (
        Index('idx_ai_logs_user_feature', 'user_id', 'feature'),
        Index('idx_ai_logs_created_at', 'created_at'),
        Index('idx_ai_logs_error', 'error_code'),
    )

    def __repr__(self):
        return f'<AIRequestLog {self.request_id}: {self.feature}>'

    @classmethod
    def get_stats_by_feature(cls, start_date: datetime = None, end_date: datetime = None):
        """按功能统计使用量"""
        from sqlalchemy import func

        query = cls.query.with_entities(
            cls.feature,
            func.count(cls.id).label('request_count'),
            func.sum(cls.total_tokens).label('total_tokens'),
            func.avg(cls.latency_ms).label('avg_latency'),
            func.sum(cls.cache_hit.cast(Integer)).label('cache_hits')
        )

        if start_date:
            query = query.filter(cls.created_at >= start_date)
        if end_date:
            query = query.filter(cls.created_at <= end_date)

        return query.group_by(cls.feature).all()

    @classmethod
    def get_stats_by_user(cls, user_id: int, days: int = 30):
        """获取用户使用统计"""
        from sqlalchemy import func
        from datetime import timedelta

        start_date = datetime.utcnow() - timedelta(days=days)

        return cls.query.with_entities(
            func.count(cls.id).label('request_count'),
            func.sum(cls.total_tokens).label('total_tokens'),
            func.avg(cls.latency_ms).label('avg_latency')
        ).filter(
            cls.user_id == user_id,
            cls.created_at >= start_date
        ).first()
