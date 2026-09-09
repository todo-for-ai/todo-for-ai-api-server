"""Collaboration template routes（列表 / 创建 / 删除 / 实例化建队）。"""

from datetime import datetime

from flask import request

from .channels import _BUILTIN_COLLAB_TEMPLATES
from ._workflow_helpers import _advance_workflow
from utils.logger import logger
from ._shared import (
    agents_bp,
    ApiResponse,
    get_current_user,
    unified_auth_required,
    db,
    Agent,
    AgentKind,
    AgentStatus,
    AgentChannel,
    AgentChannelMember,
    AuditLog,
    CollaborationTemplate,
    StepStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowStatus,
    parse_enum,
    validate_json_request,
    _client_ip,
)

@agents_bp.route("/collaboration-templates", methods=["GET"])
@unified_auth_required
def list_collaboration_templates():
    """List collaboration templates (built-in + user-created)."""
    user = get_current_user()
    category = request.args.get("category")

    # Built-in templates
    builtin = _BUILTIN_COLLAB_TEMPLATES
    if category:
        builtin = [t for t in builtin if t["category"] == category]

    # User-created templates
    query = CollaborationTemplate.query.filter_by(owner_id=user.id)
    if category:
        query = query.filter_by(category=category)
    user_templates = query.order_by(CollaborationTemplate.name.asc()).all()

    result = [
        {"id": f"builtin:{t['key']}", "name": t["name"], "description": t["description"],
         "category": t["category"], "agent_specs": t["agent_specs"],
         "workflow_steps": t.get("workflow_steps", []),
         "is_builtin": True}
        for t in builtin
    ] + [t.to_dict() for t in user_templates]

    return ApiResponse.success(result).to_response()


@agents_bp.route("/collaboration-templates", methods=["POST"])
@unified_auth_required
def create_collaboration_template():
    """Create a custom collaboration template."""
    user = get_current_user()
    data = validate_json_request()

    name = (data.get("name") or "").strip()
    if not name:
        return ApiResponse.error("name is required", 400).to_response()

    agent_specs = data.get("agent_specs", [])
    if not agent_specs:
        return ApiResponse.error("agent_specs is required (at least 1 agent)", 400).to_response()

    template = CollaborationTemplate.create(
        owner_id=user.id,
        name=name,
        description=data.get("description", ""),
        category=data.get("category"),
        agent_specs=agent_specs,
        workflow_id=data.get("workflow_id"),
    )

    db.session.commit()
    return ApiResponse.created(template.to_dict(), "Collaboration template created").to_response()


@agents_bp.route("/collaboration-templates/<int:template_id>", methods=["DELETE"])
@unified_auth_required
def delete_collaboration_template(template_id):
    """Delete a user-created collaboration template."""
    user = get_current_user()
    template = CollaborationTemplate.query.filter_by(id=template_id, owner_id=user.id).first()
    if not template:
        return ApiResponse.not_found("Template not found").to_response()

    db.session.delete(template)
    db.session.commit()
    return ApiResponse.success(None, "Template deleted").to_response()


@agents_bp.route("/collaboration-templates/<string:template_key>/instantiate", methods=["POST"])
@unified_auth_required
def instantiate_collaboration_template(template_key):
    """Instantiate a collaboration template: create the agents and optionally launch the workflow.

    For built-in templates, template_key is like "builtin:code_review_squad".
    For user templates, template_key is the numeric ID.
    """
    user = get_current_user()
    # 空 body 合法（builtin 模板无需参数）；validate_json_request 会把空 body 当 400
    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")

    # Resolve template
    if template_key.startswith("builtin:"):
        key = template_key.replace("builtin:", "")
        template_data = next((t for t in _BUILTIN_COLLAB_TEMPLATES if t["key"] == key), None)
        if not template_data:
            return ApiResponse.not_found("Built-in template not found").to_response()
        agent_specs = template_data["agent_specs"]
        workflow_steps_spec = template_data.get("workflow_steps", [])
        workflow_id = None
    else:
        try:
            tid = int(template_key)
        except ValueError:
            return ApiResponse.error("Invalid template key", 400).to_response()
        template = CollaborationTemplate.query.filter_by(id=tid, owner_id=user.id).first()
        if not template:
            return ApiResponse.not_found("Template not found").to_response()
        agent_specs = template.agent_specs or []
        workflow_id = template.workflow_id
        workflow_steps_spec = []

    # Create agents
    created_agents = []
    channel = None
    for spec in agent_specs:
        try:
            kind = parse_enum(AgentKind, spec.get("kind", "autonomous"), "kind")
        except ValueError:
            kind = AgentKind.AUTONOMOUS

        agent = Agent(
            owner_id=user.id,
            name=spec.get("name", f"Agent-{len(created_agents) + 1}"),
            description=f"Created from template: {template_key}",
            kind=kind,
            status=AgentStatus.ACTIVE,
            capabilities=spec.get("capabilities", []),
            collaboration_role=spec.get("collaboration_role", "standalone"),
            provider=spec.get("provider"),
            model=spec.get("model"),
            last_seen_at=datetime.utcnow(),
            created_by=user.email,
        )
        db.session.add(agent)
        db.session.flush()  # get the ID
        created_agents.append(agent)

    db.session.commit()

    # Create a collaboration channel for the team
    if len(created_agents) >= 2:
        channel = AgentChannel.create(
            name=f"团队: {template_key}",
            description=f"从模板 {template_key} 创建的协作频道",
            project_id=project_id,
            owner_id=user.id,
        )
        db.session.flush()  # 先取 channel.id，再建成员
        for i, agent in enumerate(created_agents):
            AgentChannelMember.create(
                channel_id=channel.id,
                agent_id=agent.id,
                role="owner" if i == 0 else "member",
            )
        db.session.commit()

    # Optionally launch workflow
    wf_run = None
    if workflow_id and project_id:
        wf = Workflow.query.filter_by(id=workflow_id, owner_id=user.id).first()
        if wf:
            wf_run = WorkflowRun.create(
                workflow_id=wf.id,
                root_task_id=None,
                project_id=project_id,
                owner_id=user.id,
                status=WorkflowStatus.PENDING,
            )
            # Create step runs
            for step in wf.steps:
                WorkflowStepRun.create(
                    run_id=wf_run.id,
                    step_key=step.step_key,
                    status=StepStatus.PENDING,
                )
            db.session.commit()
            try:
                _advance_workflow(wf_run)
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.warning("messaging.advance_workflow_failed",
                               extra={"workflow_run_id": wf_run.id})
    elif workflow_steps_spec and project_id:
        # Built-in template with workflow steps: auto-create workflow definition
        # 注意：Workflow 无 project_id 列，项目归属由 WorkflowRun 携带
        wf = Workflow.create(
            owner_id=user.id,
            name=f"模板工作流: {template_data.get('name', template_key)}",
            description=template_data.get('description', ''),
            definition={"steps": workflow_steps_spec},
            max_parallel_steps=3,
        )
        db.session.flush()  # 先取 wf.id，再建 steps
        for step_spec in workflow_steps_spec:
            # Try to match agent by capability
            agent_id = None
            req_caps = step_spec.get("required_capabilities", [])
            for agent in created_agents:
                agent_caps = set(agent.capabilities or [])
                if set(req_caps).intersection(agent_caps):
                    agent_id = agent.id
                    break

            WorkflowStep.create(
                workflow_id=wf.id,
                step_key=step_spec["step_key"],
                name=step_spec.get("name", step_spec["step_key"]),
                required_capabilities=req_caps,
                depends_on=step_spec.get("depends_on", []),
                on_failure=step_spec.get("on_failure", "abort"),
                agent_id=agent_id,
                condition=step_spec.get("condition"),
            )
        db.session.commit()

        # Launch the workflow
        wf_run = WorkflowRun.create(
            workflow_id=wf.id,
            root_task_id=None,
            project_id=project_id,
            owner_id=user.id,
            status=WorkflowStatus.PENDING,
        )
        for step in wf.steps:
            WorkflowStepRun.create(
                run_id=wf_run.id,
                step_key=step.step_key,
                status=StepStatus.PENDING,
            )
        db.session.commit()
        try:
            _advance_workflow(wf_run)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.warning("messaging.advance_workflow_failed",
                           extra={"workflow_run_id": wf_run.id})

    AuditLog.record(action="collaboration_template_instantiate",
                     resource_type="template", resource_id=0,
                     actor_type="human", actor_user_id=user.id,
                     detail={"template_key": template_key, "agents_created": len(created_agents),
                             "channel_id": channel.id if channel else None,
                             "workflow_run_id": wf_run.id if wf_run else None},
                     ip_address=_client_ip())
    db.session.commit()

    return ApiResponse.success({
        "agents": [a.to_dict() for a in created_agents],
        "channel": channel.to_dict(include_members=True) if channel else None,
        "workflow_run": wf_run.to_dict(include_step_runs=True) if wf_run else None,
    }, f"Instantiated template: {len(created_agents)} agent(s) created").to_response()


# =========================================================================
# Agent Knowledge Base
# =========================================================================
