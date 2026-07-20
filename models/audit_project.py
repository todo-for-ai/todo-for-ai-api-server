"""
Audit log and project membership models.
"""

import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


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


class ProjectRole(enum.Enum):
    """Project-level role for RBAC."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ProjectMember(BaseModel):
    """Project membership with role-based access control.

    Controls who can view / edit / manage tasks and agents within a project.
    The project owner is always a member with the OWNER role (created automatically).
    """

    __tablename__ = "project_members"

    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True, comment="Project ID")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="User ID")
    role = Column(Enum(ProjectRole), default=ProjectRole.MEMBER, nullable=False, index=True, comment="Role within the project")
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True, comment="User who sent the invitation")
    accepted_at = Column(DateTime, comment="When the invitee accepted the invitation")

    project = relationship("Project")
    user = relationship("User", foreign_keys=[user_id])
    inviter = relationship("User", foreign_keys=[invited_by])

    __table_args__ = (
        # One membership per user per project
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        result["role"] = self.role.value if self.role else None
        if self.user:
            result["user_email"] = self.user.email
            result["user_name"] = self.user.name or self.user.username or self.user.email
        return result

    @classmethod
    def get_role(cls, project_id, user_id):
        """Return the user's role in the project, or None if not a member."""
        m = cls.query.filter_by(project_id=project_id, user_id=user_id).first()
        return m.role if m else None

    @classmethod
    def can(cls, project_id, user_id, action):
        """Check if a user can perform an action in a project.

        Action hierarchy:
          - view: VIEWER+
          - edit: MEMBER+
          - manage: ADMIN+
          - admin: OWNER only
        """
        role = cls.get_role(project_id, user_id)
        if role is None:
            return False
        if action == "view":
            return role in (ProjectRole.OWNER, ProjectRole.ADMIN, ProjectRole.MEMBER, ProjectRole.VIEWER)
        if action == "edit":
            return role in (ProjectRole.OWNER, ProjectRole.ADMIN, ProjectRole.MEMBER)
        if action == "manage":
            return role in (ProjectRole.OWNER, ProjectRole.ADMIN)
        if action == "admin":
            return role == ProjectRole.OWNER
        return False

