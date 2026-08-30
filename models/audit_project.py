"""
审计日志与项目角色（统一版）

历史上本文件还定义了 ProjectMember（RBAC 版）与 ProjectRole。2026-08-31
合并收敛后，project_members 表的唯一映射位于 models/project_member.py，
ProjectRole 为其角色枚举的兼容别名，此处仅保留 AuditLog。
"""

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    JSON,
    String,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db

# 兼容再导出：api/agents 包历史使用 ProjectRole（owner/admin/maintainer/member/viewer）
from .project_member import ProjectMemberRole as ProjectRole  # noqa: F401
from .project_member import ProjectMember as _CanonicalProjectMember  # noqa: F401


class AuditLog(BaseModel):
    """Immutable audit trail for significant platform operations.

    Captures who did what, to which resource, with what result. Designed for
    compliance, debugging, and analytics — not for real-time UI.
    """

    __tablename__ = "audit_logs"

    actor_type = Column(String(20), nullable=False, index=True, comment="human / agent / system")
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="User who performed the action")
    actor_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Agent that performed the action")
    action = Column(String(100), nullable=False, index=True, comment="Action identifier (e.g. agent.created, workflow.launched)")
    resource_type = Column(String(50), nullable=False, index=True, comment="Target resource type (agent, task, workflow, etc.)")
    resource_id = Column(BigInteger, nullable=False, index=True, comment="Target resource ID")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True, comment="Project context")
    detail = Column(JSON, comment="Arbitrary action details (before/after diff, params, etc.)")
    ip_address = Column(String(45), comment="Client IP (for human actors)")

    actor_user = relationship("User")
    actor_agent = relationship("Agent")
    project = relationship("Project")

    def to_dict(self):
        result = super().to_dict()
        result["detail"] = self.detail or {}
        if self.actor_agent:
            result["actor_agent_name"] = self.actor_agent.name
        if self.actor_user:
            result["actor_user_email"] = self.actor_user.email
        return result

    @classmethod
    def record(cls, action, resource_type, resource_id, actor_type="system",
               actor_user_id=None, actor_agent_id=None, project_id=None,
               detail=None, ip_address=None):
        """Create and add an audit log entry (not yet committed)."""
        entry = cls(
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            project_id=project_id,
            detail=detail or {},
            ip_address=ip_address,
        )
        db.session.add(entry)
        return entry
