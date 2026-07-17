"""
Agent channel CRUD, messaging, and analytics endpoints.
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
    AgentChannel,
    AgentChannelMember,
    AgentChannelMessage,
    AuditLog,
    get_request_args,
    paginate_query,
    parse_enum,
)

@agents_bp.route("/channels", methods=["GET"])
@unified_auth_required
def list_channels():
    """List collaboration channels. Optional filters: project_id, task_id, is_active."""
    user = get_current_user()
    query = AgentChannel.query.filter_by(owner_id=user.id)

    project_id = request.args.get("project_id", type=int)
    task_id = request.args.get("task_id", type=int)
    is_active = request.args.get("is_active", type=str)

    if project_id:
        query = query.filter_by(project_id=project_id)
    if task_id:
        query = query.filter_by(task_id=task_id)
    if is_active is not None:
        query = query.filter_by(is_active=is_active.lower() == "true")

    channels = query.order_by(AgentChannel.updated_at.desc()).all()
    return ApiResponse.success(
        [c.to_dict(include_members=True, include_last_message=True) for c in channels],
    ).to_response()


@agents_bp.route("/channels", methods=["POST"])
@unified_auth_required
def create_channel():
    """Create a collaboration channel."""
    user = get_current_user()
    data = validate_json_request()

    name = data.get("name", "").strip()
    if not name:
        return ApiResponse.error("Channel name is required", 400).to_response()

    channel = AgentChannel.create(
        name=name,
        description=data.get("description", ""),
        project_id=data.get("project_id"),
        task_id=data.get("task_id"),
        owner_id=user.id,
    )

    # Auto-add creator's agents as members if specified
    agent_ids = data.get("agent_ids", [])
    for aid in agent_ids:
        agent = Agent.query.filter_by(id=aid, owner_id=user.id).first()
        if agent:
            AgentChannelMember.create(
                channel_id=channel.id,
                agent_id=agent.id,
                role="owner" if agent_ids.index(aid) == 0 else "member",
            )

    AuditLog.record("channel_create", target_type="channel", target_id=channel.id, user_id=user.id,
                     details={"name": name, "agent_count": len(agent_ids)})
    return ApiResponse.created(channel.to_dict(include_members=True), "Channel created").to_response()


@agents_bp.route("/channels/<int:channel_id>", methods=["GET"])
@unified_auth_required
def get_channel(channel_id):
    """Get channel details with members."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    return ApiResponse.success(channel.to_dict(include_members=True)).to_response()


@agents_bp.route("/channels/<int:channel_id>", methods=["PUT"])
@unified_auth_required
def update_channel(channel_id):
    """Update channel properties."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    data = validate_json_request()
    if "name" in data:
        channel.name = data["name"]
    if "description" in data:
        channel.description = data["description"]
    if "is_active" in data:
        channel.is_active = data["is_active"]

    db.session.commit()
    return ApiResponse.success(channel.to_dict(include_members=True), "Channel updated").to_response()


@agents_bp.route("/channels/<int:channel_id>", methods=["DELETE"])
@unified_auth_required
def delete_channel(channel_id):
    """Delete a channel."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    db.session.delete(channel)
    db.session.commit()
    AuditLog.record("channel_delete", target_type="channel", target_id=channel_id, user_id=user.id)
    return ApiResponse.success(None, "Channel deleted").to_response()


@agents_bp.route("/channels/<int:channel_id>/members", methods=["POST"])
@unified_auth_required
def add_channel_member(channel_id):
    """Add an Agent to a channel."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    data = validate_json_request()
    agent_id = data.get("agent_id")
    if not agent_id:
        return ApiResponse.error("agent_id is required", 400).to_response()

    agent = Agent.query.filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        return ApiResponse.not_found("Agent not found").to_response()

    existing = AgentChannelMember.query.filter_by(channel_id=channel_id, agent_id=agent_id).first()
    if existing:
        return ApiResponse.error("Agent is already a member", 409).to_response()

    member = AgentChannelMember.create(
        channel_id=channel_id,
        agent_id=agent_id,
        role=data.get("role", "member"),
    )
    db.session.commit()
    return ApiResponse.success(member.to_dict(), "Member added").to_response()


@agents_bp.route("/channels/<int:channel_id>/members/<int:member_id>", methods=["DELETE"])
@unified_auth_required
def remove_channel_member(channel_id, member_id):
    """Remove an Agent from a channel."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    member = AgentChannelMember.query.filter_by(id=member_id, channel_id=channel_id).first()
    if not member:
        return ApiResponse.not_found("Member not found").to_response()

    db.session.delete(member)
    db.session.commit()
    return ApiResponse.success(None, "Member removed").to_response()


@agents_bp.route("/channels/<int:channel_id>/messages", methods=["GET"])
@unified_auth_required
def list_channel_messages(channel_id):
    """List messages in a channel. Supports pagination."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    before_id = request.args.get("before_id", type=int)

    query = AgentChannelMessage.query.filter_by(channel_id=channel_id)
    if before_id:
        query = query.filter(AgentChannelMessage.id < before_id)

    messages = query.order_by(AgentChannelMessage.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    # Return in chronological order
    messages.reverse()

    return ApiResponse.success([m.to_dict() for m in messages]).to_response()


@agents_bp.route("/channels/<int:channel_id>/messages", methods=["POST"])
@unified_auth_required
def send_channel_message(channel_id):
    """Send a message to a channel (from an Agent or human)."""
    user = get_current_user()
    channel = AgentChannel.query.filter_by(id=channel_id, owner_id=user.id).first()
    if not channel:
        return ApiResponse.not_found("Channel not found").to_response()

    data = validate_json_request()
    content = data.get("content", "").strip()
    if not content:
        return ApiResponse.error("Message content is required", 400).to_response()

    msg = AgentChannelMessage.create(
        channel_id=channel_id,
        sender_agent_id=data.get("agent_id"),
        sender_user_id=user.id if not data.get("agent_id") else None,
        content=content,
        message_type=data.get("message_type", "text"),
        extra_metadata=data.get("metadata"),
    )

    # Deliver to all channel members via Notification + SSE
    for member in channel.members:
        if member.agent_id and member.agent_id != data.get("agent_id"):
            Notification.create(
                user_id=user.id,
                agent_id=member.agent_id,
                title=f"频道消息: {channel.name}",
                message=content[:200],
                category="channel_message",
                priority="info",
                task_id=channel.task_id,
                metadata={"channel_id": channel_id, "message_id": msg.id},
            )

    db.session.commit()
    return ApiResponse.created(msg.to_dict(), "Message sent").to_response()


# =========================================================================
# Collaboration Templates
# =========================================================================


# Built-in collaboration templates
_BUILTIN_COLLAB_TEMPLATES = [
    {
        "key": "code_review_squad",
        "name": "代码审查三人组",
        "description": "一个领导者协调代码审查流程，审查员执行代码审查，测试者负责测试验证。条件路由：审查通过则进入测试，审查失败则回退修复。",
        "category": "review",
        "agent_specs": [
            {"name": "审查协调者", "kind": "coordinator", "capabilities": ["coordination", "code_review"], "collaboration_role": "leader"},
            {"name": "代码审查员", "kind": "autonomous", "capabilities": ["code_review", "reading", "frontend", "backend"], "collaboration_role": "follower"},
            {"name": "测试验证员", "kind": "autonomous", "capabilities": ["testing", "qa"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "coord_review", "name": "协调审查任务", "required_capabilities": ["coordination", "code_review"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "code_review", "name": "执行代码审查", "required_capabilities": ["code_review"], "depends_on": ["coord_review"], "on_failure": "continue"},
            {"step_key": "test_on_pass", "name": "测试验证（审查通过）", "required_capabilities": ["testing"], "depends_on": ["code_review"], "on_failure": "skip",
             "condition": {"step_key": "code_review", "operator": "succeeded"}},
            {"step_key": "fix_on_fail", "name": "修复代码（审查失败）", "required_capabilities": ["code_review", "frontend"], "depends_on": ["code_review"], "on_failure": "continue",
             "condition": {"step_key": "code_review", "operator": "failed"}},
        ],
    },
    {
        "key": "research_team",
        "name": "研究小组",
        "description": "一个领导者分配研究任务，两个研究员并行调研不同方向，最后汇总。条件路由：任一方向失败时启动备选方案。",
        "category": "research",
        "agent_specs": [
            {"name": "研究主管", "kind": "coordinator", "capabilities": ["coordination", "research"], "collaboration_role": "leader"},
            {"name": "研究员 A", "kind": "autonomous", "capabilities": ["research", "reading"], "collaboration_role": "follower"},
            {"name": "研究员 B", "kind": "autonomous", "capabilities": ["research", "documentation"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "assign_research", "name": "分配研究任务", "required_capabilities": ["coordination", "research"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "research_a", "name": "研究方向 A", "required_capabilities": ["research", "reading"], "depends_on": ["assign_research"], "on_failure": "continue"},
            {"step_key": "research_b", "name": "研究方向 B", "required_capabilities": ["research", "documentation"], "depends_on": ["assign_research"], "on_failure": "continue"},
            {"step_key": "synthesis", "name": "汇总研究成果", "required_capabilities": ["coordination", "research"], "depends_on": ["research_a", "research_b"], "on_failure": "abort"},
            {"step_key": "fallback_a", "name": "方向 A 备选方案", "required_capabilities": ["research"], "depends_on": ["research_a"], "on_failure": "skip",
             "condition": {"step_key": "research_a", "operator": "failed"}},
            {"step_key": "fallback_b", "name": "方向 B 备选方案", "required_capabilities": ["documentation"], "depends_on": ["research_b"], "on_failure": "skip",
             "condition": {"step_key": "research_b", "operator": "failed"}},
        ],
    },
    {
        "key": "devops_pipeline",
        "name": "运维流水线",
        "description": "构建、部署、监控三阶段自动化运维团队。条件路由：构建失败时跳过部署直接通知，部署成功后启动监控。",
        "category": "devops",
        "agent_specs": [
            {"name": "运维协调者", "kind": "coordinator", "capabilities": ["coordination", "devops"], "collaboration_role": "leader"},
            {"name": "构建工程师", "kind": "autonomous", "capabilities": ["devops", "deployment", "backend"], "collaboration_role": "follower"},
            {"name": "监控工程师", "kind": "autonomous", "capabilities": ["devops", "security", "monitoring"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "coordinate", "name": "协调运维任务", "required_capabilities": ["coordination", "devops"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "build", "name": "构建", "required_capabilities": ["devops", "deployment"], "depends_on": ["coordinate"], "on_failure": "continue"},
            {"step_key": "deploy_on_success", "name": "部署（构建成功）", "required_capabilities": ["deployment"], "depends_on": ["build"], "on_failure": "continue",
             "condition": {"step_key": "build", "operator": "succeeded"}},
            {"step_key": "notify_on_build_fail", "name": "通知（构建失败）", "required_capabilities": ["coordination"], "depends_on": ["build"], "on_failure": "skip",
             "condition": {"step_key": "build", "operator": "failed"}},
            {"step_key": "monitor", "name": "监控", "required_capabilities": ["devops", "security"], "depends_on": ["deploy_on_success"], "on_failure": "continue",
             "condition": {"step_key": "deploy_on_success", "operator": "succeeded"}},
        ],
    },
    {
        "key": "bug_fix_pipeline",
        "name": "Bug 修复流水线",
        "description": "诊断、修复、验证三阶段 Bug 修复团队。条件路由：严重 Bug 直接分配高级工程师，普通 Bug 由常规工程师处理。",
        "category": "development",
        "agent_specs": [
            {"name": "Bug 协调者", "kind": "coordinator", "capabilities": ["coordination", "backend"], "collaboration_role": "leader"},
            {"name": "诊断工程师", "kind": "autonomous", "capabilities": ["backend", "testing"], "collaboration_role": "follower"},
            {"name": "修复工程师", "kind": "autonomous", "capabilities": ["backend", "frontend"], "collaboration_role": "follower"},
            {"name": "验证工程师", "kind": "autonomous", "capabilities": ["testing", "qa"], "collaboration_role": "follower"},
        ],
        "workflow_steps": [
            {"step_key": "triage", "name": "Bug 分诊", "required_capabilities": ["coordination"], "depends_on": [], "on_failure": "abort"},
            {"step_key": "diagnose", "name": "诊断 Bug", "required_capabilities": ["backend", "testing"], "depends_on": ["triage"], "on_failure": "continue"},
            {"step_key": "fix", "name": "修复 Bug", "required_capabilities": ["backend"], "depends_on": ["diagnose"], "on_failure": "continue"},
            {"step_key": "verify", "name": "验证修复", "required_capabilities": ["testing", "qa"], "depends_on": ["fix"], "on_failure": "continue",
             "condition": {"step_key": "fix", "operator": "succeeded"}},
            {"step_key": "escalate", "name": "升级处理（修复失败）", "required_capabilities": ["coordination"], "depends_on": ["fix"], "on_failure": "skip",
             "condition": {"step_key": "fix", "operator": "failed"}},
        ],
    },
]

@agents_bp.route("/channels/activity-trend", methods=["GET"])
@unified_auth_required
def channel_activity_trend():
    """Channel activity trend.

    Per-channel daily message count sparkline and active member count.

    Query params:
    - days: lookback window (1-90, default 14)
    - limit: max channels returned (1-20, default 10)
    """
    user = get_current_user()
    try:
        days = max(1, min(90, int(request.args.get("days", 14))))
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except (TypeError, ValueError):
        days = 14
        limit = 10

    since = datetime.utcnow() - timedelta(days=days)

    from models.agent import AgentChannel, AgentChannelMessage
    channels = (
        AgentChannel.query
        .filter(AgentChannel.owner_id == user.id)
        .order_by(AgentChannel.created_at.desc())
        .limit(limit * 2)
        .all()
    )

    date_range = []
    for i in range(days):
        d = (datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        date_range.append(d)

    results = []
    for ch in channels:
        # Daily message counts
        daily_counts = []
        active_senders = set()
        for d in date_range:
            day_start = datetime.strptime(d, "%Y-%m-%d")
            day_end = day_start + timedelta(days=1)
            msgs = (
                AgentChannelMessage.query
                .filter(
                    AgentChannelMessage.channel_id == ch.id,
                    AgentChannelMessage.created_at >= day_start,
                    AgentChannelMessage.created_at < day_end,
                )
                .all()
            )
            daily_counts.append(len(msgs))
            for m in msgs:
                if m.sender_id:
                    active_senders.add(m.sender_id)

        total = sum(daily_counts)
        if total > 0:
            results.append({
                "channel_id": ch.id,
                "channel_name": ch.name or f"Channel#{ch.id}",
                "daily_counts": daily_counts,
                "active_members": len(active_senders),
                "date_range": date_range,
            })

    results.sort(key=lambda c: sum(c["daily_counts"]), reverse=True)
    return ApiResponse.success({"channels": results[:limit], "days": days}).to_response()

