"""
Agent experience, reputation, and cross-project authorization models.
"""

import enum
from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
    or_,
)
from sqlalchemy.orm import relationship

from .base import BaseModel, db


class AgentExperience(BaseModel):
    """An experience record capturing what an Agent learned from executing a task.

    Experiences are automatically extracted from task outcomes (success/failure
    patterns) and can be shared across agents for collective learning. Each
    experience captures the task context, the strategy used, and the outcome
    pattern so that similar future tasks can benefit.
    """

    __tablename__ = "agent_experiences"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent that had this experience")
    experience_type = Column(String(50), nullable=False, default="success_pattern",
                             comment="Type: success_pattern, failure_pattern, strategy, optimization, anti_pattern")
    domain = Column(String(100), comment="Knowledge domain (e.g. 'python', 'frontend', 'devops')")
    task_type = Column(String(100), comment="Category of task (e.g. 'code_review', 'bug_fix', 'deployment')")
    capabilities_used = Column(JSON, default=list, comment="Capabilities that were relevant to this task")
    strategy = Column(Text, comment="Strategy or approach used (what was done)")
    outcome_pattern = Column(Text, comment="What happened — success factors or failure reasons")
    key_learnings = Column(Text, comment="Concise takeaways for future similar tasks")
    confidence = Column(Float, default=0.7, comment="Confidence in this experience (0.0-1.0)")
    applicability_score = Column(Float, default=0.5, comment="How broadly applicable this experience is (0.0-1.0)")
    source_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, comment="Task that generated this experience")
    source_step_key = Column(String(100), comment="Workflow step key that generated this experience")
    source_workflow_run_id = Column(Integer, comment="Workflow run that generated this experience")
    is_shared = Column(Boolean, default=False, comment="Whether this experience is shared with other agents")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, comment="Project scope")
    times_reused = Column(Integer, default=0, comment="How many times this experience was recommended/reused")
    last_reused_at = Column(DateTime, comment="Last time this experience was referenced")
    is_valid = Column(Boolean, default=True, comment="Whether this experience is still considered valid")

    agent = relationship("Agent", backref="experiences")
    source_task = relationship("Task")
    project = relationship("Project")

    def to_dict(self):
        result = super().to_dict()
        result["capabilities_used"] = self.capabilities_used or []
        return result

    @classmethod
    def extract_from_step_outcome(cls, agent_id, step_run, step_def, task=None):
        """Auto-extract an experience from a completed workflow step.

        Creates a structured experience record based on the step outcome,
        capturing what worked or what went wrong.
        """
        success = step_run.status == StepStatus.SUCCEEDED
        experience_type = "success_pattern" if success else "failure_pattern"

        # Determine domain from step capabilities
        capabilities_used = step_def.required_capabilities if step_def else []
        domain = capabilities_used[0] if capabilities_used else None

        # Build strategy description
        strategy_parts = []
        if step_def and step_def.name:
            strategy_parts.append(f"Step: {step_def.name}")
        if step_def and step_def.on_failure:
            strategy_parts.append(f"Failure strategy: {step_def.on_failure}")
        if step_run.agent_id:
            strategy_parts.append(f"Executed by agent #{step_run.agent_id}")
        strategy = "; ".join(strategy_parts) if strategy_parts else None

        # Build outcome pattern
        if success:
            outcome_parts = ["Task completed successfully."]
            if step_run.result_summary:
                outcome_parts.append(f"Result: {step_run.result_summary[:500]}")
            if step_run.started_at and step_run.finished_at:
                duration = (step_run.finished_at - step_run.started_at).total_seconds()
                outcome_parts.append(f"Duration: {duration:.1f}s")
            outcome_pattern = " ".join(outcome_parts)
        else:
            outcome_parts = ["Task failed."]
            if step_run.error:
                outcome_parts.append(f"Error: {step_run.error[:500]}")
            outcome_pattern = " ".join(outcome_parts)

        # Build key learnings
        if success:
            learnings = []
            if capabilities_used:
                learnings.append(f"Capabilities {', '.join(capabilities_used)} were effective for this task type.")
            if step_def and step_def.on_failure == "continue":
                learnings.append("Continue-on-failure strategy allowed workflow to progress.")
            key_learnings = " ".join(learnings) if learnings else "Approach was successful; repeat for similar tasks."
        else:
            learnings = []
            if step_run.error:
                learnings.append(f"Avoid: {step_run.error[:200]}")
            if capabilities_used:
                learnings.append(f"Capabilities {', '.join(capabilities_used)} may be insufficient alone.")
            key_learnings = " ".join(learnings) if learnings else "This approach failed; consider alternative strategy."

        # Determine confidence based on consistency
        confidence = 0.7 if success else 0.6
        if step_run.attempt and step_run.attempt > 1:
            confidence *= 0.8  # Lower confidence for retried steps

        experience = cls.create(
            agent_id=agent_id,
            experience_type=experience_type,
            domain=domain,
            task_type=step_def.name if step_def else None,
            capabilities_used=capabilities_used,
            strategy=strategy,
            outcome_pattern=outcome_pattern,
            key_learnings=key_learnings,
            confidence=round(confidence, 2),
            applicability_score=0.5,
            source_task_id=task.id if task else None,
            source_step_key=step_run.step_key,
            source_workflow_run_id=step_run.run_id,
            is_shared=False,
        )
        db.session.flush()
        return experience

    @classmethod
    def find_relevant_experiences(cls, agent_id, domain=None, task_type=None,
                                  capabilities=None, experience_type=None,
                                  include_shared=True, limit=10):
        """Find experiences relevant to a given task context.

        Searches the agent's own experiences and optionally shared experiences
        from other agents in the same domain/capability space.
        """
        query = cls.query.filter(cls.is_valid == True)

        if include_shared:
            query = query.filter(
                (cls.agent_id == agent_id) | (cls.is_shared == True)
            )
        else:
            query = query.filter(cls.agent_id == agent_id)

        if domain:
            query = query.filter(cls.domain == domain)
        if task_type:
            query = query.filter(cls.task_type == task_type)
        if experience_type:
            query = query.filter(cls.experience_type == experience_type)

        # Filter by capabilities overlap (JSON contains)
        if capabilities:
            # Use a simple approach: filter experiences where any capability matches
            for cap in capabilities[:3]:  # Limit to avoid overly complex queries
                query = query.filter(cls.capabilities_used.contains([cap]))

        # Order by confidence and reuse count
        query = query.order_by(cls.confidence.desc(), cls.times_reused.desc())
        return query.limit(limit).all()

    @classmethod
    def apply_decay(cls, agent_id=None, days_threshold=30, decay_rate=0.02):
        """Apply time-based confidence decay to experiences.

        Experiences that haven't been reused recently lose confidence over time,
        simulating the natural obsolescence of knowledge. Experiences that are
        frequently reused maintain or increase their confidence.

        Args:
            agent_id: Optional agent to scope the decay to (None = all agents)
            days_threshold: Only decay experiences older than this many days
            decay_rate: Confidence reduction per decay cycle (0.0-1.0)

        Returns:
            Number of experiences that were decayed.
        """
        cutoff = datetime.utcnow() - timedelta(days=days_threshold)
        query = cls.query.filter(
            cls.is_valid == True,
            cls.confidence > 0.1,  # Don't decay below 0.1
            cls.updated_at < cutoff,
        )
        if agent_id:
            query = query.filter_by(agent_id=agent_id)

        experiences = query.all()
        decayed_count = 0
        for exp in experiences:
            # Calculate days since last use or update
            reference_time = exp.last_reused_at or exp.updated_at or exp.created_at
            if not reference_time:
                continue
            days_idle = (datetime.utcnow() - reference_time).days

            if days_idle > days_threshold:
                # Apply decay: more idle = more decay
                decay_factor = 1 - (decay_rate * (days_idle / days_threshold))
                new_confidence = max(0.1, exp.confidence * decay_factor)

                # Reuse boosts: if reused frequently, decay less
                if exp.times_reused and exp.times_reused > 3:
                    reuse_boost = min(0.2, exp.times_reused * 0.02)
                    new_confidence = min(1.0, new_confidence + reuse_boost)

                if new_confidence != exp.confidence:
                    exp.confidence = round(new_confidence, 3)
                    decayed_count += 1

                    # Mark as invalid if confidence drops too low
                    if exp.confidence <= 0.1:
                        exp.is_valid = False

        if decayed_count > 0:
            db.session.flush()
        return decayed_count

    @classmethod
    def cross_validate(cls, experience_id, validator_agent_id, is_accurate: bool):
        """Cross-validate an experience by another agent.

        When an agent validates or refutes a shared experience, it affects
        the experience's confidence. Multiple validations converge the
        confidence toward the community consensus.
        """
        exp = cls.query.filter_by(id=experience_id, is_valid=True).first()
        if not exp:
            return None

        # Don't validate own experiences (already accounted for)
        if exp.agent_id == validator_agent_id:
            return exp

        if is_accurate:
            # Validation: increase confidence, cap at 1.0
            boost = 0.05
            if exp.is_shared:
                boost = 0.08  # Shared experiences get more boost from validation
            exp.confidence = min(1.0, round(exp.confidence + boost, 3))
        else:
            # Refutation: decrease confidence significantly
            penalty = 0.15
            exp.confidence = max(0.0, round(exp.confidence - penalty, 3))
            if exp.confidence <= 0.2:
                exp.is_valid = False  # Low confidence after refutation -> mark invalid

        db.session.flush()
        return exp

    @classmethod
    def get_validation_stats(cls, agent_id):
        """Get validation statistics for an agent's experiences."""
        total = cls.query.filter_by(agent_id=agent_id, is_valid=True).count()
        shared = cls.query.filter_by(agent_id=agent_id, is_shared=True, is_valid=True).count()
        high_conf = cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_valid == True,
            cls.confidence >= 0.8,
        ).count()
        low_conf = cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_valid == True,
            cls.confidence < 0.5,
        ).count()
        avg_conf = db.session.query(
            db.func.avg(cls.confidence)
        ).filter(
            cls.agent_id == agent_id,
            cls.is_valid == True,
        ).scalar() or 0

        return {
            "total_experiences": total,
            "shared_experiences": shared,
            "high_confidence": high_conf,
            "low_confidence": low_conf,
            "average_confidence": round(float(avg_conf), 3),
        }

    @classmethod
    def share_experience(cls, experience_id, agent_id):
        """Share an experience with other agents (mark as shared)."""
        exp = cls.query.filter_by(id=experience_id, agent_id=agent_id).first()
        if not exp:
            return None
        exp.is_shared = True
        db.session.flush()
        return exp

    @classmethod
    def learn_from_shared(cls, target_agent_id, experience_id):
        """An agent internalizes a shared experience from another agent.

        Creates a copy of the shared experience adapted for the learning agent,
        with reduced confidence (since it's second-hand knowledge).
        """
        source = cls.query.filter_by(id=experience_id, is_shared=True, is_valid=True).first()
        if not source:
            return None
        # Don't learn from own experiences
        if source.agent_id == target_agent_id:
            return source

        # Check if already learned
        existing = cls.query.filter_by(
            agent_id=target_agent_id,
            source_task_id=source.source_task_id,
            experience_type=source.experience_type,
            domain=source.domain,
        ).first()
        if existing:
            return existing

        # Create adapted copy with reduced confidence
        learned = cls.create(
            agent_id=target_agent_id,
            experience_type=source.experience_type,
            domain=source.domain,
            task_type=source.task_type,
            capabilities_used=source.capabilities_used or [],
            strategy=source.strategy,
            outcome_pattern=f"[Learned from agent #{source.agent_id}] {source.outcome_pattern}",
            key_learnings=source.key_learnings,
            confidence=round(source.confidence * 0.8, 2),  # Reduced confidence for learned experiences
            applicability_score=source.applicability_score,
            source_task_id=source.source_task_id,
            source_step_key=source.source_step_key,
            is_shared=False,
        )
        db.session.flush()
        return learned


class AgentReputation(BaseModel):
    """Tracks an Agent's reputation score based on task performance.

    Reputation is updated after each task completion/failure and affects
    task assignment priority. Higher reputation = higher priority.
    """

    __tablename__ = "agent_reputations"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, unique=True, index=True, comment="Agent ID")
    score = Column(Float, nullable=False, default=50.0, comment="Reputation score (0-100, starts at 50)")
    total_tasks = Column(Integer, nullable=False, default=0, comment="Total tasks assigned")
    completed_tasks = Column(Integer, nullable=False, default=0, comment="Tasks completed successfully")
    failed_tasks = Column(Integer, nullable=False, default=0, comment="Tasks failed")
    avg_completion_time = Column(Float, comment="Average completion time in seconds")
    on_time_rate = Column(Float, default=1.0, comment="Ratio of tasks completed before deadline")
    quality_score = Column(Float, default=50.0, comment="Quality score based on review feedback (0-100)")
    last_updated_at = Column(DateTime, comment="Last time reputation was recalculated")

    agent = relationship("Agent", backref="reputation")

    def to_dict(self):
        result = super().to_dict()
        result["success_rate"] = (self.completed_tasks / self.total_tasks * 100) if self.total_tasks > 0 else 0
        return result

    @classmethod
    def get_or_create(cls, agent_id):
        """Get existing reputation record or create one with defaults."""
        rep = cls.query.filter_by(agent_id=agent_id).first()
        if not rep:
            rep = cls.create(
                agent_id=agent_id,
                score=50.0,
                total_tasks=0,
                completed_tasks=0,
                failed_tasks=0,
            )
            db.session.flush()
        return rep

    @classmethod
    def record_outcome(cls, agent_id, success: bool, completion_time=None, on_time=True, quality_delta=0, context=None):
        """Record a task outcome and update the reputation score.

        Scoring:
        - Success: +2 base, +bonus for on_time, +bonus for fast completion
        - Failure: -5 base
        - Quality feedback: +/- quality_delta

        ``context`` is an optional dict merged into the audit ``detail`` so a
        reputation change point can be traced back to the originating task /
        workflow step (e.g. ``{"task_id":.., "step_key":.., "workflow_run_id":..}``).
        Only forward-only fields; do not put unserializable objects here.
        """
        rep = cls.get_or_create(agent_id)
        rep.total_tasks += 1
        rep.last_updated_at = datetime.utcnow()

        if success:
            rep.completed_tasks += 1
            score_delta = 2.0
            if on_time:
                score_delta += 1.0
                rep.on_time_rate = (rep.on_time_rate * (rep.completed_tasks - 1) + 1.0) / rep.completed_tasks
            else:
                rep.on_time_rate = (rep.on_time_rate * (rep.completed_tasks - 1) + 0.0) / rep.completed_tasks
            if completion_time and rep.avg_completion_time:
                if completion_time < rep.avg_completion_time * 0.8:
                    score_delta += 1.0  # Fast completion bonus
                rep.avg_completion_time = (rep.avg_completion_time * (rep.completed_tasks - 1) + completion_time) / rep.completed_tasks
            elif completion_time:
                rep.avg_completion_time = completion_time
        else:
            rep.failed_tasks += 1
            score_delta = -5.0

        rep.quality_score = max(0, min(100, rep.quality_score + quality_delta))
        rep.score = max(0, min(100, rep.score + score_delta))
        db.session.flush()
        # Audit reputation-impacting outcomes (failures and quality deltas) so the
        # unified security event feed can surface them. Success-only updates are
        # too frequent and low-signal to audit individually.
        if not success or quality_delta != 0:
            try:
                detail = {"success": success, "score_delta": score_delta,
                          "quality_delta": quality_delta, "new_score": rep.score,
                          "total_tasks": rep.total_tasks}
                if context:
                    detail.update(context)
                AuditLog.record(
                    action="reputation.update", resource_type="agent", resource_id=agent_id,
                    actor_type="system", actor_agent_id=agent_id,
                    detail=detail,
                )
            except Exception:
                pass  # Never fail the reputation update on an audit error
        return rep


class CrossProjectAgent(BaseModel):
    """Authorizes an Agent to participate in a project it was not originally created in.

    This enables cross-project collaboration: an Agent owned by one user can be
    invited to work on tasks in another project, subject to the project owner's
    approval. The Agent retains its original owner but gains access to the
    target project's tasks and workflows.
    """

    __tablename__ = "cross_project_agents"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True, comment="Agent being granted cross-project access")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True, comment="Target project the agent can access")
    authorized_by = Column(Integer, ForeignKey("users.id"), nullable=True, comment="User who authorized this cross-project access")
    role_in_project = Column(String(50), default="contributor", comment="Role in target project: contributor, reviewer, observer")
    capabilities_override = Column(JSON, comment="Override capabilities for this project context (null = use agent's own)")
    max_concurrent_tasks = Column(Integer, default=3, comment="Max concurrent tasks this agent can handle in this project")
    is_active = Column(Boolean, default=True, comment="Whether this cross-project authorization is currently active")
    expires_at = Column(DateTime, comment="Optional expiry time for this authorization")

    agent = relationship("Agent", backref="cross_project_access")
    project = relationship("Project", backref="external_agents")
    authorizer = relationship("User", foreign_keys=[authorized_by])

    __table_args__ = (
        # One authorization per agent per project
        {"sqlite_autoincrement": True},
    )

    def to_dict(self):
        result = super().to_dict()
        result["capabilities_override"] = self.capabilities_override or []
        if self.agent:
            result["agent_name"] = self.agent.name
            result["agent_kind"] = self.agent.kind.value if self.agent.kind else None
            result["agent_capabilities"] = self.agent.capabilities or []
        if self.project:
            result["project_name"] = self.project.name
        return result

    @classmethod
    def get_active_for_agent(cls, agent_id):
        """Get all active cross-project authorizations for an agent."""
        now = datetime.utcnow()
        return cls.query.filter(
            cls.agent_id == agent_id,
            cls.is_active == True,
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        ).all()

    @classmethod
    def get_active_for_project(cls, project_id):
        """Get all active cross-project agents for a project."""
        now = datetime.utcnow()
        return cls.query.filter(
            cls.project_id == project_id,
            cls.is_active == True,
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        ).all()

    @classmethod
    def is_authorized(cls, agent_id, project_id):
        """Check if an agent is authorized for a project."""
        now = datetime.utcnow()
        return cls.query.filter(
            cls.agent_id == agent_id,
            cls.project_id == project_id,
            cls.is_active == True,
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        ).first() is not None

    @classmethod
    def get_effective_capabilities(cls, agent_id, project_id):
        """Get the effective capabilities for an agent in a project context.

        If the agent has a capabilities_override for this project, use that;
        otherwise fall back to the agent's own capabilities.
        """
        auth = cls.query.filter(
            cls.agent_id == agent_id,
            cls.project_id == project_id,
            cls.is_active == True,
        ).first()
        if auth and auth.capabilities_override:
            return auth.capabilities_override
        agent = Agent.query.get(agent_id)
        return agent.capabilities if agent else []

