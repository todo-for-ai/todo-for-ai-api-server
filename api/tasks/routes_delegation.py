"""Task delegation routes — delegate tasks to agents and reclaim them."""

from flask import request

from models import db, Task, TaskStatus, Agent, AgentStatus
from api.base import ApiResponse
from core.auth import unified_auth_required, get_current_user

from . import tasks_bp


@tasks_bp.route('/<int:task_id>/delegate', methods=['POST'])
@unified_auth_required
def delegate_task(task_id):
    """Delegate a task to an agent."""
    data = request.get_json()
    if not data or not data.get('agent_id'):
        return ApiResponse.error('agent_id is required').to_response()

    agent_id = data['agent_id']
    task = db.session.get(Task, task_id)
    if not task:
        return ApiResponse.error('Task not found', 404).to_response()

    agent = db.session.get(Agent, agent_id)
    if not agent or agent.status != AgentStatus.ACTIVE:
        return ApiResponse.error('Agent not found or not active', 404).to_response()

    # Update task assignment
    user = get_current_user()
    assignees = task.assignees or []
    agent_ref = {'type': 'agent', 'id': agent.id, 'name': agent.display_name or agent.name}
    if agent_ref not in assignees:
        assignees.append(agent_ref)
    task.assignees = assignees
    task.status = TaskStatus.IN_PROGRESS

    # Log the delegation
    from models import TaskLog, TaskLogActorType
    log = TaskLog(
        task_id=task_id,
        actor_type=TaskLogActorType.HUMAN,
        actor_user_id=user.id if user else None,
        content=f'Delegated to agent **{agent.display_name or agent.name}**',
        content_type='text/markdown',
    )
    db.session.add(log)
    db.session.commit()

    return ApiResponse.success(data=task.to_dict()).to_response()


@tasks_bp.route('/<int:task_id>/reclaim', methods=['POST'])
@unified_auth_required
def reclaim_task(task_id):
    """Reclaim a task from an agent back to the human owner."""
    task = db.session.get(Task, task_id)
    if not task:
        return ApiResponse.error('Task not found', 404).to_response()

    user = get_current_user()

    # Remove agent-type assignees
    assignees = task.assignees or []
    agent_names = []
    cleaned = []
    for a in assignees:
        if a.get('type') == 'agent':
            agent_names.append(a.get('name', 'agent'))
        else:
            cleaned.append(a)
    task.assignees = cleaned
    task.status = TaskStatus.TODO

    # Log the reclaim
    from models import TaskLog, TaskLogActorType
    names_str = ', '.join(agent_names) if agent_names else 'agent'
    log = TaskLog(
        task_id=task_id,
        actor_type=TaskLogActorType.HUMAN,
        actor_user_id=user.id if user else None,
        content=f'Reclaimed from {names_str}',
        content_type='text/markdown',
    )
    db.session.add(log)
    db.session.commit()

    return ApiResponse.success(data=task.to_dict()).to_response()


@tasks_bp.route('/workspaces/<int:workspace_id>/delegatable-agents', methods=['GET'])
@unified_auth_required
def list_delegatable_agents(workspace_id):
    """List active agents in a workspace available for delegation."""
    agents = (
        Agent.query
        .filter(Agent.workspace_id == workspace_id, Agent.status == AgentStatus.ACTIVE)
        .order_by(Agent.name.asc())
        .all()
    )

    data = [
        {
            'id': a.id,
            'name': a.display_name or a.name,
            'role': (a.capability_tags or []),
            'status': a.status.value if a.status else None,
        }
        for a in agents
    ]
    return ApiResponse.success(data=data).to_response()
