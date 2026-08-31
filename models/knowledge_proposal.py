"""项目知识提案模型（P3.2 项目知识库自动策展）

自动策展的「提案 → 人工确认」流：
- 来源（source_type）：失败归因 / PR 评审 / 人类纠偏
- 提案（proposed）由自动策展器写入；人类 confirm 后沉淀为项目共享
  KnowledgeEntry（知识库），dismiss 则归档不入库
- dedupe_key 保证同一来源只提一次案（幂等）
"""

from sqlalchemy import Column, String, Integer, Text, ForeignKey, DateTime, JSON, UniqueConstraint
from .base import BaseModel


class ProjectKnowledgeProposal(BaseModel):
    """待确认的项目知识提案"""

    __tablename__ = 'project_knowledge_proposals'
    __table_args__ = (
        UniqueConstraint('project_id', 'dedupe_key', name='uq_knowledge_proposal_project_dedupe'),
    )

    STATUS_PROPOSED = 'proposed'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_DISMISSED = 'dismissed'
    STATUSES = (STATUS_PROPOSED, STATUS_CONFIRMED, STATUS_DISMISSED)

    SOURCE_FAILURE_ATTRIBUTION = 'failure_attribution'
    SOURCE_PR_REVIEW = 'pr_review'
    SOURCE_HUMAN_CORRECTION = 'human_correction'
    SOURCE_TYPES = (SOURCE_FAILURE_ATTRIBUTION, SOURCE_PR_REVIEW, SOURCE_HUMAN_CORRECTION)

    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, index=True, comment='项目ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True, comment='工作区ID')
    proposal_type = Column(String(50), nullable=False, default='failure_lesson',
                           comment='提案类型: failure_lesson/decision/convention/review_insight')
    title = Column(String(500), nullable=False, comment='提案标题')
    content = Column(Text, nullable=False, comment='提案内容（Markdown）')
    source_type = Column(String(50), nullable=False, default=SOURCE_FAILURE_ATTRIBUTION, comment='策展来源')
    source_ref = Column(JSON, nullable=True, comment='来源引用（task/pr/类别等）')
    status = Column(String(20), nullable=False, default=STATUS_PROPOSED, index=True, comment='状态: proposed/confirmed/dismissed')
    dedupe_key = Column(String(128), nullable=True, index=True, comment='幂等键（同来源只提一次）')
    proposed_by_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, comment='触发策展的 Agent')
    decided_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, comment='裁决用户')
    decided_at = Column(DateTime, nullable=True, comment='裁决时间')
    knowledge_entry_id = Column(Integer, ForeignKey('knowledge_entries.id'), nullable=True, comment='确认后生成的知识条目')
    dismissal_reason = Column(String(500), nullable=True, comment='驳回原因')

    def to_dict(self):
        return super().to_dict()
