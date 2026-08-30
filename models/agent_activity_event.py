"""
Unified agent activity event model.
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String
from .base import BaseModel


class AgentActivityEvent(BaseModel):
    __tablename__ = 'agent_activity_events'

    workspace_id = Column(Integer, nullable=False, index=True, comment='Workspace ID')
    agent_id = Column(Integer, index=True, comment='Primary related agent ID')

    source = Column(String(32), nullable=False, default='agent_audit', index=True, comment='Event source')
    event_type = Column(String(64), nullable=False, index=True, comment='Event type')
    level = Column(String(16), nullable=False, default='info', index=True, comment='Event level')

    message = Column(String(512), comment='Short event summary')
    payload = Column(JSON, comment='Event payload')

    occurred_at = Column(DateTime, nullable=False, index=True, comment='Event occurrence time')

    task_id = Column(BigInteger, index=True, comment='Related task ID')
    project_id = Column(Integer, index=True, comment='Related project ID')
    run_id = Column(String(64), index=True, comment='Related run ID')
    attempt_id = Column(String(64), index=True, comment='Related attempt ID')
    correlation_id = Column(String(64), index=True, comment='Correlation ID')
    request_id = Column(String(64), index=True, comment='Request ID')

    actor_type = Column(String(32), comment='Actor type')
    actor_id = Column(String(64), comment='Actor ID')
    target_type = Column(String(32), comment='Target type')
    target_id = Column(String(64), comment='Target ID')

