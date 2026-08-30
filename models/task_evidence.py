"""
任务验证证据模型（DoD Evidence）

任务的机器可验证完成标准（Definition of Done）的执行证据。
Agent 提交任务结果时附带证据，平台据此判断任务是否真正完成。
"""

from sqlalchemy import Column, String, Integer, ForeignKey, BigInteger, DateTime, JSON
from .base import BaseModel


class TaskEvidenceRecord(BaseModel):
    """任务验证证据（列存字符串，便于跨库迁移）"""

    TYPES = ('test', 'build', 'lint', 'command', 'pr', 'manual')
    STATUSES = ('passed', 'failed', 'unknown')
    """任务验证证据"""

    __tablename__ = 'task_evidences'

    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True, comment='任务ID')
    attempt_id = Column(String(64), nullable=True, index=True, comment='关联的 AgentTaskAttempt ID')
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True, index=True, comment='产出证据的 Agent ID')

    evidence_type = Column(String(20), nullable=False, comment='证据类型: test/build/lint/command/pr/manual')
    status = Column(String(20), nullable=False, default='unknown', comment='证据结论: passed/failed/unknown')
    summary = Column(String(500), comment='一句话结论，如 "24 passed, 1 failed"')
    detail = Column(JSON, comment='结构化明细（命令、输出摘要、覆盖率等）')
    url = Column(String(1000), comment='证据外链（CI 日志、PR 等）')
    verified_at = Column(DateTime, comment='证据产生/核验时间')

    def __repr__(self):
        return f'<TaskEvidenceRecord {self.id}: task={self.task_id} type={self.evidence_type} status={self.status}>'
