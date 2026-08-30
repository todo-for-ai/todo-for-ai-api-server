"""
Agent communication channels, collaboration templates, and knowledge entries.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


class AgentChannel(BaseModel):
    """A collaboration channel where multiple Agents can discuss and coordinate.

    Channels can be tied to a specific task (task-scoped) or be standalone
    (project-scoped). Agents join channels and exchange messages in real-time.
    """

    __tablename__ = "agent_channels"

    name = Column(String(255), nullable=False, comment="Channel display name")
    description = Column(Text, comment="Channel description/purpose")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True, comment="Project scope (null = global)")
    task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, index=True, comment="Task scope (null = project/global)")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Channel creator")
    is_active = Column(Boolean, nullable=False, default=True, comment="Whether the channel is active")

    project = relationship("Project")
    task = relationship("Task")
    owner = relationship("User")
    members = relationship("AgentChannelMember", back_populates="channel", cascade="all, delete-orphan")
    messages = relationship("AgentChannelMessage", back_populates="channel", cascade="all, delete-orphan")

    def to_dict(self, include_members=False, include_last_message=False):
        result = super().to_dict()
        if include_members:
            result["members"] = [m.to_dict() for m in self.members]
        if include_last_message:
            last = AgentChannelMessage.query.filter_by(channel_id=self.id).order_by(AgentChannelMessage.id.desc()).first()
            result["last_message"] = last.to_dict() if last else None
        return result


class AgentChannelMember(BaseModel):
    """An Agent's membership in a channel."""

    __tablename__ = "agent_channel_members"

    channel_id = Column(Integer, ForeignKey("agent_channels.id"), nullable=False, index=True, comment="Channel ID")
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent ID")
    role = Column(String(20), default="member", comment="Channel role: owner, member")

    channel = relationship("AgentChannel", back_populates="members")
    agent = relationship("Agent")

    __table_args__ = (
        # Unique agent per channel
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        if self.agent:
            result["agent_name"] = self.agent.name
            result["agent_kind"] = self.agent.kind.value if self.agent.kind else None
        return result


class AgentChannelMessage(BaseModel):
    """A message in an Agent collaboration channel."""

    __tablename__ = "agent_channel_messages"

    channel_id = Column(Integer, ForeignKey("agent_channels.id"), nullable=False, index=True, comment="Channel ID")
    sender_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True, comment="Sender Agent ID (null if human)")
    sender_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True, comment="Sender User ID (null if agent)")
    content = Column(Text, nullable=False, comment="Message content")
    message_type = Column(String(50), default="text", comment="Message type: text, system, action")
    # NOTE: 'metadata' is a reserved attribute name in SQLAlchemy 2.0 declarative
    # (shadows the Mapper MetaData). Map a non-reserved Python attribute
    # 'extra_metadata' to the same DB column 'metadata' to preserve the schema.
    extra_metadata = Column("metadata", JSON, comment="Extra structured metadata")

    channel = relationship("AgentChannel", back_populates="messages")
    sender_agent = relationship("Agent")
    sender_user = relationship("User")

    def to_dict(self):
        result = super().to_dict()
        # super() keyed by DB column name 'metadata' which resolves to the reserved
        # Mapper MetaData; replace with the actual JSON value under the API key.
        result["metadata"] = self.extra_metadata
        if self.sender_agent:
            result["sender_name"] = self.sender_agent.name
            result["sender_type"] = "agent"
        elif self.sender_user:
            result["sender_name"] = self.sender_user.name or self.sender_user.email
            result["sender_type"] = "human"
        else:
            result["sender_name"] = "system"
            result["sender_type"] = "system"
        return result


class CollaborationTemplate(BaseModel):
    """A reusable pattern for assembling a team of Agents.

    Defines the agent roles (kind, capabilities, collaboration_role) and
    optionally a workflow to execute. Users can instantiate a template to
    quickly set up a working multi-Agent team.
    """

    __tablename__ = "collaboration_templates"

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, comment="Template creator")
    name = Column(String(255), nullable=False, comment="Template display name")
    description = Column(Text, comment="Template description")
    category = Column(String(100), comment="Category tag (e.g. devops, research, review)")
    agent_specs = Column(JSON, nullable=False, comment="List of agent spec dicts: {name, kind, capabilities, collaboration_role, provider, model}")
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=True, comment="Optional workflow to attach")
    is_builtin = Column(Boolean, default=False, comment="Whether this is a built-in template")

    owner = relationship("User")
    workflow = relationship("Workflow")

    def to_dict(self):
        result = super().to_dict()
        result["agent_specs"] = self.agent_specs or []
        return result


class KnowledgeEntry(BaseModel):
    """A piece of knowledge learned or stored by an Agent.

    Agents can persist insights, patterns, solutions, or any structured
    information that can be reused across tasks. Entries are keyed by
    domain/tag and support full-text search.
    """

    __tablename__ = "knowledge_entries"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Owning Agent")
    title = Column(String(500), nullable=False, comment="Short descriptive title")
    content = Column(Text, nullable=False, comment="Knowledge content (can be markdown, JSON, etc.)")
    domain = Column(String(100), comment="Knowledge domain (e.g. 'python', 'frontend', 'devops')")
    tags = Column(JSON, default=list, comment="List of tags for categorization")
    entry_type = Column(String(50), default="insight", comment="Type: insight, pattern, solution, reference, rule")
    source_task_id = Column(BigInteger, ForeignKey("tasks.id"), nullable=True, comment="Task that generated this knowledge")
    source_type = Column(String(50), default="manual", comment="Source: manual, auto_extracted, imported, shared")
    confidence = Column(Float, default=1.0, comment="Confidence score 0.0-1.0")
    access_count = Column(Integer, default=0, comment="How many times this entry has been accessed")
    is_valid = Column(Boolean, default=True, comment="Whether this entry is still considered valid")
    shared_with_project = Column(Boolean, default=False, comment="Whether shared with all project members")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Project scope if shared")

    agent = relationship("Agent", backref="knowledge_entries")
    source_task = relationship("Task")
    project = relationship("Project")

    def to_dict(self, include_content=True):
        result = super().to_dict()
        if not include_content:
            result.pop("content", None)
        result["tags"] = self.tags or []
        return result

