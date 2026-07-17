"""
Security event endpoints — unified event log, export, daily trend, by-agent.
"""

import csv
import io
from datetime import datetime

from flask import make_response, request
from sqlalchemy import or_

from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AuditLog,
    Project,
    AgentConflict,
    AgentSandbox,
    SandboxViolation,
)


# ── Internal helper ──────────────────────────────────────────────────

def _collect_security_events(user, args):
    """Collect normalized security events across sandbox violations, agent
    conflicts, and security-relevant audit entries. Returns (events, error_response).

    Shared by list_security_events and the CSV export endpoint so the two stay
    consistent. Filters are read from `args` (a MultiDict-like): agent_id,
    workflow_run_id, event_type, severity, since, until, search.
    """
    agent_filter = args.get("agent_id", type=int)
    run_filter = args.get("workflow_run_id", type=int)
    event_type_filter = args.get("event_type")
    severity_filter = args.get("severity")
    search = (args.get("search") or "").strip().lower()
    since_str = args.get("since")
    until_str = args.get("until")
    since = None
    until = None
    if since_str:
        try:
            since = datetime.fromisoformat(since_str)
        except (ValueError, TypeError):
            return None, ApiResponse.error("Invalid 'since' datetime (use ISO 8601)", 400).to_response()
    if until_str:
        try:
            until = datetime.fromisoformat(until_str)
        except (ValueError, TypeError):
            return None, ApiResponse.error("Invalid 'until' datetime (use ISO 8601)", 400).to_response()

    events = []

    # 1. Sandbox violations (scoped to this owner via sandbox ownership)
    vq = SandboxViolation.query.join(
        AgentSandbox, SandboxViolation.sandbox_id == AgentSandbox.id
    ).filter(AgentSandbox.owner_id == user.id)
    if agent_filter:
        vq = vq.filter(SandboxViolation.agent_id == agent_filter)
    if severity_filter:
        # Only CRITICAL severity maps to sandbox violations
        if severity_filter == "CRITICAL":
            pass
        else:
            vq = vq.filter(False)
    if since:
        vq = vq.filter(SandboxViolation.blocked_at >= since)
    if until:
        vq = vq.filter(SandboxViolation.blocked_at <= until)
    if event_type_filter and event_type_filter != "sandbox_violation":
        vq = vq.filter(False)
    for v in vq.order_by(SandboxViolation.blocked_at.desc()).limit(200).all():
        events.append({
            "event_type": "sandbox_violation",
            "occurred_at": v.blocked_at.isoformat() if v.blocked_at else None,
            "severity": "CRITICAL",
            "agent_id": v.agent_id,
            "title": f"Sandbox violation: {v.violation_type.value if v.violation_type else 'unknown'}",
            "detail": v.detail or v.attempted_action or "",
            "source": "sandbox_violation",
            "source_id": v.id,
            "workflow_run_id": None,
            "extra": {"violation_type": v.violation_type.value if v.violation_type else None,
                      "execution_id": v.execution_id, "sandbox_id": v.sandbox_id},
        })

    # 2. Agent conflicts
    cq = AgentConflict.query.filter_by(owner_id=user.id)
    if agent_filter:
        # agent_ids is a JSON list; filter in Python after fetch for portability
        pass
    if run_filter:
        cq = cq.filter(AgentConflict.workflow_run_id == run_filter)
    if severity_filter:
        cq = cq.filter(AgentConflict.severity == severity_filter)
    if since:
        cq = cq.filter(AgentConflict.created_at >= since)
    if until:
        cq = cq.filter(AgentConflict.created_at <= until)
    for c in cq.order_by(AgentConflict.created_at.desc()).limit(200).all():
        if agent_filter and (not c.agent_ids or agent_filter not in (c.agent_ids or [])):
            continue
        if event_type_filter and event_type_filter != "conflict":
            continue
        events.append({
            "event_type": "conflict",
            "occurred_at": c.created_at.isoformat() if c.created_at else None,
            "severity": c.severity.value if c.severity else "INFO",
            "agent_id": (c.agent_ids or [None])[0] if c.agent_ids else None,
            "title": c.title or c.conflict_type.value if c.conflict_type else "Conflict",
            "detail": c.description or "",
            "source": "agent_conflict",
            "source_id": c.id,
            "workflow_run_id": c.workflow_run_id,
            "extra": {"conflict_type": c.conflict_type.value if c.conflict_type else None,
                      "status": c.status.value if c.status else None,
                      "suggested_strategy": c.suggested_strategy.value if c.suggested_strategy else None},
        })

    # 3. Security-relevant audit log entries
    SECURITY_AUDIT_PREFIXES = ("sandbox.", "conflict.", "reputation.", "workflow_step_overridden", "workflow_step_override_cleared")
    aq = AuditLog.query.filter(
        or_(
            AuditLog.actor_user_id == user.id,
            AuditLog.project_id.in_([p.id for p in Project.query.filter_by(owner_id=user.id).all()]),
        )
    )
    # Prefix filtering (SQLAlchemy .op or startswith depending on dialect; use Python-side for portability)
    audit_rows = aq.order_by(AuditLog.created_at.desc()).limit(500).all()
    for a in audit_rows:
        if not a.action or not any(a.action.startswith(p) for p in SECURITY_AUDIT_PREFIXES):
            continue
        if event_type_filter and event_type_filter != "audit":
            continue
        if since and a.created_at and a.created_at < since:
            continue
        if until and a.created_at and a.created_at > until:
            continue
        events.append({
            "event_type": "audit",
            "occurred_at": a.created_at.isoformat() if a.created_at else None,
            "severity": "CRITICAL" if "revoke" in (a.action or "").lower() or "violation" in (a.action or "").lower()
                        else ("WARNING" if "auto_resolve" in (a.action or "") or "override" in (a.action or "") else "INFO"),
            "agent_id": a.actor_agent_id,
            "title": a.action or "audit",
            "detail": (a.detail or "")[:500] if isinstance(a.detail, str) else str(a.detail or "")[:500],
            "source": "audit_log",
            "source_id": a.id,
            "workflow_run_id": None,
            "extra": {"resource_type": a.resource_type, "resource_id": a.resource_id,
                      "actor_type": a.actor_type, "actor_user_id": a.actor_user_id},
        })

    # Merge and sort by occurred_at desc
    events.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)

    # Keyword search across title/detail (case-insensitive, Python-side since
    # the feed is merged from heterogeneous sources)
    if search:
        events = [
            e for e in events
            if search in (e.get("title") or "").lower()
            or search in (e.get("detail") or "").lower()
        ]
    return events, None


# ── Routes ───────────────────────────────────────────────────────────

@agents_bp.route("/security/events", methods=["GET"])
@unified_auth_required
def list_security_events():
    """Unified security event log: aggregates sandbox violations, agent
    conflicts, and security-relevant audit entries (sandbox./conflict./
    reputation./workflow_step_overridden) into a single time-ordered feed.

    Each event is normalized to:
      {event_type, occurred_at, severity, agent_id, title, detail,
       source, source_id, workflow_run_id}

    Filters: agent_id, workflow_run_id, event_type, severity, since (ISO),
    until (ISO), search (keyword on title/detail), plus standard pagination.
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    # Pagination (in-memory since merged from multiple sources)
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 20, type=int) or 20
    per_page = max(1, min(100, per_page))
    total = len(events)
    start = (page - 1) * per_page
    page_items = events[start:start + per_page]
    pagination = {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        "has_prev": page > 1,
        "has_next": (start + per_page) < total,
    }
    return ApiResponse.success(
        data={"items": page_items, "pagination": pagination},
        message="Security events",
    ).to_response()


@agents_bp.route("/security/events/export", methods=["GET"])
@unified_auth_required
def export_security_events():
    """Export the unified security event log as CSV or JSON.

    Accepts the same filters as GET /security/events (agent_id,
    workflow_run_id, event_type, severity, since, until, search) plus
    `format` (csv | json, default csv). Up to 1000 rows.
    CSV returns a text/csv attachment; JSON returns a JSON array attachment
    (each item is the full normalized event object, including `extra`).
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    # Cap export volume
    export_rows = events[:1000]
    fmt = (request.args.get("format") or "csv").lower()

    if fmt == "json":
        # Return the full normalized event objects for programmatic consumers.
        payload = io.StringIO()
        import json as _json
        _json.dump(export_rows, payload, ensure_ascii=False, default=str)
        resp = make_response(payload.getvalue())
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            'attachment; filename="security_events.json"'
        )
        return resp

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "occurred_at", "event_type", "severity", "agent_id",
        "workflow_run_id", "source", "source_id", "title", "detail",
    ])
    for e in export_rows:
        detail = e.get("detail") or ""
        if not isinstance(detail, str):
            detail = str(detail)
        writer.writerow([
            e.get("occurred_at") or "",
            e.get("event_type") or "",
            e.get("severity") or "",
            e.get("agent_id") if e.get("agent_id") is not None else "",
            e.get("workflow_run_id") if e.get("workflow_run_id") is not None else "",
            e.get("source") or "",
            e.get("source_id") if e.get("source_id") is not None else "",
            (e.get("title") or "").replace("\n", " ").replace("\r", " "),
            detail.replace("\n", " ").replace("\r", " "),
        ])
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        'attachment; filename="security_events.csv"'
    )
    return resp


@agents_bp.route("/security/events/daily-trend", methods=["GET"])
@unified_auth_required
def security_events_daily_trend():
    """Daily aggregation of security events for trend visualization.

    Reuses _collect_security_events with the same filters (agent_id,
    workflow_run_id, event_type, severity, since, until, search), then
    buckets events by the date portion of occurred_at. Returns:
      {
        days: [{date, sandbox_violation, conflict, audit, total}],
        totals: {sandbox_violation, conflict, audit, total}
      }
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    buckets = {}  # date -> {sandbox_violation, conflict, audit}
    for e in events[:1000]:
        ts = e.get("occurred_at") or ""
        # occurred_at is ISO; date is the first 10 chars (YYYY-MM-DD)
        day = ts[:10] if len(ts) >= 10 else None
        if not day:
            continue
        etype = e.get("event_type") or "audit"
        b = buckets.setdefault(day, {"sandbox_violation": 0, "conflict": 0, "audit": 0})
        if etype in b:
            b[etype] += 1
        else:
            b["audit"] += 1

    # Sort by date ascending
    sorted_days = sorted(buckets.items(), key=lambda kv: kv[0])
    days = [{"date": d, **counts, "total": sum(counts.values())} for d, counts in sorted_days]
    totals = {
        "sandbox_violation": sum(d["sandbox_violation"] for d in days),
        "conflict": sum(d["conflict"] for d in days),
        "audit": sum(d["audit"] for d in days),
        "total": sum(d["total"] for d in days),
    }
    return ApiResponse.success(
        data={"days": days, "totals": totals},
        message="Security events daily trend",
    ).to_response()


@agents_bp.route("/security/events/by-agent", methods=["GET"])
@unified_auth_required
def security_events_by_agent():
    """Per-agent aggregation of security events for ranking.

    Reuses _collect_security_events with the same filters. Buckets events
    by agent_id (events without an agent_id fall under agent_id=null).
    Returns agents: [{agent_id, name, total, sandbox_violation, conflict,
    audit, critical, warning, info}] sorted by total desc (top 50).
    """
    user = get_current_user()
    events, err = _collect_security_events(user, request.args)
    if err is not None:
        return err

    buckets = {}  # agent_id -> counts
    for e in events[:1000]:
        aid = e.get("agent_id")
        key = aid if aid is not None else 0  # 0 = "no agent"
        b = buckets.setdefault(key, {
            "agent_id": aid,
            "total": 0,
            "sandbox_violation": 0, "conflict": 0, "audit": 0,
            "CRITICAL": 0, "WARNING": 0, "INFO": 0,
        })
        b["total"] += 1
        etype = e.get("event_type") or "audit"
        if etype in ("sandbox_violation", "conflict", "audit"):
            b[etype] += 1
        else:
            b["audit"] += 1
        sev = e.get("severity") or "INFO"
        if sev in ("CRITICAL", "WARNING", "INFO"):
            b[sev] += 1
        else:
            b["INFO"] += 1

    # Resolve agent names (best-effort, single query for known ids)
    known_ids = [k for k in buckets.keys() if k != 0]
    name_map = {}
    if known_ids:
        for a in Agent.query.filter(Agent.id.in_(known_ids)).all():
            name_map[a.id] = a.name

    ranked = sorted(buckets.values(), key=lambda b: b["total"], reverse=True)[:50]
    for b in ranked:
        b["name"] = name_map.get(b["agent_id"]) if b["agent_id"] else "(无 Agent)"

    return ApiResponse.success(
        data={"agents": ranked},
        message="Security events by agent",
    ).to_response()
