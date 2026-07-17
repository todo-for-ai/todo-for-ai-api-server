"""
Agent experience CRUD, analytics, decay, validation, and sharing endpoints.
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
    AgentExperience,
    AgentReputation,
    Task,
    TaskAssignment,
    TaskAssignmentState,
    TaskStatus,
    AuditLog,
    KnowledgeEntry,
    get_request_args,
    paginate_query,
    _queue_sse,
    _client_ip,
    flush_sse_notifications,
    parse_enum,
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


@agents_bp.route("/experiences/stats", methods=["GET"])
@unified_auth_required
def experiences_stats():
    """Aggregate AgentExperience stats for the current user.

    Breaks down experiences (valid only) by domain, task_type, and
    experience_type across all of the user's Agents. Also reports shared
    count, total reuse count, and average confidence. Reveals where the
    collective knowledge base is concentrated and where it is thin.
    """
    user = get_current_user()
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({
            "total": 0, "by_domain": {}, "by_task_type": {},
            "by_experience_type": {}, "shared": 0, "total_reuses": 0, "avg_confidence": None,
            "by_confidence_bucket": {}, "top_reused": [], "by_domain_tasktype": {}, "by_domain_reuses": {}, "by_task_type_reuses": {}, "by_experience_type_reuses": {},
        }).to_response()

    rows = AgentExperience.query.filter(
        AgentExperience.agent_id.in_(agent_ids),
        AgentExperience.is_valid.is_(True),
    ).with_entities(
        AgentExperience.id,
        AgentExperience.domain,
        AgentExperience.task_type,
        AgentExperience.experience_type,
        AgentExperience.is_shared,
        AgentExperience.times_reused,
        AgentExperience.confidence,
        AgentExperience.key_learnings,
    ).all()

    by_domain: dict = {}
    by_task_type: dict = {}
    by_exp_type: dict = {}
    shared = 0
    total_reuses = 0
    confidences = []
    confidence_buckets = {"0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
    reuse_candidates = []
    domain_task_matrix: dict = {}  # {domain: {task_type: count}}
    by_domain_reuses: dict = {}  # {domain: cumulative reuse count}
    by_task_type_reuses: dict = {}  # {task_type: cumulative reuse count}
    by_exp_type_reuses: dict = {}  # {experience_type: cumulative reuse count}
    for exp_id, domain, task_type, exp_type, is_shared, times_reused, confidence, key_learnings in rows:
        d = domain or "(未分类)"
        by_domain[d] = by_domain.get(d, 0) + 1
        by_domain_reuses[d] = by_domain_reuses.get(d, 0) + (times_reused or 0)
        tt = task_type or "(未分类)"
        if task_type:
            by_task_type[task_type] = by_task_type.get(task_type, 0) + 1
            by_task_type_reuses[task_type] = by_task_type_reuses.get(task_type, 0) + (times_reused or 0)
        domain_task_matrix.setdefault(d, {})
        domain_task_matrix[d][tt] = domain_task_matrix[d].get(tt, 0) + 1
        et = exp_type or "(未分类)"
        by_exp_type[et] = by_exp_type.get(et, 0) + 1
        by_exp_type_reuses[et] = by_exp_type_reuses.get(et, 0) + (times_reused or 0)
        if is_shared:
            shared += 1
        total_reuses += times_reused or 0
        if confidence is not None:
            confidences.append(confidence)
            c = confidence
            if c < 0.3:
                confidence_buckets["0-0.3"] += 1
            elif c < 0.5:
                confidence_buckets["0.3-0.5"] += 1
            elif c < 0.7:
                confidence_buckets["0.5-0.7"] += 1
            elif c < 0.85:
                confidence_buckets["0.7-0.85"] += 1
            else:
                confidence_buckets["0.85-1.0"] += 1
        if (times_reused or 0) > 0:
            reuse_candidates.append({
                "id": exp_id,
                "domain": d,
                "task_type": task_type,
                "experience_type": et,
                "times_reused": times_reused or 0,
                "confidence": confidence,
                "key_learnings": (key_learnings or "")[:120],
            })

    avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else None
    # Sort breakdowns by count desc for display
    by_domain_sorted = dict(sorted(by_domain.items(), key=lambda kv: kv[1], reverse=True))
    by_task_sorted = dict(sorted(by_task_type.items(), key=lambda kv: kv[1], reverse=True))
    by_domain_reuses_sorted = dict(sorted(by_domain_reuses.items(), key=lambda kv: kv[1], reverse=True))
    by_task_type_reuses_sorted = dict(sorted(by_task_type_reuses.items(), key=lambda kv: kv[1], reverse=True))
    by_exp_type_reuses_sorted = dict(sorted(by_exp_type_reuses.items(), key=lambda kv: kv[1], reverse=True))
    top_reused = sorted(reuse_candidates, key=lambda x: x["times_reused"], reverse=True)[:10]
    return ApiResponse.success({
        "total": len(rows),
        "by_domain": by_domain_sorted,
        "by_task_type": by_task_sorted,
        "by_experience_type": by_exp_type,
        "shared": shared,
        "total_reuses": total_reuses,
        "avg_confidence": avg_conf,
        "by_confidence_bucket": confidence_buckets,
        "top_reused": top_reused,
        "by_domain_tasktype": domain_task_matrix,
        "by_domain_reuses": by_domain_reuses_sorted,
        "by_task_type_reuses": by_task_type_reuses_sorted,
        "by_experience_type_reuses": by_exp_type_reuses_sorted,
    }).to_response()


@agents_bp.route("/experiences/scatter", methods=["GET"])
@unified_auth_required
def experiences_scatter():
    """Confidence × reuse-count scatter points for the user's experiences.

    Returns one point per valid experience: confidence, times_reused, domain,
    task_type, experience_type. Lets the frontend plot a scatter chart showing
    whether high-confidence experiences actually get reused more. Caps the
    point count via ``limit`` (default 200, newest first) to bound payload.
    """
    user = get_current_user()
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"points": [], "max_reuses": 0}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .order_by(AgentExperience.times_reused.desc())
        .with_entities(
            AgentExperience.id,
            AgentExperience.domain,
            AgentExperience.task_type,
            AgentExperience.experience_type,
            AgentExperience.times_reused,
            AgentExperience.confidence,
        )
        .limit(limit)
        .all()
    )

    points = []
    max_reuses = 0
    for exp_id, domain, task_type, exp_type, times_reused, confidence in rows:
        tr = times_reused or 0
        if tr > max_reuses:
            max_reuses = tr
        points.append({
            "id": exp_id,
            "domain": domain or "(未分类)",
            "task_type": task_type or "(未分类)",
            "experience_type": exp_type or "(未分类)",
            "times_reused": tr,
            "confidence": confidence,
        })
    return ApiResponse.success({"points": points, "max_reuses": max_reuses}).to_response()


@agents_bp.route("/experiences/decay-by-domain", methods=["GET"])
@unified_auth_required
def experiences_decay_by_domain():
    """Per-domain decay comparison for the user's experiences.

    Aggregates valid experiences by domain, reporting for each domain:
    total count, active count (confidence >= 0.5), decayed count
    (confidence < 0.5), average confidence, and total reuses.
    Sorted by decayed count descending. Reveals which knowledge
    domains have the most stale / low-confidence entries.
    """
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        limit = 15

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"domains": [], "total_active": 0, "total_decayed": 0}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.domain,
            AgentExperience.confidence,
            AgentExperience.times_reused,
        )
        .all()
    )

    buckets = {}  # {domain: {total, active, decayed, conf_sum, conf_n, reuses}}
    for domain, confidence, times_reused in rows:
        d = domain or "(未分类)"
        conf = confidence if confidence is not None else 0.0
        b = buckets.get(d)
        if b is None:
            b = {"total": 0, "active": 0, "decayed": 0, "conf_sum": 0.0, "conf_n": 0, "reuses": 0}
            buckets[d] = b
        b["total"] += 1
        if conf >= 0.5:
            b["active"] += 1
        else:
            b["decayed"] += 1
        b["conf_sum"] += conf
        b["conf_n"] += 1
        b["reuses"] += (times_reused or 0)

    total_active = sum(b["active"] for b in buckets.values())
    total_decayed = sum(b["decayed"] for b in buckets.values())

    domains = []
    for d, b in buckets.items():
        domains.append({
            "domain": d,
            "total": b["total"],
            "active": b["active"],
            "decayed": b["decayed"],
            "avg_confidence": round(b["conf_sum"] / b["conf_n"], 3) if b["conf_n"] else 0.0,
            "reuses": b["reuses"],
        })
    domains.sort(key=lambda x: x["decayed"], reverse=True)
    domains = domains[:limit]

    return ApiResponse.success({
        "domains": domains,
        "total_active": total_active,
        "total_decayed": total_decayed,
    }).to_response()


@agents_bp.route("/experiences/decay-by-task-type", methods=["GET"])
@unified_auth_required
def experiences_decay_by_task_type():
    """Per-task-type decay comparison for the user's experiences.

    Same as decay-by-domain but grouped by task_type. Reveals which
    task categories have the most stale / low-confidence entries.
    """
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 15))))
    except (TypeError, ValueError):
        limit = 15

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"task_types": [], "total_active": 0, "total_decayed": 0}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.task_type,
            AgentExperience.confidence,
            AgentExperience.times_reused,
        )
        .all()
    )

    buckets = {}
    for task_type, confidence, times_reused in rows:
        t = task_type or "(未分类)"
        conf = confidence if confidence is not None else 0.0
        b = buckets.get(t)
        if b is None:
            b = {"total": 0, "active": 0, "decayed": 0, "conf_sum": 0.0, "conf_n": 0, "reuses": 0}
            buckets[t] = b
        b["total"] += 1
        if conf >= 0.5:
            b["active"] += 1
        else:
            b["decayed"] += 1
        b["conf_sum"] += conf
        b["conf_n"] += 1
        b["reuses"] += (times_reused or 0)

    total_active = sum(b["active"] for b in buckets.values())
    total_decayed = sum(b["decayed"] for b in buckets.values())

    task_types = []
    for t, b in buckets.items():
        task_types.append({
            "task_type": t,
            "total": b["total"],
            "active": b["active"],
            "decayed": b["decayed"],
            "avg_confidence": round(b["conf_sum"] / b["conf_n"], 3) if b["conf_n"] else 0.0,
            "reuses": b["reuses"],
        })
    task_types.sort(key=lambda x: x["decayed"], reverse=True)
    task_types = task_types[:limit]

    return ApiResponse.success({
        "task_types": task_types,
        "total_active": total_active,
        "total_decayed": total_decayed,
    }).to_response()


@agents_bp.route("/experiences/confidence-distribution", methods=["GET"])
@unified_auth_required
def experiences_confidence_distribution():
    """Confidence interval distribution for the user's experiences.

    Buckets all valid experiences by confidence into 5 intervals:
    [0,0.2), [0.2,0.4), [0.4,0.6), [0.6,0.8), [0.8,1.0].
    Returns per-bucket count, percentage, and average times_reused.
    Reveals whether the experience pool is mostly high- or low-confidence.
    """
    user = get_current_user()

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"bins": [], "total": 0}).to_response()

    BINS = [
        ("0-0.2", 0.0, 0.2),
        ("0.2-0.4", 0.2, 0.4),
        ("0.4-0.6", 0.4, 0.6),
        ("0.6-0.8", 0.6, 0.8),
        ("0.8-1.0", 0.8, 1.01),  # include 1.0
    ]

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.confidence,
            AgentExperience.times_reused,
        )
        .all()
    )

    total = len(rows)
    bin_data = {label: {"count": 0, "reuses": 0} for label, _, _ in BINS}
    for confidence, times_reused in rows:
        conf = confidence if confidence is not None else 0.0
        for label, lo, hi in BINS:
            if lo <= conf < hi:
                bin_data[label]["count"] += 1
                bin_data[label]["reuses"] += (times_reused or 0)
                break

    bins = []
    for label, lo, hi in BINS:
        d = bin_data[label]
        bins.append({
            "label": label,
            "range_low": lo,
            "range_high": min(hi, 1.0),
            "count": d["count"],
            "percentage": round(d["count"] / total * 100, 1) if total else 0.0,
            "avg_reuses": round(d["reuses"] / d["count"], 1) if d["count"] else 0.0,
        })

    return ApiResponse.success({"bins": bins, "total": total}).to_response()


@agents_bp.route("/experiences/source-distribution", methods=["GET"])
@unified_auth_required
def experiences_source_distribution():
    """Experience count by creation source for the current user.

    Groups valid experiences by origin: manual (no workflow run),
    workflow (has source_workflow_run_id), auto_step (has source_step_key
    but no workflow run). Per-source: count, percentage, avg confidence,
    avg times_reused. Reveals where the experience pool comes from.
    """
    user = get_current_user()

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"sources": [], "total": 0}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.source_workflow_run_id,
            AgentExperience.source_step_key,
            AgentExperience.confidence,
            AgentExperience.times_reused,
        )
        .all()
    )

    total = len(rows)
    src_data: dict = {}
    for wf_run_id, step_key, confidence, times_reused in rows:
        if wf_run_id is not None:
            src = "workflow"
        elif step_key is not None:
            src = "auto_step"
        else:
            src = "manual"
        if src not in src_data:
            src_data[src] = {"count": 0, "conf_sum": 0.0, "reuse_sum": 0}
        src_data[src]["count"] += 1
        src_data[src]["conf_sum"] += (confidence if confidence is not None else 0.0)
        src_data[src]["reuse_sum"] += (times_reused or 0)

    sources = []
    for src, d in sorted(src_data.items(), key=lambda kv: kv[1]["count"], reverse=True):
        sources.append({
            "source": src,
            "count": d["count"],
            "percentage": round(d["count"] / total * 100, 1) if total else 0.0,
            "avg_confidence": round(d["conf_sum"] / d["count"], 3) if d["count"] else 0.0,
            "avg_reuses": round(d["reuse_sum"] / d["count"], 1) if d["count"] else 0.0,
        })

    return ApiResponse.success({"sources": sources, "total": total}).to_response()


@agents_bp.route("/experiences/propagation-chain", methods=["GET"])
@unified_auth_required
def experiences_propagation_chain():
    """Experience sharing propagation chain for the current user.

    Groups shared experiences (is_shared=True) by source agent_id. Per-agent:
    shared_count, total_reuses, top domains, and top propagated experiences.
    Reveals which agents contribute most to collective learning and how
    knowledge flows from source to the rest of the fleet.
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"chains": [], "total_shared": 0, "total_propagated": 0}).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    # Query all shared, valid experiences
    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_shared.is_(True),
            AgentExperience.is_valid.is_(True),
        )
        .all()
    )

    chain_data: dict = {}  # {agent_id: {shared_count, total_reuses, domains: {d: count}, top_exp: [...]}}
    total_shared = 0
    total_propagated = 0
    for exp in rows:
        aid = exp.agent_id
        if aid not in chain_data:
            chain_data[aid] = {"shared_count": 0, "total_reuses": 0, "domains": {}, "top_exp": []}
        chain_data[aid]["shared_count"] += 1
        total_shared += 1
        reuses = exp.times_reused or 0
        chain_data[aid]["total_reuses"] += reuses
        total_propagated += reuses
        if exp.domain:
            chain_data[aid]["domains"][exp.domain] = chain_data[aid]["domains"].get(exp.domain, 0) + 1
        chain_data[aid]["top_exp"].append({
            "id": exp.id,
            "domain": exp.domain,
            "experience_type": exp.experience_type,
            "times_reused": reuses,
            "confidence": exp.confidence,
        })

    chains = []
    for aid, d in sorted(chain_data.items(), key=lambda kv: kv[1]["total_reuses"], reverse=True)[:limit]:
        # Sort top_exp by times_reused desc, keep top 5
        top_exp = sorted(d["top_exp"], key=lambda x: x["times_reused"], reverse=True)[:5]
        # Top 3 domains
        top_domains = sorted(d["domains"].items(), key=lambda kv: kv[1], reverse=True)[:3]
        chains.append({
            "source_agent_id": aid,
            "source_agent_name": name_map.get(aid, f"Agent#{aid}"),
            "shared_count": d["shared_count"],
            "total_reuses": d["total_reuses"],
            "top_domains": [dm for dm, _ in top_domains],
            "top_experiences": top_exp,
        })

    return ApiResponse.success({
        "chains": chains,
        "total_shared": total_shared,
        "total_propagated": total_propagated,
    }).to_response()


@agents_bp.route("/experiences/skill-coverage-radar", methods=["GET"])
@unified_auth_required
def experiences_skill_coverage_radar():
    """Per-Agent skill coverage radar across experience domains.

    For each of the user's agents, counts distinct domain entries in
    AgentExperience (valid only). Returns a domain list (up to N most
    common) and per-agent normalized scores (0-100) so the front end can
    render a radar/spider chart.
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 6))))
        domains = max(3, min(12, int(request.args.get("domains", 8))))
    except (TypeError, ValueError):
        limit = 6
        domains = 8

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({
            "agents": [], "domain_labels": [], "max_count": 0,
        }).to_response()

    name_map = {
        aid: name
        for aid, name in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(Agent.id, Agent.name).all()
    }

    # Find top N domains by total experience count
    domain_rows = (
        AgentExperience.query
        .filter(AgentExperience.agent_id.in_(agent_ids), AgentExperience.is_valid.is_(True))
        .with_entities(AgentExperience.domain, func.count().label("cnt"))
        .group_by(AgentExperience.domain)
        .order_by(func.count().desc())
        .limit(domains)
        .all()
    )
    domain_labels = [d for d, _ in domain_rows if d]
    if not domain_labels:
        return ApiResponse.success({
            "agents": [], "domain_labels": [], "max_count": 0,
        }).to_response()

    # Per-agent per-domain counts
    exp_rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
            AgentExperience.domain.in_(domain_labels),
        )
        .with_entities(AgentExperience.agent_id, AgentExperience.domain, func.count().label("cnt"))
        .group_by(AgentExperience.agent_id, AgentExperience.domain)
        .all()
    )

    # Build {agent_id: {domain: count}}
    data: dict = {}
    for aid, dom, cnt in exp_rows:
        data.setdefault(aid, {})[dom] = cnt

    # Find max count for normalization
    max_count = max((cnt for _, _, cnt in exp_rows), default=1)

    # Sort agents by total experience count
    totals = {aid: sum(d.values()) for aid, d in data.items()}
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    agents_out = []
    for aid, _ in top:
        scores = []
        for dom in domain_labels:
            raw = data.get(aid, {}).get(dom, 0)
            scores.append(round(raw / max_count * 100, 1) if max_count else 0.0)
        agents_out.append({
            "agent_id": aid,
            "name": name_map.get(aid, f"Agent#{aid}"),
            "scores": scores,
            "total_experiences": totals.get(aid, 0),
        })

    return ApiResponse.success({
        "agents": agents_out,
        "domain_labels": domain_labels,
        "max_count": max_count,
    }).to_response()


@agents_bp.route("/experiences/reuse-trend", methods=["GET"])
@unified_auth_required
def experiences_reuse_trend():
    """Daily reuse + decay trend for the user's experiences.

    Buckets valid experiences by the date they were last reused (falling back
    to creation date when never reused). For each day reports: number of
    experiences reused that day, total reuse count accumulated that day,
    average confidence, and how many of those experiences are now decayed
    (confidence < 0.5) — surfacing whether reuse keeps knowledge fresh or
    whether stale experiences still linger. Supports ``days`` window.
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = datetime.utcnow() - timedelta(days=days)

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({
            "trend": [],
            "total_reused": 0,
            "total_reuse_count": 0,
            "decayed_count": 0,
            "total_experiences": 0,
        }).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
        )
        .with_entities(
            AgentExperience.id,
            AgentExperience.times_reused,
            AgentExperience.confidence,
            AgentExperience.last_reused_at,
            AgentExperience.created_at,
        )
        .all()
    )

    buckets = {}
    total_reused = 0          # experiences with times_reused > 0
    total_reuse_count = 0     # sum of times_reused
    decayed_count = 0         # confidence < 0.5
    for _exp_id, times_reused, confidence, last_reused_at, created_at in rows:
        tr = times_reused or 0
        conf = confidence if confidence is not None else 0.0
        ref = last_reused_at or created_at
        if ref is None:
            continue
        if ref < since:
            # Only count experiences that were reused/created within the window
            # for the daily buckets, but still contribute to totals.
            total_reuse_count += tr
            if tr > 0:
                total_reused += 1
            if conf < 0.5:
                decayed_count += 1
            continue
        d = ref.date().isoformat()
        b = buckets.get(d)
        if b is None:
            b = {"date": d, "reused": 0, "reuse_count": 0, "conf_sum": 0.0, "conf_n": 0, "decayed": 0}
            buckets[d] = b
        b["reused"] += 1 if tr > 0 else 0
        b["reuse_count"] += tr
        b["conf_sum"] += conf
        b["conf_n"] += 1
        if conf < 0.5:
            b["decayed"] += 1
        total_reuse_count += tr
        if tr > 0:
            total_reused += 1
        if conf < 0.5:
            decayed_count += 1

    trend = []
    for d in sorted(buckets.keys()):
        b = buckets[d]
        trend.append({
            "date": b["date"],
            "reused": b["reused"],
            "reuse_count": b["reuse_count"],
            "avg_confidence": round(b["conf_sum"] / b["conf_n"], 3) if b["conf_n"] else 0.0,
            "decayed": b["decayed"],
        })

    return ApiResponse.success({
        "trend": trend,
        "total_reused": total_reused,
        "total_reuse_count": total_reuse_count,
        "decayed_count": decayed_count,
        "total_experiences": len(rows),
    }).to_response()


@agents_bp.route("/experiences/confidence-decay-forecast", methods=["GET"])
@unified_auth_required
def experiences_confidence_decay_forecast():
    """Confidence decay forecast using linear regression on daily averages.

    Computes daily average confidence from the reuse trend, fits a simple
    linear regression, and projects 7 days into the future. Returns the
    historical trend plus forecast points, regression slope, and projected
    days-until-decay-threshold (avg confidence < 0.5). Reveals whether
    the experience pool is decaying and when it might cross the decay
    threshold if the trend continues.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"trend": [], "forecast": [], "slope": 0, "r_squared": 0, "days_to_decay": None}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
            AgentExperience.confidence.isnot(None),
        )
        .with_entities(
            AgentExperience.confidence,
            AgentExperience.last_reused_at,
            AgentExperience.created_at,
        )
        .all()
    )

    since = datetime.utcnow() - timedelta(days=days)

    # Bucket by date
    buckets: dict = {}
    for conf, last_reused_at, created_at in rows:
        ref = last_reused_at or created_at
        if ref is None or ref < since:
            continue
        d = ref.date().isoformat()
        buckets.setdefault(d, []).append(conf if conf is not None else 0.0)

    if len(buckets) < 3:
        return ApiResponse.success({"trend": [], "forecast": [], "slope": 0, "r_squared": 0, "days_to_decay": None}).to_response()

    # Build sorted daily averages
    daily = []
    for d in sorted(buckets.keys()):
        vals = buckets[d]
        daily.append({"date": d, "avg_confidence": round(sum(vals) / len(vals), 3)})

    # Linear regression: y = a + b*x
    n = len(daily)
    xs = list(range(n))
    ys = [d["avg_confidence"] for d in daily]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    ss_yy = sum((y - y_mean) ** 2 for y in ys)

    b = ss_xy / ss_xx if ss_xx else 0.0
    a = y_mean - b * x_mean
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx and ss_yy else 0.0

    # Forecast 7 days ahead
    from datetime import date as date_type
    last_date = datetime.strptime(daily[-1]["date"], "%Y-%m-%d").date()
    forecast = []
    for i in range(1, 8):
        fx = n - 1 + i
        fy = a + b * fx
        fd = last_date + timedelta(days=i)
        forecast.append({"date": fd.isoformat(), "predicted_confidence": round(max(0, min(1, fy)), 3)})

    # Days until avg confidence < 0.5
    days_to_decay = None
    if b < 0 and y_mean > 0.5:
        # Solve a + b * x = 0.5
        x_decay = (0.5 - a) / b
        days_to_decay = max(0, round(x_decay - (n - 1)))

    return ApiResponse.success({
        "trend": daily,
        "forecast": forecast,
        "slope": round(b, 4),
        "r_squared": round(r_squared, 4),
        "days_to_decay": days_to_decay,
    }).to_response()


@agents_bp.route("/experiences/low-confidence", methods=["GET"])
@unified_auth_required
def experiences_low_confidence():
    """List the current user's valid experiences with low confidence.

    Returns experiences (across all of the user's Agents) whose confidence
    falls below ``max_confidence`` (default 0.5), sorted by confidence
    ascending. Each entry includes agent_id, domain, task_type,
    experience_type, confidence, times_reused, and a key_learnings excerpt.
    Surfaces weak knowledge entries that may need reinforcement or removal.
    """
    user = get_current_user()
    try:
        max_confidence = max(0.0, min(1.0, float(request.args.get("max_confidence", 0.5))))
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        max_confidence = 0.5
        limit = 20

    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).with_entities(Agent.id).all()]
    if not agent_ids:
        return ApiResponse.success({"max_confidence": max_confidence, "items": []}).to_response()

    rows = (
        AgentExperience.query
        .filter(
            AgentExperience.agent_id.in_(agent_ids),
            AgentExperience.is_valid.is_(True),
            AgentExperience.confidence.isnot(None),
            AgentExperience.confidence < max_confidence,
        )
        .order_by(AgentExperience.confidence.asc())
        .limit(limit)
        .with_entities(
            AgentExperience.id, AgentExperience.agent_id,
            AgentExperience.domain, AgentExperience.task_type,
            AgentExperience.experience_type, AgentExperience.confidence,
            AgentExperience.times_reused, AgentExperience.key_learnings,
        )
        .all()
    )

    items = [{
        "id": r.id,
        "agent_id": r.agent_id,
        "domain": r.domain or "(未分类)",
        "task_type": r.task_type,
        "experience_type": r.experience_type or "(未分类)",
        "confidence": r.confidence,
        "times_reused": r.times_reused or 0,
        "key_learnings": (r.key_learnings or "")[:120],
    } for r in rows]

    return ApiResponse.success({"max_confidence": max_confidence, "items": items}).to_response()


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

@agents_bp.route("/cross-project/authorize", methods=["POST"])
@unified_auth_required
def authorize_cross_project_agent():
    """Authorize an Agent to work in a different project.

    The authorizing user must be an ADMIN or OWNER of the target project.
    The agent's owner must be a member of the target project or the authorizer
    must be the agent's owner.
    """
    user = get_current_user()
    data = validate_json_request()

    agent_id = data.get("agent_id")
    project_id = data.get("project_id")
    if not agent_id or not project_id:
        return ApiResponse.error("agent_id and project_id are required").to_response()

    # Verify agent exists and user owns it
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found or not owned by you").to_response()

    # Verify project exists and user has admin access
    project = Project.query.get(project_id)
    if not project:
        return ApiResponse.not_found("Project not found").to_response()

    user_role = ProjectMember.get_role(project_id, user.id)
    if user_role not in (ProjectRole.OWNER, ProjectRole.ADMIN):
        return ApiResponse.error("You must be ADMIN or OWNER of the target project").to_response()

    # Check if already authorized
    existing = CrossProjectAgent.query.filter_by(
        agent_id=agent_id, project_id=project_id
    ).first()
    if existing:
        existing.is_active = True
        existing.role_in_project = data.get("role_in_project", existing.role_in_project)
        if "capabilities_override" in data:
            existing.capabilities_override = data["capabilities_override"]
        if "max_concurrent_tasks" in data:
            existing.max_concurrent_tasks = data["max_concurrent_tasks"]
        if "expires_at" in data:
            existing.expires_at = data["expires_at"]
        db.session.commit()
        return ApiResponse.success(existing.to_dict(), "Cross-project authorization updated").to_response()

    auth = CrossProjectAgent.create(
        agent_id=agent_id,
        project_id=project_id,
        authorized_by=user.id,
        role_in_project=data.get("role_in_project", "contributor"),
        capabilities_override=data.get("capabilities_override"),
        max_concurrent_tasks=data.get("max_concurrent_tasks", 3),
        expires_at=data.get("expires_at"),
    )
    db.session.commit()

    notify_sse("cross_project_authorized", {
        "agent_id": agent_id,
        "project_id": project_id,
        "role_in_project": auth.role_in_project,
    })
    return ApiResponse.success(auth.to_dict(), "Agent authorized for cross-project access").to_response()


@agents_bp.route("/cross-project/revoke", methods=["POST"])
@unified_auth_required
def revoke_cross_project_agent():
    """Revoke an Agent's cross-project authorization."""
    user = get_current_user()
    data = validate_json_request()

    agent_id = data.get("agent_id")
    project_id = data.get("project_id")
    if not agent_id or not project_id:
        return ApiResponse.error("agent_id and project_id are required").to_response()

    auth = CrossProjectAgent.query.filter_by(
        agent_id=agent_id, project_id=project_id
    ).first()
    if not auth:
        return ApiResponse.not_found("Cross-project authorization not found").to_response()

    # Verify user is the agent owner or project admin
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        user_role = ProjectMember.get_role(project_id, user.id)
        if user_role not in (ProjectRole.OWNER, ProjectRole.ADMIN):
            return ApiResponse.error("Not authorized to revoke this access").to_response()

    auth.is_active = False
    db.session.commit()
    return ApiResponse.success(None, "Cross-project authorization revoked").to_response()


@agents_bp.route("/<int:agent_id>/cross-project", methods=["GET"])
@unified_auth_required
def list_agent_cross_projects(agent_id):
    """List all projects an Agent is authorized to work in."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    authorizations = CrossProjectAgent.get_active_for_agent(agent_id)
    return ApiResponse.success(
        [a.to_dict() for a in authorizations],
        f"Agent authorized in {len(authorizations)} projects",
    ).to_response()


@agents_bp.route("/projects/<int:project_id>/external-agents", methods=["GET"])
@unified_auth_required
def list_project_external_agents(project_id):
    """List all external Agents authorized to work in this project."""
    user = get_current_user()
    project = Project.query.get(project_id)
    if not project:
        return ApiResponse.not_found("Project not found").to_response()

    # Verify user has access to the project
    user_role = ProjectMember.get_role(project_id, user.id)
    if not user_role and project.owner_id != user.id:
        return ApiResponse.error("Access denied").to_response()

    authorizations = CrossProjectAgent.get_active_for_project(project_id)
    return paginate_query(
        CrossProjectAgent.query.filter(
            CrossProjectAgent.project_id == project_id,
            CrossProjectAgent.is_active == True,
        ).order_by(CrossProjectAgent.created_at.desc()),
        "agents",
    )


@agents_bp.route("/cross-project/discover-agents", methods=["GET"])
@unified_auth_required
def discover_cross_project_agents():
    """Discover agents across all projects the user has access to.

    Returns agents that could be assigned to tasks, including external
    agents authorized for the user's projects.
    """
    user = get_current_user()
    args = get_request_args()

    # Get all projects the user has access to
    user_projects = ProjectMember.query.filter_by(user_id=user.id).with_entities(
        ProjectMember.project_id
    ).all()
    owned_projects = Project.query.filter_by(owner_id=user.id).with_entities(
        Project.id
    ).all()
    project_ids = list(set(
        [p.project_id for p in user_projects] + [p.id for p in owned_projects]
    ))

    if not project_ids:
        return ApiResponse.success([], "No projects found").to_response()

    # Find cross-project agents for these projects
    query = CrossProjectAgent.query.filter(
        CrossProjectAgent.project_id.in_(project_ids),
        CrossProjectAgent.is_active == True,
    )

    # Optional capability filter
    capability = args.get("capability")
    if capability:
        query = query.join(Agent, CrossProjectAgent.agent_id == Agent.id).filter(
            Agent.capabilities.contains([capability])
        )

    authorizations = query.order_by(CrossProjectAgent.created_at.desc()).all()

    # Deduplicate by agent_id
    seen = set()
    result = []
    for auth in authorizations:
        if auth.agent_id not in seen:
            seen.add(auth.agent_id)
            result.append(auth.to_dict())

    return ApiResponse.success(result, f"Found {len(result)} cross-project agents").to_response()


@agents_bp.route("/cross-project/capable-agents", methods=["GET"])
@unified_auth_required
def find_capable_agents_cross_project():
    """Find agents across all accessible projects that have specific capabilities.

    Query params: capabilities (comma-separated), project_id (optional filter)
    """
    user = get_current_user()
    args = get_request_args()

    capabilities_str = args.get("capabilities", "")
    if not capabilities_str:
        return ApiResponse.error("capabilities parameter is required").to_response()

    capabilities = [c.strip() for c in capabilities_str.split(",") if c.strip()]
    if not capabilities:
        return ApiResponse.error("No valid capabilities provided").to_response()

    # Get accessible project IDs
    user_projects = ProjectMember.query.filter_by(user_id=user.id).with_entities(
        ProjectMember.project_id
    ).all()
    owned_projects = Project.query.filter_by(owner_id=user.id).with_entities(
        Project.id
    ).all()
    project_ids = list(set(
        [p.project_id for p in user_projects] + [p.id for p in owned_projects]
    ))

    # Filter by specific project if requested
    filter_project_id = args.get("project_id", type=int)
    if filter_project_id:
        if filter_project_id not in project_ids:
            return ApiResponse.error("No access to specified project").to_response()
        project_ids = [filter_project_id]

    # Find agents: own agents + cross-project authorized agents
    # 1. Own agents with matching capabilities
    own_agents = Agent.query.filter(
        Agent.owner_id == user.id,
        Agent.status == AgentStatus.ACTIVE,
    ).all()

    # 2. Cross-project authorized agents
    cross_auths = CrossProjectAgent.query.filter(
        CrossProjectAgent.project_id.in_(project_ids),
        CrossProjectAgent.is_active == True,
    ).all()
    cross_agent_ids = [a.agent_id for a in cross_auths]
    cross_agents = Agent.query.filter(
        Agent.id.in_(cross_agent_ids),
        Agent.status == AgentStatus.ACTIVE,
    ).all() if cross_agent_ids else []

    # Combine and deduplicate
    all_agents = {}
    for a in own_agents:
        all_agents[a.id] = {"agent": a, "source": "own", "projects": project_ids}
    for auth in cross_auths:
        agent = next((a for a in cross_agents if a.id == auth.agent_id), None)
        if agent and agent.id not in all_agents:
            all_agents[agent.id] = {
                "agent": agent,
                "source": "cross_project",
                "projects": [auth.project_id],
                "role_in_project": auth.role_in_project,
            }

    # Score each agent for the requested capabilities
    results = []
    for agent_id, info in all_agents.items():
        agent = info["agent"]
        agent_caps = normalize_match_terms(agent.capabilities or [])
        req_caps = normalize_match_terms(capabilities)
        matched = agent_caps.intersection(req_caps)

        if matched:
            results.append({
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_kind": agent.kind.value if agent.kind else None,
                "matched_capabilities": sorted(matched),
                "match_score": len(matched) * 10,
                "source": info["source"],
                "available_projects": info["projects"],
                "role_in_project": info.get("role_in_project"),
                "collaboration_role": agent.collaboration_role or "standalone",
            })

    # Sort by match score
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return ApiResponse.success(results, f"Found {len(results)} capable agents").to_response()


# ---------------------------------------------------------------------------
# Agent Experience Decay & Validation endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/<int:agent_id>/experiences/decay", methods=["POST"])
@unified_auth_required
def apply_experience_decay(agent_id):
    """Apply time-based confidence decay to an Agent's experiences.

    Query params:
      days_threshold – minimum age in days before decay applies (default 30)
      decay_rate – confidence reduction factor per cycle (default 0.02)
    """
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    data = validate_json_request() or {}
    days_threshold = data.get("days_threshold", 30)
    decay_rate = data.get("decay_rate", 0.02)

    decayed = AgentExperience.apply_decay(
        agent_id=agent_id,
        days_threshold=days_threshold,
        decay_rate=decay_rate,
    )
    db.session.commit()

    return ApiResponse.success(
        {"decayed_count": decayed},
        f"Applied decay to {decayed} experiences",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/<int:experience_id>/validate", methods=["POST"])
@unified_auth_required
def validate_experience(agent_id, experience_id):
    """Cross-validate an experience by another Agent.

    The validator agent confirms or refutes the experience's accuracy,
    affecting its confidence score.
    """
    user = get_current_user()
    # Verify the validator agent belongs to the user
    validator = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not validator:
        return ApiResponse.not_found("Validator agent not found").to_response()

    data = validate_json_request()
    is_accurate = data.get("is_accurate", True)
    if "is_accurate" not in data:
        return ApiResponse.error("is_accurate is required (true/false)").to_response()

    result = AgentExperience.cross_validate(
        experience_id=experience_id,
        validator_agent_id=agent_id,
        is_accurate=is_accurate,
    )
    if not result:
        return ApiResponse.not_found("Experience not found or already invalid").to_response()

    db.session.commit()
    action = "验证通过" if is_accurate else "已反驳"
    return ApiResponse.success(
        result.to_dict(),
        f"经验已{action}，新置信度: {result.confidence}",
    ).to_response()


@agents_bp.route("/<int:agent_id>/experiences/validation-stats", methods=["GET"])
@unified_auth_required
def get_experience_validation_stats(agent_id):
    """Get validation statistics for an Agent's experiences."""
    user = get_current_user()
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    stats = AgentExperience.get_validation_stats(agent_id)
    return ApiResponse.success(stats).to_response()


@agents_bp.route("/maintenance/decay-all-experiences", methods=["POST"])
@unified_auth_required
def decay_all_experiences():
    """System maintenance: apply decay to all agents' experiences.

    Typically called by a scheduled job or admin action.
    """
    user = get_current_user()
    data = validate_json_request() or {}
    days_threshold = data.get("days_threshold", 30)
    decay_rate = data.get("decay_rate", 0.02)

    decayed = AgentExperience.apply_decay(
        agent_id=None,  # All agents
        days_threshold=days_threshold,
        decay_rate=decay_rate,
    )
    db.session.commit()

    return ApiResponse.success(
        {"decayed_count": decayed},
        f"Applied decay to {decayed} experiences across all agents",
    ).to_response()


# ---------------------------------------------------------------------------
# Agent Adaptive Capabilities endpoints
# ---------------------------------------------------------------------------

@agents_bp.route("/experiences/decay-alerts", methods=["GET"])
@unified_auth_required
def experiences_decay_alerts():
    """Flag Agents whose experience-base confidence is declining.

    Splits each Agent's valid experiences into two halves by created_at
    (older vs newer) within the window and compares average confidence.
    Agents whose newer-half average is meaningfully below the older-half
    average are returned as decay alerts with a recommended action, so
    owners can re-train or review recent low-quality experiences.
    """
    user = get_current_user()
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
        min_drop = max(0.02, min(0.5, float(request.args.get("min_drop", 0.1))))
        limit = max(1, min(30, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days, min_drop, limit = 30, 0.1, 10

    from models.agent import AgentExperience
    since = datetime.utcnow() - timedelta(days=days)
    midpoint = datetime.utcnow() - timedelta(days=days / 2)

    rows = (
        AgentExperience.query
        .join(Agent, AgentExperience.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            AgentExperience.created_at >= since,
            AgentExperience.is_valid.is_(True),
            AgentExperience.confidence.isnot(None),
        )
        .with_entities(
            AgentExperience.agent_id,
            Agent.name,
            AgentExperience.confidence,
            AgentExperience.created_at,
        )
        .all()
    )

    buckets = {}
    for aid, aname, conf, created in rows:
        if conf is None or created is None:
            continue
        info = buckets.setdefault(aid, {"name": aname or f"Agent#{aid}", "older": [], "newer": []})
        if created < midpoint:
            info["older"].append(conf)
        else:
            info["newer"].append(conf)

    def _avg(xs):
        return sum(xs) / len(xs) if xs else None

    alerts = []
    for aid, info in buckets.items():
        older_avg = _avg(info["older"])
        newer_avg = _avg(info["newer"])
        if older_avg is None or newer_avg is None:
            continue
        drop = older_avg - newer_avg
        if drop < min_drop:
            continue
        alerts.append({
            "agent_id": aid,
            "agent_name": info["name"],
            "older_avg_confidence": round(older_avg, 3),
            "newer_avg_confidence": round(newer_avg, 3),
            "drop": round(drop, 3),
            "older_count": len(info["older"]),
            "newer_count": len(info["newer"]),
            "current_confidence": round(newer_avg, 3),
            "recommendation": "review_recent_experiences" if drop >= 0.2 else "monitor",
        })

    alerts.sort(key=lambda a: a["drop"], reverse=True)
    return ApiResponse.success({
        "alerts": alerts[:limit],
        "total_alerts": len(alerts),
        "days": days,
        "min_drop": min_drop,
    }).to_response()

