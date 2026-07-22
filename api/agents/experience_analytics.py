"""
Experience analytics routes.

Extracted from experiences.py to separate analytics from CRUD operations.
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
