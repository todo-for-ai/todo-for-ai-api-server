"""
Agent experience CRUD and action endpoints.

Analytics, decay, and validation routes are in experience_analytics.py.
Cross-project authorization routes are in cross_project.py.
"""

from datetime import datetime, timedelta

from flask import request

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentExperience,
    AgentReputation,
    Task,
    TaskStatus,
    AuditLog,
    get_request_args,
    paginate_query,
    validate_json_request,
    notify_sse,
    _client_ip,
    flush_sse_notifications,
    WorkflowStepRun,
    WorkflowRun,
    WorkflowStep,
    StepStatus,
)

@agents_bp.route("/<int:agent_id>/experiences", methods=["GET"])
@unified_auth_required
def list_agent_experiences(agent_id):
    """List experiences for an agent, with optional filters."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    query = AgentExperience.query.filter_by(agent_id=agent_id, is_valid=True)

    # Optional filters
    experience_type = args.get("experience_type")
    if experience_type:
        query = query.filter_by(experience_type=experience_type)
    domain = args.get("domain")
    if domain:
        query = query.filter_by(domain=domain)
    task_type = args.get("task_type")
    if task_type:
        query = query.filter_by(task_type=task_type)
    is_shared = args.get("is_shared")
    if is_shared is not None:
        query = query.filter_by(is_shared=is_shared.lower() == "true")

    query = query.order_by(AgentExperience.confidence.desc(), AgentExperience.created_at.desc())
    return paginate_query(query, "experiences")


@agents_bp.route("/<int:agent_id>/experiences", methods=["POST"])
@unified_auth_required
def create_agent_experience(agent_id):
    """Manually create an experience record for an agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request()
    required = ["experience_type", "strategy"]
    for f in required:
        if not data.get(f):
            return ApiResponse.error(f"Missing required field: {f}").to_response()

    exp = AgentExperience.create(
        agent_id=agent_id,
        experience_type=data["experience_type"],
        domain=data.get("domain"),
        task_type=data.get("task_type"),
        capabilities_used=data.get("capabilities_used", []),
        strategy=data["strategy"],
        outcome_pattern=data.get("outcome_pattern"),
        key_learnings=data.get("key_learnings"),
        confidence=data.get("confidence", 0.7),
        applicability_score=data.get("applicability_score", 0.5),
        source_task_id=data.get("source_task_id"),
        is_shared=data.get("is_shared", False),
        project_id=data.get("project_id"),
    )
    db.session.commit()

    notify_sse("agent_experience_created", {
        "agent_id": agent_id,
        "experience_id": exp.id,
        "experience_type": exp.experience_type,
    })
    return ApiResponse.success(exp.to_dict(), "Experience created").to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>", methods=["GET"])
@unified_auth_required
def get_agent_experience(agent_id, experience_id):
    """Get a specific experience record."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.query.filter_by(id=experience_id, agent_id=agent_id).first()
    if not exp:
        return ApiResponse.not_found("Experience not found").to_response()

    # Increment access count
    exp.access_count = (exp.access_count or 0) + 1
    db.session.commit()

    return ApiResponse.success(exp.to_dict()).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>", methods=["PUT"])
@unified_auth_required
def update_agent_experience(agent_id, experience_id):
    """Update an experience record."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.query.filter_by(id=experience_id, agent_id=agent_id).first()
    if not exp:
        return ApiResponse.not_found("Experience not found").to_response()

    data = validate_json_request()
    updatable = ["experience_type", "domain", "task_type", "capabilities_used",
                 "strategy", "outcome_pattern", "key_learnings", "confidence",
                 "applicability_score", "is_shared", "is_valid", "project_id"]
    for field in updatable:
        if field in data:
            setattr(exp, field, data[field])

    db.session.commit()
    return ApiResponse.success(exp.to_dict(), "Experience updated").to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>", methods=["DELETE"])
@unified_auth_required
def delete_agent_experience(agent_id, experience_id):
    """Soft-delete an experience (mark as invalid)."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.query.filter_by(id=experience_id, agent_id=agent_id).first()
    if not exp:
        return ApiResponse.not_found("Experience not found").to_response()

    exp.is_valid = False
    db.session.commit()
    return ApiResponse.success(None, "Experience deleted").to_response()


@agents_bp.route("/<int:agent_id>/experiences/recommend", methods=["GET"])
@unified_auth_required
def recommend_experiences(agent_id):
    """Recommend relevant experiences for an upcoming task context.

    Query params: domain, task_type, capabilities (comma-separated)
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    domain = args.get("domain")
    task_type = args.get("task_type")
    capabilities = args.get("capabilities", "").split(",") if args.get("capabilities") else None

    experiences = AgentExperience.find_relevant_experiences(
        agent_id=agent_id,
        domain=domain,
        task_type=task_type,
        capabilities=capabilities,
        include_shared=True,
        limit=10,
    )

    # Increment reuse count for recommended experiences
    now = datetime.utcnow()
    for exp in experiences:
        exp.times_reused = (exp.times_reused or 0) + 1
        exp.last_reused_at = now

    db.session.commit()

    return ApiResponse.success(
        [e.to_dict() for e in experiences],
        f"Found {len(experiences)} relevant experiences",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/share", methods=["POST"])
@unified_auth_required
def share_agent_experience(agent_id, experience_id):
    """Share an experience with other agents in the domain."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    exp = AgentExperience.share_experience(experience_id, agent_id)
    if not exp:
        return ApiResponse.not_found("Experience not found or not owned by this agent").to_response()

    db.session.commit()
    notify_sse("agent_experience_shared", {
        "agent_id": agent_id,
        "experience_id": exp.id,
        "domain": exp.domain,
    })
    return ApiResponse.success(exp.to_dict(), "Experience shared").to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/learn", methods=["POST"])
@unified_auth_required
def learn_from_experience(agent_id, experience_id):
    """An agent internalizes a shared experience from another agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    learned = AgentExperience.learn_from_shared(
        target_agent_id=agent_id,
        experience_id=experience_id,
    )
    if not learned:
        return ApiResponse.error("Experience not found, not shared, or already learned").to_response()

    db.session.commit()
    notify_sse("agent_experience_learned", {
        "agent_id": agent_id,
        "experience_id": learned.id,
        "source_experience_id": experience_id,
    })
    return ApiResponse.success(learned.to_dict(), "Experience learned").to_response()


@agents_bp.route("/<int:agent_id>/experiences/shared", methods=["GET"])
@unified_auth_required
def list_shared_experiences(agent_id):
    """List shared experiences from other agents that this agent can learn from."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    args = get_request_args()
    query = AgentExperience.query.filter(
        AgentExperience.is_shared == True,
        AgentExperience.is_valid == True,
        AgentExperience.agent_id != agent_id,  # Exclude own experiences
    )

    domain = args.get("domain")
    if domain:
        query = query.filter_by(domain=domain)
    task_type = args.get("task_type")
    if task_type:
        query = query.filter_by(task_type=task_type)

    query = query.order_by(AgentExperience.confidence.desc(), AgentExperience.created_at.desc())
    return paginate_query(query, "experiences")


@agents_bp.route("/<int:agent_id>/experiences/auto-extract", methods=["POST"])
@unified_auth_required
def auto_extract_experiences(agent_id):
    """Auto-extract experiences from recent task outcomes for an agent.

    Scans the agent's completed workflow steps and generates experience
    records for successful and failed outcomes.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    # Find recent step runs by this agent that don't have experiences yet
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent_steps = WorkflowStepRun.query.filter(
        WorkflowStepRun.agent_id == agent_id,
        WorkflowStepRun.status.in_([StepStatus.SUCCEEDED, StepStatus.FAILED]),
        WorkflowStepRun.finished_at >= cutoff,
    ).order_by(WorkflowStepRun.finished_at.desc()).limit(50).all()

    extracted = []
    for sr in recent_steps:
        # Skip if experience already extracted for this step
        existing = AgentExperience.query.filter_by(
            agent_id=agent_id,
            source_step_key=sr.step_key,
            source_workflow_run_id=sr.run_id,
        ).first()
        if existing:
            continue

        # Get step definition
        wf_run = WorkflowRun.query.get(sr.run_id)
        step_def = None
        if wf_run:
            step_def = WorkflowStep.query.filter_by(
                workflow_id=wf_run.workflow_id, step_key=sr.step_key
            ).first()

        # Get task if available
        task = Task.query.get(sr.task_id) if sr.task_id else None

        exp = AgentExperience.extract_from_step_outcome(
            agent_id=agent_id,
            step_run=sr,
            step_def=step_def,
            task=task,
        )
        extracted.append(exp)

    db.session.commit()

    notify_sse("agent_experiences_extracted", {
        "agent_id": agent_id,
        "count": len(extracted),
    })
    return ApiResponse.success(
        [e.to_dict() for e in extracted],
        f"Extracted {len(extracted)} experiences",
    ).to_response()


# ---------------------------------------------------------------------------
# Cross-Project Agent Collaboration endpoints
# ---------------------------------------------------------------------------

