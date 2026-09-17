"""LLM 调用指标模型 —— agent-runtime 每次引擎真实调用的一条记录。

数据由 daemon 经 ``POST /agent/llm-metrics/batch`` 摄取（幂等键 call_id），
用户级/组织级聚合查询见 services/llm_metrics.py 与 api/llm_metrics.py。
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, BigInteger, Float, Index, ForeignKey
from sqlalchemy.orm import relationship

from .base import BaseModel, db


class LlmCallMetric(BaseModel):
    """单次 LLM 引擎调用指标"""

    __tablename__ = 'llm_call_metrics'
    # 三个查询热路径：组织水位（workspace_id, created_at）、
    # 用户个人视图（owner_user_id, created_at）、Agent 详情（agent_id, created_at）
    __table_args__ = (
        Index('idx_llm_call_metrics_ws_created', 'workspace_id', 'created_at'),
        Index('idx_llm_call_metrics_owner_created', 'owner_user_id', 'created_at'),
        Index('idx_llm_call_metrics_agent_created', 'agent_id', 'created_at'),
    )

    call_id = Column(String(36), nullable=False, unique=True, comment='调用唯一ID(daemon生成,幂等摄取)')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True, comment='工作区ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, index=True, comment='Agent ID')
    owner_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True,
                           comment='归属用户(agent.owner_id,缺省creator_user_id)')
    task_id = Column(BigInteger, nullable=True, comment='任务ID')
    attempt_id = Column(String(64), nullable=True, comment='Attempt ID')

    engine = Column(String(32), nullable=False, default='', comment='执行引擎: claude/codex/opencode/custom/openclaw')
    model = Column(String(128), nullable=False, default='', comment='实际使用的模型')
    base_url = Column(String(255), nullable=False, default='', comment='LLM API 端点')
    status = Column(String(16), nullable=False, default='success', comment='状态: success/failed/timeout')
    duration_ms = Column(Integer, nullable=False, default=0, comment='调用耗时(毫秒)')

    input_tokens = Column(Integer, comment='输入tokens')
    output_tokens = Column(Integer, comment='输出tokens')
    total_tokens = Column(Integer, comment='总tokens')
    cache_read_tokens = Column(Integer, comment='缓存读取tokens')
    cost_usd = Column(Float, comment='成本(美元,引擎上报则填)')

    error_code = Column(String(64), comment='错误码')
    error_message = Column(String(512), comment='错误摘要')

    agent = relationship('Agent')
    workspace = relationship('Organization')
    owner = relationship('User')

    @classmethod
    def record_call(cls, agent, data: dict):
        """按 daemon 上报数据建一条记录（归属用户在服务端解析，不信任客户端）"""
        owner_user_id = getattr(agent, 'owner_id', None) or getattr(agent, 'creator_user_id', None)
        return cls(
            call_id=data['call_id'],
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            owner_user_id=owner_user_id,
            task_id=data.get('task_id'),
            attempt_id=data.get('attempt_id'),
            engine=data.get('engine', ''),
            model=data.get('model', ''),
            base_url=data.get('base_url', ''),
            status=data.get('status', 'success'),
            duration_ms=data.get('duration_ms') or 0,
            input_tokens=data.get('input_tokens'),
            output_tokens=data.get('output_tokens'),
            total_tokens=data.get('total_tokens'),
            cache_read_tokens=data.get('cache_read_tokens'),
            cost_usd=data.get('cost_usd'),
            error_code=data.get('error_code'),
            error_message=data.get('error_message'),
            created_by=f"agent:{agent.id}",
        )

    def to_dict(self, exclude=None):
        data = super().to_dict(exclude=exclude)
        if isinstance(self.created_at, datetime):
            data['created_at'] = self.created_at.isoformat()
        return data
