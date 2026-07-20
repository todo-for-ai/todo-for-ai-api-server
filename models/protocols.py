"""
Collaboration protocol models: CollaborationProtocol, ProtocolMessage and enums.
"""

import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


class ProtocolType(enum.Enum):
    """Types of structured collaboration protocols between Agents."""
    PROPOSAL = "proposal"       # One agent proposes, others vote
    VOTE = "vote"              # Simple majority vote
    CONSENSUS = "consensus"     # All must agree
    AUCTION = "auction"         # Competitive bidding for task assignment
    HANDOFF = "handoff"         # Structured task handoff with context
    DELIBERATION = "deliberation"  # Multi-round deliberation before final vote
    RANKED_VOTE = "ranked_vote"    # Ranked-choice voting (instant runoff)
    WEIGHTED_VOTE = "weighted_vote"  # Vote weighted by agent reputation


class ProtocolStatus(enum.Enum):
    """Status of a collaboration protocol instance."""
    OPEN = "open"
    VOTING = "voting"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class CollaborationProtocol(BaseModel):
    """A structured collaboration protocol instance between Agents.

    Supports proposal/vote/consensus/auction/handoff patterns for
    structured multi-Agent decision making.
    """

    __tablename__ = "collaboration_protocols"

    protocol_type = Column(String(30), nullable=False, comment="Protocol type: proposal, vote, consensus, auction, handoff")
    status = Column(String(20), nullable=False, default="open", comment="Current status")
    title = Column(String(500), nullable=False, comment="Protocol title / proposal subject")
    description = Column(Text, comment="Detailed description of the proposal/protocol")
    initiator_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, comment="Agent who initiated")
    channel_id = Column(Integer, ForeignKey("agent_channels.id"), nullable=True, comment="Associated channel")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Project scope")
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, comment="Related task")
    # Protocol config
    config = Column(JSON, comment="Protocol-specific config (e.g. quorum, timeout, auction rules)")
    # Results
    result = Column(JSON, comment="Protocol result (e.g. vote counts, winning bid, consensus outcome)")
    deadline = Column(DateTime, comment="Optional deadline for voting/response")
    resolved_at = Column(DateTime, comment="When the protocol was resolved")

    initiator = relationship("Agent", foreign_keys=[initiator_agent_id])
    channel = relationship("AgentChannel")
    project = relationship("Project")
    task = relationship("Task")
    messages = relationship("ProtocolMessage", back_populates="protocol", cascade="all, delete-orphan")

    def to_dict(self, include_messages=False):
        result = super().to_dict()
        result["protocol_type"] = self.protocol_type
        result["status"] = self.status
        result["config"] = self.config or {}
        result["result"] = self.result or {}
        if include_messages:
            result["messages"] = [m.to_dict() for m in self.messages]
        return result


class ProtocolMessage(BaseModel):
    """A message within a collaboration protocol (vote, bid, response, etc.)."""

    __tablename__ = "protocol_messages"

    protocol_id = Column(Integer, ForeignKey("collaboration_protocols.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, comment="Agent who sent this message")
    message_type = Column(String(30), nullable=False, comment="Type: vote, bid, accept, reject, comment, counter_proposal")
    content = Column(Text, comment="Message content")
    payload = Column(JSON, comment="Structured data (e.g. vote choice, bid amount, counter-proposal)")

    protocol = relationship("CollaborationProtocol", back_populates="messages")
    agent = relationship("Agent")

    def to_dict(self):
        result = super().to_dict()
        result["payload"] = self.payload or {}
        return result

