"""
预算与配额模型（P2.6）

按 Agent / 项目 / 工作区设置资源上限，任务派发与 AgentRun 创建路径强制校验：
- resources:
    tokens            - Token 用量（仅 workspace 级统计有效，数据源 AIRequestLog）
    duration_minutes  - Agent 累计执行时长（数据源 agent_runs）
    concurrent        - 并发执行数（即时值：active leases）
- periods: total（不限起点）/ daily / weekly / monthly（周期滚动窗口）
超限动作：写入 interaction_request 审批事件（budget_exceeded）+ 审计，
同一预算在同一周期内只告警一次（幂等）。
"""

from sqlalchemy import Column, String, Integer, BigInteger, ForeignKey, Boolean, UniqueConstraint
from .base import BaseModel


class Budget(BaseModel):
    """资源预算/配额"""

    __tablename__ = 'budgets'
    __table_args__ = (
        UniqueConstraint(
            'scope_type', 'agent_id', 'project_id', 'workspace_id', 'resource', 'period',
            name='uq_budget_scope_resource_period',
        ),
    )

    SCOPE_TYPES = ('agent', 'project', 'workspace')
    RESOURCES = ('tokens', 'duration_minutes', 'concurrent')
    PERIODS = ('total', 'daily', 'weekly', 'monthly')

    scope_type = Column(String(20), nullable=False, comment='范围类型: agent/project/workspace')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, index=True, comment='Agent ID（scope=agent）')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, index=True, comment='项目ID（scope=project）')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    resource = Column(String(30), nullable=False, comment='资源类型: tokens/duration_minutes/concurrent')
    limit_value = Column(BigInteger, nullable=False, comment='上限值')
    period = Column(String(20), nullable=False, default='total', comment='周期: total/daily/weekly/monthly')
    is_active = Column(Boolean, nullable=False, default=True, comment='是否启用')

    def __repr__(self):
        return f'<Budget {self.id}: {self.scope_type}/{self.resource}/{self.period} limit={self.limit_value}>'

    def to_dict(self):
        result = super().to_dict()
        return result
