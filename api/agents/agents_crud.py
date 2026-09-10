"""Agent 生命周期路由：列表 / 创建 / 自助注册 / 发现 / 详情 / 更新 / 心跳。

从 ``_core.py`` 拆出（迭代 47）。所有权语义：仅 ``owner_id`` 归属当前用户
（协作 Agent 的组织共享由工作区侧路由承担）。
"""

from datetime import datetime

from flask import request

from services.agent_working_schedule import normalize_working_schedule

from ._shared import (
    agents_bp,
    ApiResponse,
    get_request_args,
    paginate_serialized,
    validate_json_request,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentKind,
    AgentStatus,
    AuditLog,
    Notification,
    mark_stale_agents_offline,
    expire_stale_assignments,
    _client_ip,
    _queue_sse,
    parse_enum,
    get_owned_agent_or_response,
)


def _normalize_working_schedule_field(data):
    """校验并规范化 data 中的 working_schedule 字段。

    返回 (data, None)；字段非法时返回 (None, 错误响应)。
    """
    if "working_schedule" not in data:
        return data, None
    try:
        data["working_schedule"] = normalize_working_schedule(data["working_schedule"])
        return data, None
    except ValueError as e:
        return None, ApiResponse.error(f"Invalid working_schedule: {e}", 400).to_response()


@agents_bp.route("", methods=["GET"])
@unified_auth_required
def list_agents():
    """List current user's Agents."""
    try:
        current_user = get_current_user()
        args = get_request_args()

        stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
        expired_assignments = expire_stale_assignments(current_user=current_user)
        if stale_agents or expired_assignments:
            db.session.commit()

        query = Agent.query.filter_by(owner_id=current_user.id)

        status = request.args.get("status")
        if status:
            try:
                query = query.filter_by(status=parse_enum(AgentStatus, status, "status"))
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        if args["search"]:
            search_term = f"%{args['search']}%"
            query = query.filter(Agent.name.like(search_term) | Agent.description.like(search_term))

        if args["sort_by"] == "name":
            order_column = Agent.name
        elif args["sort_by"] == "last_seen_at":
            order_column = Agent.last_seen_at
        else:
            order_column = Agent.created_at

        query = query.order_by(order_column.desc() if args["sort_order"] == "desc" else order_column.asc())
        result = paginate_serialized(
            query,
            args["page"],
            args["per_page"],
            lambda agent: agent.to_dict(include_stats=True),
        )

        return ApiResponse.success(result, "Agents retrieved successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to retrieve Agents: {str(e)}", 500).to_response()


@agents_bp.route("", methods=["POST"])
@unified_auth_required
def create_agent():
    """Create an Agent identity."""
    try:
        current_user = get_current_user()
        data = validate_json_request(
            required_fields=["name"],
            optional_fields=["description", "kind", "status", "provider", "model", "capabilities", "config", "collaboration_role", "working_schedule"],
        )

        if isinstance(data, tuple):
            return data

        data, err = _normalize_working_schedule_field(data)
        if err:
            return err

        try:
            kind = parse_enum(AgentKind, data.get("kind", AgentKind.ASSISTANT.value), "kind")
            status = parse_enum(AgentStatus, data.get("status", AgentStatus.ACTIVE.value), "status")
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        agent = Agent(
            owner_id=current_user.id,
            name=data["name"].strip(),
            description=data.get("description"),
            kind=kind,
            status=status,
            provider=data.get("provider"),
            model=data.get("model"),
            capabilities=data.get("capabilities") or [],
            config=data.get("config") or {},
            last_seen_at=datetime.utcnow() if status == AgentStatus.ACTIVE else None,
            created_by=current_user.email,
        )

        db.session.add(agent)
        db.session.commit()

        AuditLog.record(
            action="agent.created", resource_type="agent", resource_id=agent.id,
            actor_type="human", actor_user_id=current_user.id,
            detail={"name": agent.name, "kind": kind.value},
            ip_address=_client_ip(),
        )
        db.session.commit()

        return ApiResponse.created(agent.to_dict(include_stats=True), "Agent created successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create Agent: {str(e)}", 500).to_response()


@agents_bp.route("/self-register", methods=["POST"])
@unified_auth_required
def self_register_agent():
    """Allow an external Agent to self-register into the platform.

    If an agent with the same name and provider already exists, it updates
    the existing record instead of creating a duplicate. This supports
    idempotent registration by agents that restart frequently.
    """
    try:
        user = get_current_user()
        data = validate_json_request(
            required_fields=["name"],
            optional_fields=["description", "kind", "provider", "model", "capabilities", "config", "collaboration_role"],
        )

        if isinstance(data, tuple):
            return data

        name = data["name"].strip()
        provider = data.get("provider", "")

        # Check for existing agent with same name + provider (idempotent registration)
        existing = Agent.query.filter_by(owner_id=user.id, name=name, provider=provider).first() if provider else None

        if existing:
            # Update existing agent
            if data.get("description"):
                existing.description = data["description"]
            if data.get("model"):
                existing.model = data["model"]
            if data.get("capabilities"):
                existing.capabilities = data["capabilities"]
            if data.get("config"):
                existing.config = data["config"]
            if data.get("collaboration_role"):
                existing.collaboration_role = data["collaboration_role"]
            existing.status = AgentStatus.ACTIVE
            existing.last_seen_at = datetime.utcnow()
            db.session.commit()

            AuditLog.record("agent.self_register", resource_type="agent", resource_id=existing.id,
                            actor_type="agent", actor_user_id=user.id,
                            detail={"name": name, "action": "updated"}, ip_address=_client_ip())
            db.session.commit()

            return ApiResponse.success(existing.to_dict(include_stats=True), "Agent re-registered").to_response()

        # Create new agent
        try:
            kind = parse_enum(AgentKind, data.get("kind", AgentKind.AUTONOMOUS.value), "kind")
        except ValueError as e:
            return ApiResponse.error(str(e), 400).to_response()

        agent = Agent(
            owner_id=user.id,
            name=name,
            description=data.get("description"),
            kind=kind,
            status=AgentStatus.ACTIVE,
            provider=provider,
            model=data.get("model"),
            capabilities=data.get("capabilities") or [],
            config=data.get("config") or {},
            collaboration_role=data.get("collaboration_role"),
            last_seen_at=datetime.utcnow(),
            created_by=user.email,
        )
        db.session.add(agent)
        db.session.commit()

        AuditLog.record("agent.self_register", resource_type="agent", resource_id=agent.id,
                        actor_type="agent", actor_user_id=user.id,
                        detail={"name": name, "action": "created"}, ip_address=_client_ip())
        db.session.commit()

        return ApiResponse.created(agent.to_dict(include_stats=True), "Agent self-registered").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Self-registration failed: {str(e)}", 500).to_response()


@agents_bp.route("/discover", methods=["GET"])
@unified_auth_required
def discover_agents():
    """Find available Agents by capability, role, or kind.

    Query params:
      capability — filter by capability (can be repeated)
      collaboration_role — filter by role (leader/follower/standalone)
      kind — filter by kind (assistant/autonomous/coordinator/external)
      status — filter by status (default: active)
    """
    user = get_current_user()
    query = Agent.query.filter_by(owner_id=user.id)

    status = request.args.get("status", "active")
    if status:
        try:
            status_enum = parse_enum(AgentStatus, status, "status")
            query = query.filter_by(status=status_enum)
        except ValueError:
            pass

    kind = request.args.get("kind")
    if kind:
        try:
            kind_enum = parse_enum(AgentKind, kind, "kind")
            query = query.filter_by(kind=kind_enum)
        except ValueError:
            pass

    role = request.args.get("collaboration_role")
    if role:
        query = query.filter_by(collaboration_role=role)

    capabilities = request.args.getlist("capability")
    agents = query.all()

    # Post-filter by capabilities (JSON column query is DB-dependent)
    if capabilities:
        filtered = []
        for agent in agents:
            agent_caps = set(agent.capabilities or [])
            if any(c in agent_caps for c in capabilities):
                filtered.append(agent)
        agents = filtered

    return ApiResponse.success([a.to_dict(include_stats=True) for a in agents]).to_response()


@agents_bp.route("/<int:agent_id>", methods=["GET"])
@unified_auth_required
def get_agent(agent_id):
    """Get Agent details."""
    current_user = get_current_user()
    agent, response = get_owned_agent_or_response(agent_id, current_user)
    if response:
        return response

    stale_agents = mark_stale_agents_offline(owner_id=current_user.id)
    expired_assignments = expire_stale_assignments(current_user=current_user, agent_id=agent.id)
    if stale_agents or expired_assignments:
        db.session.commit()

    return ApiResponse.success(agent.to_dict(include_stats=True), "Agent retrieved successfully").to_response()


@agents_bp.route("/<int:agent_id>", methods=["PUT"])
@unified_auth_required
def update_agent(agent_id):
    """Update Agent settings."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        data = validate_json_request(
            optional_fields=["name", "description", "kind", "status", "provider", "model", "capabilities", "config", "collaboration_role", "role_template_id", "working_schedule"],
        )

        if isinstance(data, tuple):
            return data

        data, err = _normalize_working_schedule_field(data)
        if err:
            return err

        # 岗位角色绑定：校验模板存在且为内置或同工作区
        if "role_template_id" in data:
            from models import AgentRoleTemplate, AgentRoleTemplateStatus
            raw_role = data["role_template_id"]
            if raw_role in (None, "", 0):
                data["role_template_id"] = None
            else:
                template = db.session.get(AgentRoleTemplate, int(raw_role))
                if not template or template.status != AgentRoleTemplateStatus.ACTIVE:
                    return ApiResponse.error("Role template not found or inactive", 400).to_response()
                if not template.is_builtin and template.workspace_id != agent.workspace_id:
                    return ApiResponse.error("Role template does not belong to this agent workspace", 400).to_response()
                data["role_template_id"] = template.id

        if "kind" in data:
            try:
                data["kind"] = parse_enum(AgentKind, data["kind"], "kind")
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        if "status" in data:
            try:
                data["status"] = parse_enum(AgentStatus, data["status"], "status")
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()

        if "name" in data:
            data["name"] = data["name"].strip()

        # Capability registration mode: merge (default) or replace
        cap_mode = data.pop("_capability_mode", "merge")
        if "capabilities" in data and cap_mode == "merge":
            existing = set(agent.capabilities or [])
            new_caps = set(data["capabilities"] or [])
            data["capabilities"] = sorted(existing | new_caps)

        agent.update_from_dict(data)
        if data.get("status") == AgentStatus.ACTIVE:
            agent.last_seen_at = datetime.utcnow()
        db.session.commit()

        # If capabilities or config changed, notify the owner via SSE + Notification
        config_changed_fields = [f for f in ("capabilities", "config", "working_schedule") if f in data]
        if config_changed_fields:
            _queue_sse(
                current_user.id,
                "agent_config_changed",
                {"agent_id": agent.id, "agent_name": agent.name, "changed_fields": config_changed_fields},
            )
            Notification.create_notification(
                user_id=current_user.id,
                event_type="agent_config_changed",
                agent_id=agent.id,
                payload={"changed_fields": config_changed_fields},
            )

        return ApiResponse.success(agent.to_dict(include_stats=True), "Agent updated successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update Agent: {str(e)}", 500).to_response()


@agents_bp.route("/<int:agent_id>/heartbeat", methods=["POST"])
@unified_auth_required
def heartbeat_agent(agent_id):
    """Record Agent heartbeat."""
    try:
        current_user = get_current_user()
        agent, response = get_owned_agent_or_response(agent_id, current_user)
        if response:
            return response

        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status:
            try:
                agent.status = parse_enum(AgentStatus, status, "status")
            except ValueError as e:
                return ApiResponse.error(str(e), 400).to_response()
        else:
            agent.heartbeat()

        agent.last_seen_at = datetime.utcnow()
        db.session.commit()

        return ApiResponse.success(agent.to_dict(include_stats=True), "Agent heartbeat recorded").to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to record Agent heartbeat: {str(e)}", 500).to_response()
