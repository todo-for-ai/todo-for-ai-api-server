"""
Agent knowledge CRUD, search, sharing, auto-extract, and propagation network endpoints.
"""

from datetime import datetime, timedelta

from flask import request
from sqlalchemy import func

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    KnowledgeEntry,
    AgentExperience,
    AuditLog,
    get_request_args,
    paginate_query,
    parse_enum,
)

@agents_bp.route("/<int:agent_id>/knowledge", methods=["GET"])
@unified_auth_required
def list_knowledge_entries(agent_id):
    """List knowledge entries for an Agent, with optional filters."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    query = KnowledgeEntry.query.filter_by(agent_id=agent_id, is_valid=True)

    domain = request.args.get("domain")
    if domain:
        query = query.filter_by(domain=domain)

    entry_type = request.args.get("entry_type")
    if entry_type:
        query = query.filter_by(entry_type=entry_type)

    source_type = request.args.get("source_type")
    if source_type:
        query = query.filter_by(source_type=source_type)

    tag = request.args.get("tag")
    if tag:
        # Filter by tag in JSON array
        query = query.filter(KnowledgeEntry.tags.contains([tag]))

    search = request.args.get("search", "").strip()
    if search:
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(f"%{search}%"),
                KnowledgeEntry.content.ilike(f"%{search}%"),
            )
        )

    include_content = request.args.get("include_content", "true").lower() == "true"
    query = query.order_by(KnowledgeEntry.updated_at.desc())
    result = paginate_query(query, default_per_page=50)

    entries = [e.to_dict(include_content=include_content) for e in result.items]
    return ApiResponse.success({
        "items": entries,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
    }).to_response()


@agents_bp.route("/<int:agent_id>/knowledge", methods=["POST"])
@unified_auth_required
def create_knowledge_entry(agent_id):
    """Create a knowledge entry for an Agent."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request()
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return ApiResponse.error("title and content are required", 400).to_response()

    entry = KnowledgeEntry.create(
        agent_id=agent_id,
        title=title,
        content=content,
        domain=data.get("domain"),
        tags=data.get("tags", []),
        entry_type=data.get("entry_type", "insight"),
        source_task_id=data.get("source_task_id"),
        source_type=data.get("source_type", "manual"),
        confidence=data.get("confidence", 1.0),
        shared_with_project=data.get("shared_with_project", False),
        project_id=data.get("project_id"),
    )
    db.session.commit()

    AuditLog.record("knowledge_entry_create", target_type="knowledge_entry",
                     target_id=entry.id, actor_type="human", actor_user_id=user.id,
                     detail={"agent_id": agent_id, "title": title, "domain": data.get("domain")},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.created(entry.to_dict(), "Knowledge entry created").to_response()


@agents_bp.route("/<int:agent_id>/knowledge/<int:entry_id>", methods=["GET"])
@unified_auth_required
def get_knowledge_entry(agent_id, entry_id):
    """Get a specific knowledge entry."""
    user = get_current_user()
    entry = KnowledgeEntry.query.filter_by(id=entry_id, agent_id=agent_id).first()
    if not entry:
        return ApiResponse.not_found("Knowledge entry not found").to_response()

    # Access control: owner or shared with project
    agent = Agent.query.filter_by(id=agent_id).first()
    if agent and agent.owner_id != user.id:
        if not entry.shared_with_project or not entry.project_id:
            return ApiResponse.not_found("Knowledge entry not found").to_response()
        # Check project membership
        pm = ProjectMember.query.filter_by(project_id=entry.project_id, user_id=user.id).first()
        if not pm:
            return ApiResponse.not_found("Knowledge entry not found").to_response()

    # Increment access count
    entry.access_count = (entry.access_count or 0) + 1
    db.session.commit()

    return ApiResponse.success(entry.to_dict()).to_response()


@agents_bp.route("/<int:agent_id>/knowledge/<int:entry_id>", methods=["PUT"])
@unified_auth_required
def update_knowledge_entry(agent_id, entry_id):
    """Update a knowledge entry."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    entry = KnowledgeEntry.query.filter_by(id=entry_id, agent_id=agent_id).first()
    if not entry:
        return ApiResponse.not_found("Knowledge entry not found").to_response()

    data = validate_json_request()
    if "title" in data:
        entry.title = data["title"]
    if "content" in data:
        entry.content = data["content"]
    if "domain" in data:
        entry.domain = data["domain"]
    if "tags" in data:
        entry.tags = data["tags"]
    if "entry_type" in data:
        entry.entry_type = data["entry_type"]
    if "confidence" in data:
        entry.confidence = data["confidence"]
    if "is_valid" in data:
        entry.is_valid = data["is_valid"]
    if "shared_with_project" in data:
        entry.shared_with_project = data["shared_with_project"]
    if "project_id" in data:
        entry.project_id = data["project_id"]

    db.session.commit()
    return ApiResponse.success(entry.to_dict(), "Knowledge entry updated").to_response()


@agents_bp.route("/<int:agent_id>/knowledge/<int:entry_id>", methods=["DELETE"])
@unified_auth_required
def delete_knowledge_entry(agent_id, entry_id):
    """Delete (invalidate) a knowledge entry."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    entry = KnowledgeEntry.query.filter_by(id=entry_id, agent_id=agent_id).first()
    if not entry:
        return ApiResponse.not_found("Knowledge entry not found").to_response()

    # Soft delete — mark as invalid instead of removing
    entry.is_valid = False
    db.session.commit()
    return ApiResponse.success(None, "Knowledge entry deleted").to_response()


@agents_bp.route("/<int:agent_id>/knowledge/search", methods=["GET"])
@unified_auth_required
def search_knowledge(agent_id):
    """Search knowledge entries by query string, domain, or tags."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    # Access control: must be owner or shared project member
    if agent.owner_id != user.id:
        return ApiResponse.error("Access denied", 403).to_response()

    q = request.args.get("q", "").strip()
    domain = request.args.get("domain")
    tags = request.args.get("tags", "")  # comma-separated
    limit = min(int(request.args.get("limit", 20)), 100)
    entry_type = request.args.get("entry_type")

    query = KnowledgeEntry.query.filter_by(agent_id=agent_id, is_valid=True)

    if domain:
        query = query.filter_by(domain=domain)
    if entry_type:
        query = query.filter_by(entry_type=entry_type)
    if tags:
        for tag in tags.split(","):
            tag = tag.strip()
            if tag:
                query = query.filter(KnowledgeEntry.tags.contains([tag]))
    if q:
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(f"%{q}%"),
                KnowledgeEntry.content.ilike(f"%{q}%"),
            )
        )

    entries = query.order_by(KnowledgeEntry.confidence.desc(), KnowledgeEntry.access_count.desc()).limit(limit).all()
    return ApiResponse.success([e.to_dict() for e in entries]).to_response()


@agents_bp.route("/knowledge/shared", methods=["GET"])
@unified_auth_required
def list_shared_knowledge():
    """List knowledge entries shared with projects the user is a member of."""
    user = get_current_user()
    project_ids = [pm.project_id for pm in ProjectMember.query.filter_by(user_id=user.id).all()]

    if not project_ids:
        return ApiResponse.success([]).to_response()

    domain = request.args.get("domain")
    entry_type = request.args.get("entry_type")
    search = request.args.get("search", "").strip()

    query = KnowledgeEntry.query.filter(
        KnowledgeEntry.shared_with_project == True,
        KnowledgeEntry.is_valid == True,
        KnowledgeEntry.project_id.in_(project_ids),
    )
    if domain:
        query = query.filter_by(domain=domain)
    if entry_type:
        query = query.filter_by(entry_type=entry_type)
    if search:
        query = query.filter(
            db.or_(
                KnowledgeEntry.title.ilike(f"%{search}%"),
                KnowledgeEntry.content.ilike(f"%{search}%"),
            )
        )

    query = query.order_by(KnowledgeEntry.updated_at.desc())
    result = paginate_query(query, default_per_page=50)
    entries = [e.to_dict(include_content=False) for e in result.items]
    return ApiResponse.success({
        "items": entries,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
    }).to_response()


@agents_bp.route("/<int:agent_id>/knowledge/auto-extract", methods=["POST"])
@unified_auth_required
def auto_extract_knowledge(agent_id):
    """Auto-extract knowledge from completed task assignments for an Agent.

    Reviews the Agent's recently completed tasks and generates knowledge
    entries from task summaries and results.
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request() or {}
    limit = min(data.get("limit", 10), 50)

    # Find recently completed assignments with output summaries
    recent_assignments = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent_id,
        TaskAssignment.state == TaskAssignmentState.DONE,
        TaskAssignment.output_summary.isnot(None),
        TaskAssignment.output_summary != "",
    ).order_by(TaskAssignment.updated_at.desc()).limit(limit).all()

    created = []
    for assignment in recent_assignments:
        task = Task.query.get(assignment.task_id) if assignment.task_id else None
        # Check if we already have a knowledge entry for this task
        existing = KnowledgeEntry.query.filter_by(
            agent_id=agent_id,
            source_task_id=assignment.task_id,
            source_type="auto_extracted",
        ).first()
        if existing:
            continue

        title = f"经验: {task.title if task else f'任务 #{assignment.task_id}'}"
        content_parts = []
        if task:
            content_parts.append(f"任务: {task.title}")
            content_parts.append(f"描述: {task.description or '无'}")
        content_parts.append(f"执行摘要: {assignment.output_summary}")
        if assignment.notes:
            content_parts.append(f"备注: {assignment.notes}")

        # Infer domain from task tags
        domain = None
        if task and task.tags:
            task_tags = task.tags if isinstance(task.tags, list) else []
            if task_tags:
                domain = task_tags[0]

        entry = KnowledgeEntry.create(
            agent_id=agent_id,
            title=title,
            content="\n\n".join(content_parts),
            domain=domain,
            tags=task.tags if task and task.tags else [],
            entry_type="insight",
            source_task_id=assignment.task_id,
            source_type="auto_extracted",
            confidence=0.7,  # Auto-extracted starts with lower confidence
        )
        created.append(entry)

    db.session.commit()

    AuditLog.record("knowledge_auto_extract", target_type="agent",
                     target_id=agent_id, actor_type="human", actor_user_id=user.id,
                     detail={"entries_created": len(created)},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.success({
        "entries_created": len(created),
        "entries": [e.to_dict() for e in created],
    }, f"Auto-extracted {len(created)} knowledge entries").to_response()


# =========================================================================
# Workflow Version Management
# =========================================================================

@agents_bp.route("/knowledge-propagation-network", methods=["GET"])
@unified_auth_required
def knowledge_propagation_network():
    """Build a cross-Agent knowledge propagation network.

    Nodes are Agents that shared experiences (sources); edges connect
    frequent contributors weighted by reuse volume, revealing which
    Agents propagate knowledge most broadly.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 90))))
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        days, limit = 90, 20

    from models.agent import AgentExperience
    since = datetime.utcnow() - timedelta(days=days)

    shared = (
        AgentExperience.query
        .join(Agent, AgentExperience.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            AgentExperience.is_shared == True,
            AgentExperience.created_at >= since,
        )
        .with_entities(
            AgentExperience.agent_id,
            AgentExperience.domain,
            AgentExperience.times_reused,
            AgentExperience.id,
        )
        .all()
    )

    source_contrib = {}
    for src_id, domain, reused, _eid in shared:
        c = source_contrib.setdefault(src_id, {"domains": set(), "reuses": 0, "exps": 0})
        if domain:
            c["domains"].add(domain)
        c["reuses"] += reused or 0
        c["exps"] += 1

    all_agents = Agent.query.filter_by(owner_id=user.id).all()
    agent_names = {a.id: (a.name or f"Agent#{a.id}") for a in all_agents}

    nodes = []
    for src_id, c in sorted(source_contrib.items(), key=lambda kv: kv[1]["reuses"], reverse=True)[:limit]:
        nodes.append({
            "agent_id": src_id,
            "agent_name": agent_names.get(src_id, f"Agent#{src_id}"),
            "shared_experiences": c["exps"],
            "total_reuses": c["reuses"],
            "domains": sorted(c["domains"])[:8],
        })

    edges = []
    top_ids = [n["agent_id"] for n in nodes]
    for i in range(len(top_ids)):
        for j in range(len(top_ids)):
            if i == j:
                continue
            src_reuses = source_contrib.get(top_ids[i], {}).get("reuses", 0)
            dst_reuses = source_contrib.get(top_ids[j], {}).get("reuses", 0)
            if src_reuses > 0 and dst_reuses > 0:
                flow = round(min(src_reuses, dst_reuses) * 0.2)
                if flow > 0:
                    edges.append({"source": top_ids[i], "target": top_ids[j], "weight": flow})
    edges.sort(key=lambda e: e["weight"], reverse=True)
    edges = edges[:limit]

    total_shared = sum(c["exps"] for c in source_contrib.values())
    total_reuses = sum(c["reuses"] for c in source_contrib.values())
    return ApiResponse.success({
        "nodes": nodes,
        "edges": edges,
        "days": days,
        "total_shared_experiences": total_shared,
        "total_reuses": total_reuses,
    }).to_response()

