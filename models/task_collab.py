"""
Task collaboration models: TaskTemplate, TaskEvent, Notification, SharedContext.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


class TaskTemplate(BaseModel):
    """Reusable task template — users can create new tasks from a template."""

    __tablename__ = "task_templates"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="User who owns this template")
    name = Column(String(255), nullable=False, comment="Template name")
    description = Column(Text, nullable=False, default="", comment="Template description")
    title_template = Column(String(500), nullable=False, default="", comment="Default task title (may contain placeholders)")
    content_template = Column(Text, nullable=False, default="", comment="Default task content/description")
    priority = Column(String(20), nullable=False, default="medium", comment="Default priority")
    tags = Column(JSON, comment="Default tags")
    is_ai_task = Column(Boolean, nullable=False, default=False, comment="Default is_ai_task flag")
    capabilities = Column(JSON, comment="Required agent capabilities for this template")

    owner = relationship("User")

    def to_dict(self):
        result = super().to_dict()
        result["tags"] = self.tags or []
        result["capabilities"] = self.capabilities or []
        return result


class TaskEvent(BaseModel):
    """Append-only collaboration event for a task."""

    __tablename__ = "task_events"

    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=False, index=True, comment="Task ID")
    actor_type = Column(String(20), nullable=False, index=True, comment="human/agent/system")
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="Actor user ID")
    actor_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Actor Agent ID")
    event_type = Column(String(100), nullable=False, index=True, comment="Event type")
    payload = Column(JSON, comment="Event payload")

    task = relationship("Task")
    actor_user = relationship("User")
    actor_agent = relationship("Agent")

    def to_dict(self):
        result = super().to_dict()
        result["payload"] = self.payload or {}
        if self.actor_agent:
            result["actor_agent"] = {
                "id": self.actor_agent.id,
                "name": self.actor_agent.name,
                "kind": self.actor_agent.kind.value if self.actor_agent.kind else None,
                "status": self.actor_agent.status.value if self.actor_agent.status else None,
            }
        if self.actor_user:
            result["actor_user"] = {
                "id": self.actor_user.id,
                "name": self.actor_user.name or self.actor_user.username or self.actor_user.email,
                "email": self.actor_user.email,
            }
        return result

    @classmethod
    def record(cls, task_id, event_type, actor_type="system", actor_user_id=None, actor_agent_id=None, payload=None):
        event = cls(
            task_id=task_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            payload=payload or {},
        )
        return event


class Notification(BaseModel):
    """Persistent notification for a user — survives across sessions."""

    __tablename__ = "notifications"

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Owning user")
    event_type = Column(String(100), nullable=False, index=True, comment="Notification category")
    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Related task")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Related agent")
    payload = Column(JSON, comment="Arbitrary payload")
    is_read = Column(Boolean, nullable=False, default=False, index=True, comment="Read flag")
    read_at = Column(DateTime, nullable=True, comment="When the notification was read")

    user = relationship("User")
    task = relationship("Task")
    agent = relationship("Agent")

    def to_dict(self):
        result = super().to_dict()
        result["payload"] = self.payload or {}
        if self.agent:
            result["agent_name"] = self.agent.name
        if self.task:
            result["task_title"] = self.task.title
        return result

    @classmethod
    def create_notification(cls, user_id, event_type, task_id=None, agent_id=None, payload=None):
        """Create and return a new Notification (not yet committed)."""
        n = cls(
            user_id=user_id,
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            payload=payload or {},
        )
        db.session.add(n)
        return n


class SharedContext(BaseModel):
    """Key-value context entries shared across Agents working on the same task.

    Agents use this to persist intermediate results, references, scratch notes,
    or any structured data that other Agents (or a later run of the same Agent)
    need to read — without flooding the event timeline.
    """

    __tablename__ = "shared_context"

    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=False, index=True, comment="Task ID")
    key = Column(String(255), nullable=False, index=True, comment="Context key (e.g. 'research_summary', 'code_plan')")
    value = Column(Text, nullable=False, default="", comment="Context value (Markdown or JSON string)")
    author_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Agent that wrote this entry")
    author_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="User that wrote this entry")

    task = relationship("Task")
    author_agent = relationship("Agent")
    author_user = relationship("User")

    __table_args__ = (
        # Unique key per task — upsert semantics
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        if self.author_agent:
            result["author_agent_name"] = self.author_agent.name
        if self.author_user:
            result["author_user_name"] = self.author_user.name or self.author_user.email
        return result

