"""
Agent 记忆模型（多维度作用域 + 租户硬隔离）

一行记忆 = 某个作用域（organization/project/agent/user/session）下的一条
可检索事实。隔离规则：
- 每行强制 organization_id（硬租户边界，任何查询都必须带它）；
- user 作用域的「个人记忆」也是 per-org 的（用户在组织 A 存的记忆不会
  泄漏到组织 B）——自托管多租户平台的安全默认；
- 跨作用域读取只允许通过 scopes.py 的继承链（会话→项目→Agent→User→组织）。
"""

import enum

from sqlalchemy import Column, String, Integer, Text, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import relationship

from .base import BaseModel


class MemoryScopeType(enum.Enum):
    """记忆作用域维度（专用于会话，暂映射 GoalLoop 运行）"""
    ORGANIZATION = 'organization'   # 组织级：全组织共享的制度/惯例
    PROJECT = 'project'             # 项目级：项目的持久教训/结论
    AGENT = 'agent'                 # Agent 级：该 Agent 的个人经验记忆
    USER = 'user'                   # User 级：用户偏好（per-org 隔离）
    SESSION = 'session'             # 会话级：一次循环运行内的临时记忆


# 召回优先级：越具体的维度越先被采信
SCOPE_PRECEDENCE = (
    MemoryScopeType.SESSION,
    MemoryScopeType.PROJECT,
    MemoryScopeType.AGENT,
    MemoryScopeType.USER,
    MemoryScopeType.ORGANIZATION,
)

SCOPE_LABELS = {
    MemoryScopeType.SESSION: '会话记忆',
    MemoryScopeType.PROJECT: '项目记忆',
    MemoryScopeType.AGENT: 'Agent记忆',
    MemoryScopeType.USER: '用户记忆',
    MemoryScopeType.ORGANIZATION: '组织记忆',
}


class AgentMemory(BaseModel):
    """多维度作用域记忆"""

    __tablename__ = 'agent_memories'

    # ── 隔离维度 ──
    organization_id = Column(
        Integer, ForeignKey('organizations.id'), nullable=False, index=True,
        comment='硬租户边界：任何读写都必须带组织 ID',
    )
    scope_type = Column(String(32), nullable=False, index=True,
                        comment='作用域：organization/project/agent/user/session')
    scope_id = Column(Integer, nullable=False, index=True,
                      comment='作用域实体 ID（按 scope_type 解释）')

    # ── 内容 ──
    kind = Column(String(32), default='insight', index=True,
                  comment='记忆类型：insight/pattern/solution/rule/preference/summary')
    title = Column(String(500), nullable=False, comment='简短标题')
    content = Column(Text, nullable=False, comment='记忆正文')

    # ── 来源与生命周期 ──
    source_type = Column(String(50), default='manual',
                         comment='来源：manual/loop_done/loop_blocked/loop_review/human')
    source_task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, index=True,
                      comment='作者 Agent（用户/系统写入时可为空）')
    confidence = Column(Integer, default=70,
                        comment='置信度 0-100（经验类默认 70，人工确认 90+）')
    is_valid = Column(Integer, nullable=False, default=1, index=True,
                      comment='1=有效，0=已遗忘（软删除）')
    access_count = Column(Integer, nullable=False, default=0, comment='被召回次数')
    last_accessed_at = Column(DateTime, comment='最近一次被召回时间')
    expires_at = Column(DateTime, nullable=True,
                        comment='过期时间（会话级记忆随循环结束可过期）')

    # ── 幂等去重（同作用域内同 dedupe_key 只存一条） ──
    dedupe_key = Column(String(64), nullable=False, comment='规范化内容的哈希')

    __table_args__ = (
        Index('uq_agent_memories_scope_dedupe',
              'organization_id', 'scope_type', 'scope_id', 'dedupe_key',
              unique=True),
        Index('ix_agent_memories_scope_lookup',
              'organization_id', 'scope_type', 'scope_id', 'is_valid'),
    )

    agent = relationship('Agent', foreign_keys=[agent_id])

    @property
    def scope_label(self) -> str:
        try:
            return SCOPE_LABELS[MemoryScopeType(self.scope_type)]
        except ValueError:
            return self.scope_type

    def to_dict(self, include_content=True):
        result = super().to_dict()
        result['scope_label'] = self.scope_label
        if not include_content:
            result.pop('content', None)
        return result
