"""
Agent 审计事件模型
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String
from .base import BaseModel


class AgentAuditEvent(BaseModel):
    __tablename__ = 'agent_audit_events'

    workspace_id = Column(Integer, nullable=False, index=True, comment='工作区ID')
    event_type = Column(String(64), nullable=False, index=True, comment='事件类型')

    actor_type = Column(String(32), nullable=False, comment='行为主体类型')
    actor_id = Column(String(64), nullable=False, comment='行为主体ID')
    target_type = Column(String(32), nullable=False, comment='目标类型')
    target_id = Column(String(64), nullable=False, comment='目标ID')

    source = Column(String(32), nullable=False, default='api', index=True, comment='事件来源')
    level = Column(String(16), nullable=False, default='info', index=True, comment='事件级别')
    risk_score = Column(Integer, nullable=False, default=0, comment='风险分')
    correlation_id = Column(String(64), index=True, comment='链路关联ID')
    request_id = Column(String(64), index=True, comment='请求ID')
    run_id = Column(String(64), index=True, comment='关联运行ID')
    attempt_id = Column(String(64), index=True, comment='关联尝试ID')
    task_id = Column(BigInteger, index=True, comment='关联任务ID')
    project_id = Column(Integer, index=True, comment='关联项目ID')
    actor_agent_id = Column(Integer, index=True, comment='行为主体Agent ID')
    target_agent_id = Column(Integer, index=True, comment='目标Agent ID')
    duration_ms = Column(Integer, comment='耗时毫秒')
    error_code = Column(String(64), comment='错误码')
    payload = Column(JSON, comment='附加信息')
    ip = Column(String(64), comment='请求IP')
    user_agent = Column(String(512), comment='UA')
    occurred_at = Column(DateTime, nullable=False, index=True, comment='事件发生时间')
