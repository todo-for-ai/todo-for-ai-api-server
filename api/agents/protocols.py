"""
Collaboration protocol CRUD, deliberation, resolution, and analytics endpoints.
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
    CollaborationProtocol,
    ProtocolMessage,
    ProtocolType,
    ProtocolStatus,
    AuditLog,
    get_request_args,
    paginate_query,
    parse_enum,
)

@agents_bp.route("/protocols", methods=["GET"])
@unified_auth_required
def list_protocols():
    """List collaboration protocols with optional filters."""
    user = get_current_user()
    query = CollaborationProtocol.query

    # Filter by project
    project_id = request.args.get("project_id", type=int)
    if project_id:
        query = query.filter_by(project_id=project_id)

    # Filter by status
    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status)

    # Filter by type
    protocol_type = request.args.get("protocol_type")
    if protocol_type:
        query = query.filter_by(protocol_type=protocol_type)

    # Filter by initiator
    initiator_id = request.args.get("initiator_agent_id", type=int)
    if initiator_id:
        query = query.filter_by(initiator_agent_id=initiator_id)

    # Only show protocols for agents owned by this user
    agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).all()]
    if agent_ids:
        query = query.filter(
            db.or_(
                CollaborationProtocol.initiator_agent_id.in_(agent_ids),
                CollaborationProtocol.project_id.in_(
                    [pm.project_id for pm in ProjectMember.query.filter_by(user_id=user.id).all()]
                ),
            )
        )

    query = query.order_by(CollaborationProtocol.created_at.desc())
    result = paginate_query(query, default_per_page=30)
    protocols = [p.to_dict() for p in result.items]
    return ApiResponse.success({
        "items": protocols,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
    }).to_response()


@agents_bp.route("/protocols", methods=["POST"])
@unified_auth_required
def create_protocol():
    """Create a new collaboration protocol (proposal, vote, consensus, auction, or handoff)."""
    user = get_current_user()
    data = validate_json_request()

    protocol_type = (data.get("protocol_type") or "").strip()
    if protocol_type not in [e.value for e in ProtocolType]:
        return ApiResponse.error(f"Invalid protocol_type. Must be one of: {', '.join(e.value for e in ProtocolType)}", 400).to_response()

    title = (data.get("title") or "").strip()
    if not title:
        return ApiResponse.error("title is required", 400).to_response()

    initiator_agent_id = data.get("initiator_agent_id")
    if not initiator_agent_id:
        return ApiResponse.error("initiator_agent_id is required", 400).to_response()

    # Verify the initiator agent belongs to this user
    agent = Agent.query.filter_by(id=initiator_agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.error("Initiator agent not found or not owned by you", 400).to_response()

    deadline = None
    if data.get("deadline"):
        try:
            deadline = datetime.fromisoformat(data["deadline"])
        except (ValueError, TypeError):
            return ApiResponse.error("Invalid deadline format (use ISO 8601)", 400).to_response()

    protocol = CollaborationProtocol.create(
        protocol_type=protocol_type,
        status=ProtocolStatus.OPEN.value,
        title=title,
        description=data.get("description", ""),
        initiator_agent_id=initiator_agent_id,
        channel_id=data.get("channel_id"),
        project_id=data.get("project_id"),
        task_id=data.get("task_id"),
        config=data.get("config", {}),
        deadline=deadline,
    )
    db.session.commit()

    # Notify channel members if channel is specified
    if protocol.channel_id:
        members = AgentChannelMember.query.filter_by(channel_id=protocol.channel_id).all()
        for member in members:
            if member.agent_id != initiator_agent_id:
                Notification.create(
                    agent_id=member.agent_id,
                    event_type="protocol_created",
                    title=f"新协议: {title}",
                    message=f"{agent.name} 发起了 {protocol_type} 协议: {title}",
                    payload={"protocol_id": protocol.id, "protocol_type": protocol_type},
                )
        db.session.commit()

    return ApiResponse.created(protocol.to_dict(), "Protocol created").to_response()


@agents_bp.route("/protocols/<int:protocol_id>", methods=["GET"])
@unified_auth_required
def get_protocol(protocol_id):
    """Get a protocol with its messages."""
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    return ApiResponse.success(protocol.to_dict(include_messages=True)).to_response()


@agents_bp.route("/protocols/<int:protocol_id>/respond", methods=["POST"])
@unified_auth_required
def respond_to_protocol(protocol_id):
    """Respond to a protocol (vote, bid, accept, reject, counter-proposal, comment)."""
    user = get_current_user()
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    if protocol.status not in (ProtocolStatus.OPEN.value, ProtocolStatus.VOTING.value):
        return ApiResponse.error(f"Protocol is {protocol.status}, cannot respond", 400).to_response()

    # Check deadline
    if protocol.deadline and datetime.utcnow() > protocol.deadline:
        protocol.status = ProtocolStatus.EXPIRED.value
        db.session.commit()
        return ApiResponse.error("Protocol has expired", 400).to_response()

    data = validate_json_request()
    agent_id = data.get("agent_id")
    if not agent_id:
        return ApiResponse.error("agent_id is required", 400).to_response()

    # Verify agent ownership
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.error("Agent not found or not owned by you", 400).to_response()

    message_type = (data.get("message_type") or "").strip()
    valid_types = ["vote", "bid", "accept", "reject", "comment", "counter_proposal"]
    if message_type not in valid_types:
        return ApiResponse.error(f"Invalid message_type. Must be one of: {', '.join(valid_types)}", 400).to_response()

    msg = ProtocolMessage.create(
        protocol_id=protocol_id,
        agent_id=agent_id,
        message_type=message_type,
        content=data.get("content", ""),
        payload=data.get("payload", {}),
    )

    # Auto-resolve logic based on protocol type
    _try_resolve_protocol(protocol)

    db.session.commit()
    return ApiResponse.created(msg.to_dict(), "Response recorded").to_response()


@agents_bp.route("/protocols/<int:protocol_id>/resolve", methods=["POST"])
@unified_auth_required
def resolve_protocol(protocol_id):
    """Manually resolve a protocol (force accept/reject/cancel)."""
    user = get_current_user()
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    data = validate_json_request()
    resolution = (data.get("resolution") or "").strip()
    if resolution not in ("accepted", "rejected", "cancelled"):
        return ApiResponse.error("resolution must be accepted, rejected, or cancelled", 400).to_response()

    protocol.status = resolution
    protocol.resolved_at = datetime.utcnow()
    protocol.result = data.get("result", {"manual_resolution": resolution})

    db.session.commit()
    return ApiResponse.success(protocol.to_dict(), f"Protocol {resolution}").to_response()


@agents_bp.route("/protocols/analytics", methods=["GET"])
@unified_auth_required
def protocol_analytics():
    """Analytics for collaboration protocols: usage, resolution rates, participation.

    Query params:
      days – look-back window (default 30)
    """
    user = get_current_user()
    args = get_request_args()
    days = args.get("days", 30, type=int)
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))

    # Get user's protocols (via initiator agent ownership)
    user_agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).all()]
    if not user_agent_ids:
        return ApiResponse.success({
            "total_protocols": 0,
            "by_type": {},
            "by_status": {},
            "resolution_rate": 0,
        }).to_response()

    protocols = CollaborationProtocol.query.filter(
        CollaborationProtocol.initiator_agent_id.in_(user_agent_ids),
        CollaborationProtocol.created_at >= since,
    ).all()

    by_type = {}
    by_status = {}
    resolved_count = 0
    total_messages = 0
    participation = {}  # agent_id -> message count

    for p in protocols:
        by_type[p.protocol_type] = by_type.get(p.protocol_type, 0) + 1
        by_status[p.status] = by_status.get(p.status, 0) + 1
        if p.status in ("accepted", "rejected"):
            resolved_count += 1

        # Count messages
        msgs = ProtocolMessage.query.filter_by(protocol_id=p.id).all()
        total_messages += len(msgs)
        for m in msgs:
            participation[m.agent_id] = participation.get(m.agent_id, 0) + 1

    # Top participants
    top_participants = sorted(participation.items(), key=lambda x: -x[1])[:10]
    top_participants_data = []
    for aid, count in top_participants:
        agent = Agent.query.get(aid)
        if agent:
            top_participants_data.append({
                "agent_id": aid,
                "agent_name": agent.name,
                "message_count": count,
            })

    resolution_rate = round(resolved_count / len(protocols) * 100, 1) if protocols else 0
    avg_messages = round(total_messages / len(protocols), 1) if protocols else 0

    return ApiResponse.success({
        "window_days": days,
        "total_protocols": len(protocols),
        "by_type": by_type,
        "by_status": by_status,
        "resolved_count": resolved_count,
        "resolution_rate": resolution_rate,
        "total_messages": total_messages,
        "avg_messages_per_protocol": avg_messages,
        "top_participants": top_participants_data,
    }).to_response()


@agents_bp.route("/protocols/<int:protocol_id>/deliberate", methods=["POST"])
@unified_auth_required
def add_deliberation_message(protocol_id):
    """Add a deliberation message (argument/evidence/comment) to a deliberation protocol.

    For DELIBERATION type protocols, this contributes to the required
    discussion before final voting can resolve.
    """
    user = get_current_user()
    protocol = CollaborationProtocol.query.get(protocol_id)
    if not protocol:
        return ApiResponse.not_found("Protocol not found").to_response()

    if protocol.protocol_type != ProtocolType.DELIBERATION.value:
        return ApiResponse.error("This endpoint is only for deliberation protocols").to_response()

    if protocol.status != "open":
        return ApiResponse.error("Protocol is no longer open for deliberation").to_response()

    data = validate_json_request()
    agent_id = data.get("agent_id")
    message_type = data.get("message_type", "comment")
    if message_type not in ("comment", "argument", "evidence"):
        return ApiResponse.error("message_type must be comment, argument, or evidence").to_response()

    # Verify agent ownership
    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    msg = ProtocolMessage.create(
        protocol_id=protocol_id,
        agent_id=agent_id,
        message_type=message_type,
        content=data.get("content", ""),
        payload=data.get("payload", {}),
    )
    db.session.commit()

    # Try to resolve after adding deliberation message
    _try_resolve_protocol(protocol)
    db.session.commit()

    notify_sse("protocol_deliberation", {
        "protocol_id": protocol_id,
        "agent_id": agent_id,
        "message_type": message_type,
    })
    return ApiResponse.success(msg.to_dict(), "Deliberation message added").to_response()


def _try_resolve_protocol(protocol):
    """Auto-resolve a protocol if conditions are met.

    - Proposal: accepted if any 'accept', rejected if any 'reject'
    - Vote: resolved when quorum reached (config.quorum or simple majority)
    - Consensus: accepted only if all participants accept
    - Auction: resolved at deadline or when no new bids
    - Handoff: accepted when target agent accepts
    """
    messages = ProtocolMessage.query.filter_by(protocol_id=protocol.id).all()
    config = protocol.config or {}

    if protocol.protocol_type == ProtocolType.PROPOSAL.value:
        accepts = [m for m in messages if m.message_type == "accept"]
        rejects = [m for m in messages if m.message_type == "reject"]
        if accepts:
            protocol.status = ProtocolStatus.ACCEPTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"accepted_by": [m.agent_id for m in accepts]}
        elif rejects:
            protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"rejected_by": [m.agent_id for m in rejects]}

    elif protocol.protocol_type == ProtocolType.VOTE.value:
        votes_for = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "for"]
        votes_against = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "against"]
        quorum = config.get("quorum", 2)
        if len(votes_for) + len(votes_against) >= quorum:
            if len(votes_for) > len(votes_against):
                protocol.status = ProtocolStatus.ACCEPTED.value
            else:
                protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"votes_for": len(votes_for), "votes_against": len(votes_against)}

    elif protocol.protocol_type == ProtocolType.CONSENSUS.value:
        # Get all participants (channel members or specified in config)
        participant_ids = config.get("participant_agent_ids", [])
        if not participant_ids and protocol.channel_id:
            participant_ids = [m.agent_id for m in AgentChannelMember.query.filter_by(channel_id=protocol.channel_id).all()]
        if not participant_ids:
            return

        accepts = {m.agent_id for m in messages if m.message_type == "accept"}
        rejects = {m.agent_id for m in messages if m.message_type == "reject"}

        if rejects & set(participant_ids):
            protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"rejected_by": list(rejects & set(participant_ids))}
        elif accepts.issuperset(set(participant_ids)):
            protocol.status = ProtocolStatus.ACCEPTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"accepted_by": list(accepts)}

    elif protocol.protocol_type == ProtocolType.HANDOFF.value:
        accepts = [m for m in messages if m.message_type == "accept"]
        if accepts:
            protocol.status = ProtocolStatus.ACCEPTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {"accepted_by": accepts[0].agent_id}

    elif protocol.protocol_type == ProtocolType.WEIGHTED_VOTE.value:
        # Vote weighted by agent reputation score
        votes_for = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "for"]
        votes_against = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("choice") == "against"]
        quorum = config.get("quorum", 2)

        if len(votes_for) + len(votes_against) >= quorum:
            weighted_for = 0.0
            weighted_against = 0.0
            vote_breakdown = []
            for m in votes_for:
                rep = AgentReputation.query.filter_by(agent_id=m.agent_id).first()
                weight = rep.score if rep else 50.0
                weighted_for += weight
                vote_breakdown.append({"agent_id": m.agent_id, "choice": "for", "weight": weight})
            for m in votes_against:
                rep = AgentReputation.query.filter_by(agent_id=m.agent_id).first()
                weight = rep.score if rep else 50.0
                weighted_against += weight
                vote_breakdown.append({"agent_id": m.agent_id, "choice": "against", "weight": weight})

            if weighted_for > weighted_against:
                protocol.status = ProtocolStatus.ACCEPTED.value
            else:
                protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {
                "weighted_for": round(weighted_for, 2),
                "weighted_against": round(weighted_against, 2),
                "vote_breakdown": vote_breakdown,
                "total_votes": len(votes_for) + len(votes_against),
            }

    elif protocol.protocol_type == ProtocolType.RANKED_VOTE.value:
        # Ranked-choice voting with instant runoff
        # Each vote message has payload.rankings = [option1, option2, ...]
        ranked_votes = [m for m in messages if m.message_type == "vote" and m.payload and m.payload.get("rankings")]
        options = config.get("options", [])
        quorum = config.get("quorum", 2)

        if len(ranked_votes) >= quorum and options:
            # Run instant-runoff voting
            active_options = list(options)
            eliminated = []
            rounds = []

            while len(active_options) > 1:
                # Count first-preference votes among active options
                counts = {opt: 0 for opt in active_options}
                for m in ranked_votes:
                    rankings = m.payload.get("rankings", [])
                    # Find highest-ranked still-active option
                    for opt in rankings:
                        if opt in active_options:
                            counts[opt] += 1
                            break

                total = sum(counts.values())
                rounds.append({"active_options": list(active_options), "counts": dict(counts), "total": total})

                if total == 0:
                    break

                # Check for majority
                max_opt = max(counts, key=counts.get)
                if counts[max_opt] > total / 2:
                    protocol.status = ProtocolStatus.ACCEPTED.value
                    protocol.resolved_at = datetime.utcnow()
                    protocol.result = {
                        "winner": max_opt,
                        "rounds": rounds,
                        "total_votes": len(ranked_votes),
                    }
                    return

                # Eliminate the option with fewest votes
                min_opt = min(counts, key=counts.get)
                active_options.remove(min_opt)
                eliminated.append(min_opt)

            if active_options:
                protocol.status = ProtocolStatus.ACCEPTED.value
                protocol.resolved_at = datetime.utcnow()
                protocol.result = {
                    "winner": active_options[0],
                    "rounds": rounds,
                    "eliminated": eliminated,
                    "total_votes": len(ranked_votes),
                }

    elif protocol.protocol_type == ProtocolType.DELIBERATION.value:
        # Multi-round deliberation: requires N discussion messages before final vote
        discussion_messages = [m for m in messages if m.message_type in ("comment", "argument", "evidence")]
        final_votes = [m for m in messages if m.message_type == "vote"]
        min_discussion = config.get("min_discussion_messages", 2)
        quorum = config.get("quorum", 2)

        # Only allow final voting after sufficient discussion
        if len(discussion_messages) >= min_discussion and len(final_votes) >= quorum:
            votes_for = [m for m in final_votes if m.payload and m.payload.get("choice") == "for"]
            votes_against = [m for m in final_votes if m.payload and m.payload.get("choice") == "against"]

            if len(votes_for) > len(votes_against):
                protocol.status = ProtocolStatus.ACCEPTED.value
            else:
                protocol.status = ProtocolStatus.REJECTED.value
            protocol.resolved_at = datetime.utcnow()
            protocol.result = {
                "discussion_count": len(discussion_messages),
                "votes_for": len(votes_for),
                "votes_against": len(votes_against),
                "deliberation_complete": True,
            }


# =========================================================================
# Agent Reputation System
# =========================================================================

@agents_bp.route("/protocol-decision-latency", methods=["GET"])
@unified_auth_required
def protocol_decision_latency():
    """Collaboration protocol decision latency analysis.

    For resolved protocols, computes the latency from creation to
    resolution aggregated by protocol type (avg/median/min/max).
    """
    user = get_current_user()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    from models.agent import CollaborationProtocol
    user_agent_ids = [a.id for a in Agent.query.filter_by(owner_id=user.id).all()]
    if not user_agent_ids:
        return ApiResponse.success({"types": [], "days": days, "total": 0}).to_response()

    since = datetime.utcnow() - timedelta(days=days)
    protocols = (
        CollaborationProtocol.query
        .filter(
            CollaborationProtocol.initiator_agent_id.in_(user_agent_ids),
            CollaborationProtocol.created_at >= since,
            CollaborationProtocol.resolved_at.isnot(None),
        )
        .all()
    )

    by_type = {}
    for p in protocols:
        if not p.resolved_at or not p.created_at:
            continue
        latency = (p.resolved_at - p.created_at).total_seconds()
        if latency < 0:
            continue
        by_type.setdefault(p.protocol_type, []).append(latency)

    def median(vals):
        s = sorted(vals)
        n = len(s)
        if n == 0:
            return 0
        if n % 2 == 1:
            return s[n // 2]
        return (s[n // 2 - 1] + s[n // 2]) / 2

    types = []
    total = 0
    for ptype, lats in sorted(by_type.items(), key=lambda kv: len(kv[1]), reverse=True):
        total += len(lats)
        types.append({
            "protocol_type": ptype,
            "count": len(lats),
            "avg_seconds": round(sum(lats) / len(lats), 1),
            "median_seconds": round(median(lats), 1),
            "min_seconds": round(min(lats), 1),
            "max_seconds": round(max(lats), 1),
        })

    return ApiResponse.success({"types": types, "days": days, "total": total}).to_response()

