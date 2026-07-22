"""
Capability and skill analytics endpoints: capability gap analysis,
skill matching, specialization evolution, and capability supply-demand.

Task allocation fairness and workload forecast remain in analytics.py.
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
    CrossProjectAgent,
    Task,
    TaskAssignment,
    TaskStatus,
    Project,
)


# ---------------------------------------------------------------------------
# Capability Gap Analysis
# ---------------------------------------------------------------------------

@agents_bp.route("/capability-gap-analysis", methods=["GET"])
@unified_auth_required
def agent_capability_gap_analysis():
    """Analyze capability gaps for each agent.

    Compares each agent's declared capabilities against their actual experience
    domains. Identifies:
    - Gaps: domains with successful experiences but NOT in declared capabilities
    - Overclaims: declared capabilities with NO supporting successful experiences
    - Coverage score: ratio of experience-backed capabilities to total declared

    Returns per-agent gap analysis with recommendations.
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
        min_confidence = max(0.0, min(1.0, float(request.args.get("min_confidence", 0.5))))
    except (TypeError, ValueError):
        limit = 10
        min_confidence = 0.5

    # Get all user's agents
    agents = Agent.query.filter_by(owner_id=user.id).all()

    results = []
    for agent in agents:
        caps = set(agent.capabilities or []) if agent.capabilities else set()
        if not caps:
            continue

        # Get successful experience domains for this agent
        success_domains = (
            db.session.query(
                AgentExperience.domain,
                func.count(AgentExperience.id),
                func.avg(AgentExperience.confidence),
            )
            .filter(
                AgentExperience.agent_id == agent.id,
                AgentExperience.experience_type == "success_pattern",
                AgentExperience.confidence >= min_confidence,
                AgentExperience.is_valid == True,
                AgentExperience.domain.isnot(None),
            )
            .group_by(AgentExperience.domain)
            .all()
        )

        # Also check failure domains
        failure_domains = (
            db.session.query(
                AgentExperience.domain,
                func.count(AgentExperience.id),
            )
            .filter(
                AgentExperience.agent_id == agent.id,
                AgentExperience.experience_type == "failure_pattern",
                AgentExperience.is_valid == True,
                AgentExperience.domain.isnot(None),
            )
            .group_by(AgentExperience.domain)
            .all()
        )

        exp_domain_set = {d[0] for d in success_domains if d[0]}
        fail_domain_map = {d[0]: d[1] for d in failure_domains if d[0]}

        # Normalize: lowercase, strip for comparison
        def normalize(s):
            return s.strip().lower() if s else ""

        norm_caps = {normalize(c): c for c in caps}
        norm_exp = {normalize(d) for d in exp_domain_set}

        # Gaps: experience domains not in capabilities
        gap_domains = norm_exp - set(norm_caps.keys())
        gaps = []
        for d in success_domains:
            if normalize(d[0]) in gap_domains:
                gaps.append({
                    "domain": d[0],
                    "success_count": d[1],
                    "avg_confidence": round(float(d[2]), 2) if d[2] else 0.0,
                    "failure_count": fail_domain_map.get(d[0], 0),
                })

        # Overclaims: capabilities with no successful experience
        overclaim_domains = set(norm_caps.keys()) - norm_exp
        overclaims = []
        for nc, oc in norm_caps.items():
            if nc in overclaim_domains:
                fail_count = sum(v for k, v in fail_domain_map.items() if normalize(k) == nc)
                overclaims.append({
                    "capability": oc,
                    "failure_count": fail_count,
                    "risk": "high" if fail_count > 3 else ("medium" if fail_count > 0 else "low"),
                })

        # Coverage score
        backed_caps = set(norm_caps.keys()) & norm_exp
        coverage_score = round(len(backed_caps) / len(norm_caps) * 100, 1) if norm_caps else 0.0

        # Experience strength per matched capability
        matched = []
        for nc, oc in norm_caps.items():
            if nc in norm_exp:
                for d in success_domains:
                    if normalize(d[0]) == nc:
                        matched.append({
                            "capability": oc,
                            "domain": d[0],
                            "success_count": d[1],
                            "avg_confidence": round(float(d[2]), 2) if d[2] else 0.0,
                        })
                        break

        if gaps or overclaims:
            results.append({
                "agent_id": agent.id,
                "agent_name": agent.name or f"Agent#{agent.id}",
                "total_capabilities": len(caps),
                "coverage_score": coverage_score,
                "gaps": gaps,
                "overclaims": overclaims,
                "matched": matched,
            })

    # Sort by coverage score ascending (most gaps first), limit
    results.sort(key=lambda r: r["coverage_score"])
    return ApiResponse.success({"agents": results[:limit]}).to_response()


# ---------------------------------------------------------------------------
# Skill Matching
# ---------------------------------------------------------------------------

@agents_bp.route("/skill-matching", methods=["GET"])
@unified_auth_required
def agent_skill_matching():
    """Agent skill matching recommendation.

    For unassigned in-progress tasks, match task title/description keywords
    to agent capabilities and experience domains. Returns per-task
    recommended agents with match scores.

    Query params:
    - limit: max tasks returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    from models.agent import Task, TaskAssignment
    # Find unassigned in-progress tasks
    unassigned_tasks = (
        Task.query
        .filter(
            Task.owner_id == user.id,
            Task.status == "in_progress",
            ~Task.id.in_(
                TaskAssignment.query
                .filter(TaskAssignment.status == "active")
                .with_entities(TaskAssignment.task_id)
            ),
        )
        .order_by(Task.created_at.desc())
        .limit(limit * 2)
        .all()
    )

    # Get all active agents with capabilities
    agents = Agent.query.filter(Agent.owner_id == user.id, Agent.status == "active").all()
    agent_caps = {}
    for a in agents:
        caps = set()
        if a.capabilities:
            for c in (a.capabilities if isinstance(a.capabilities, list) else []):
                caps.add(c.lower())
        # Add experience domains
        if hasattr(a, 'experiences') and a.experiences:
            for exp in a.experiences:
                if hasattr(exp, 'domain') and exp.domain:
                    caps.add(exp.domain.lower())
        agent_caps[a.id] = {"name": a.name or f"Agent#{a.id}", "caps": caps}

    results = []
    for task in unassigned_tasks:
        # Extract keywords from task title and description
        text = (task.title or "") + " " + (task.description or "")
        keywords = set(w.lower() for w in text.split() if len(w) > 2)
        if not keywords:
            continue

        recommendations = []
        for aid, info in agent_caps.items():
            if not info["caps"]:
                continue
            matched = keywords & info["caps"]
            if matched:
                score = round(len(matched) / len(keywords) * 100, 1)
                recommendations.append({
                    "agent_id": aid,
                    "agent_name": info["name"],
                    "match_score": score,
                    "matched_capabilities": sorted(matched),
                })

        recommendations.sort(key=lambda r: r["match_score"], reverse=True)
        if recommendations:
            results.append({
                "task_id": task.id,
                "task_title": task.title or f"Task#{task.id}",
                "recommendations": recommendations[:3],
            })

    return ApiResponse.success({"tasks": results[:limit]}).to_response()


# ---------------------------------------------------------------------------
# Specialization Evolution
# ---------------------------------------------------------------------------

@agents_bp.route("/specialization-evolution", methods=["GET"])
@unified_auth_required
def agent_specialization_evolution():
    """Track how each Agent's domain coverage evolves over time.

    Buckets AgentExperience by (agent, week) and counts distinct domains
    per week. Returns per-agent weekly domain coverage series and the
    list of domains learned, revealing specialization vs generalization.
    """
    user = get_current_user()
    try:
        weeks = max(2, min(26, int(request.args.get("weeks", 12))))
        limit = max(1, min(15, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        weeks, limit = 12, 8

    from models.agent import AgentExperience
    days = weeks * 7
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        AgentExperience.query
        .join(Agent, AgentExperience.agent_id == Agent.id)
        .filter(
            Agent.owner_id == user.id,
            AgentExperience.created_at >= since,
            AgentExperience.domain.isnot(None),
        )
        .with_entities(
            AgentExperience.agent_id,
            Agent.name,
            AgentExperience.domain,
            AgentExperience.created_at,
        )
        .all()
    )

    now = datetime.utcnow()
    agent_weeks = {}
    for aid, aname, domain, created in rows:
        if not domain or not created:
            continue
        delta_days = (now - created).days
        week_idx = weeks - 1 - (delta_days // 7)
        if week_idx < 0 or week_idx >= weeks:
            continue
        info = agent_weeks.setdefault(aid, {"name": aname or f"Agent#{aid}", "weeks": {}})
        info["weeks"].setdefault(week_idx, set()).add(domain)

    results = []
    for aid, info in agent_weeks.items():
        series = [len(info["weeks"].get(w, set())) for w in range(weeks)]
        all_domains = set()
        for ds in info["weeks"].values():
            all_domains.update(ds)
        if sum(series) == 0:
            continue
        peak = max(series)
        peak_week = series.index(peak) if peak > 0 else 0
        results.append({
            "agent_id": aid,
            "agent_name": info["name"],
            "series": series,
            "peak_domains": peak,
            "peak_week_idx": peak_week,
            "total_domains": len(all_domains),
            "domains": sorted(all_domains)[:10],
        })

    results.sort(key=lambda r: r["total_domains"], reverse=True)
    week_labels = [f"W-{weeks - 1 - w}" for w in range(weeks)]
    return ApiResponse.success({
        "agents": results[:limit],
        "weeks": weeks,
        "week_labels": week_labels,
    }).to_response()


# ---------------------------------------------------------------------------
# Capability Supply-Demand
# ---------------------------------------------------------------------------

@agents_bp.route("/capability-supply-demand", methods=["GET"])
@unified_auth_required
def capability_supply_demand():
    """Analyze supply vs demand for each capability.

    Supply = number of the user's Agents declaring a capability.
    Demand = number of active (non-terminal) tasks requiring that
    capability, across projects owned by the user. Identifies
    bottleneck capabilities (demand exceeds supply) and surplus
    capabilities (supply with no demand) so owners can rebalance the
    fleet's declared skills against actual task requirements.
    """
    import json
    user = get_current_user()
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    from models.task import Task, TaskStatus
    from models.project import Project

    def _as_list(raw):
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return []
        if isinstance(raw, list):
            return [str(c) for c in raw if c]
        return []

    # supply: capabilities declared by the user's agents
    agents = Agent.query.filter_by(owner_id=user.id).with_entities(Agent.capabilities).all()
    supply = {}
    agent_total = 0
    for (caps,) in agents:
        unique = set(_as_list(caps))
        if not unique:
            continue
        agent_total += 1
        for c in unique:
            supply[c] = supply.get(c, 0) + 1

    # demand: capabilities required by active tasks in the user's projects
    tasks = (
        Task.query
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.owner_id == user.id,
            Task.status.in_([
                TaskStatus.TODO,
                TaskStatus.IN_PROGRESS,
                TaskStatus.REVIEW,
                TaskStatus.BLOCKED,
            ]),
        )
        .with_entities(Task.required_capabilities)
        .all()
    )
    demand = {}
    active_task_total = 0
    for (req,) in tasks:
        unique = set(_as_list(req))
        if not unique:
            continue
        active_task_total += 1
        for c in unique:
            demand[c] = demand.get(c, 0) + 1

    all_caps = sorted(set(supply) | set(demand))
    items = []
    for c in all_caps:
        s = supply.get(c, 0)
        d = demand.get(c, 0)
        if d > 0 and s == 0:
            status = "missing"
        elif d > s:
            status = "bottleneck"
        elif d == 0 and s > 0:
            status = "unused_supply"
        elif s > d:
            status = "surplus"
        else:
            status = "balanced"
        items.append({
            "capability": c,
            "supply": s,
            "demand": d,
            "gap": s - d,
            "ratio": round(d / s, 2) if s > 0 else None,
            "status": status,
        })

    items.sort(key=lambda x: (x["demand"], x["supply"]), reverse=True)
    bottleneck = [i for i in items if i["status"] in ("bottleneck", "missing")]
    return ApiResponse.success({
        "capabilities": items[:limit],
        "total_capabilities": len(items),
        "bottleneck_count": len(bottleneck),
        "agent_total": agent_total,
        "active_task_total": active_task_total,
    }).to_response()
