"""
Agent 团队任务编排 API

支持多 Agent 协作处理任务的编排机制
"""

from flask import Blueprint, request, g
from datetime import datetime

from models import (
    db, TeamTaskOrchestration, OrchestrationStrategy, OrchestrationStatus,
    TeamSubtask, SubtaskStatus, AgentTeam, AgentTeamStatus, Agent,
    AgentStatus, Project, Task, TaskStatus
)
from core.auth import unified_auth_required, get_current_user
from api.agent_common import (agent_session_required,
                              ensure_workspace_access,
                              get_workspace_or_404,
                              write_agent_audit)
from api.base import ApiResponse, validate_json_request


agent_team_orchestration_bp = Blueprint('agent_team_orchestration', __name__)


@agent_team_orchestration_bp.route('/workspaces/<int:workspace_id>/tasks/<int:task_id>/orchestrate', methods=['POST'])
@unified_auth_required
def start_orchestration(workspace_id, task_id):
    """启动团队编排"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    # 检查任务
    task = Task.query.filter_by(
        id=task_id,
        project_id=Task.project_id  # 需要通过 project 关联检查 workspace
    ).join(Task.project).filter(
        Project.organization_id == workspace_id
    ).first()

    if not task:
        return ApiResponse.not_found('Task not found').to_response()

    data = validate_json_request(
        required_fields=['team_id', 'strategy'],
        optional_fields=['participating_agent_ids', 'subtasks',
                         'config', 'output_aggregator'],
    )
    if isinstance(data, tuple):
        return data

    team_id = data['team_id']
    strategy_str = data['strategy']

    # 检查团队
    team = AgentTeam.query.filter_by(
        id=team_id,
        workspace_id=workspace_id,
        status=AgentTeamStatus.ACTIVE
    ).first()

    if not team:
        return ApiResponse.error('Team not found or inactive', 404).to_response()

    # 验证策略
    try:
        strategy = OrchestrationStrategy(strategy_str)
    except ValueError:
        return ApiResponse.error(
            f'Invalid strategy. Valid options: {[s.value for s in OrchestrationStrategy]}',
            400
        ).to_response()

    # 获取参与的 Agent
    participating_ids = data.get('participating_agent_ids', [])
    if not participating_ids:
        # 默认使用团队所有成员
        participating_ids = [m.agent_id for m in team.members.all()]

    # 验证 Agent 存在
    agents = Agent.query.filter(
        Agent.id.in_(participating_ids),
        Agent.workspace_id == workspace_id,
        Agent.status == AgentStatus.ACTIVE
    ).all()

    valid_agent_ids = [a.id for a in agents]

    # 创建编排实例
    orchestration = TeamTaskOrchestration(
        team_id=team_id,
        task_id=task_id,
        workspace_id=workspace_id,
        created_by_user_id=user.id,
        strategy=strategy,
        participating_agent_ids=valid_agent_ids,
        status=OrchestrationStatus.PENDING,
        output_aggregator_agent_id=data.get('output_aggregator'),
        config=data.get('config', {}),
    )

    db.session.add(orchestration)
    db.session.flush()  # 获取 orchestration.id

    # 创建子任务
    subtasks_data = data.get('subtasks', [])
    if not subtasks_data:
        # 自动创建子任务（基于策略）
        subtasks_data = _auto_create_subtasks(strategy, valid_agent_ids, task)

    for i, subtask_data in enumerate(subtasks_data):
        subtask = TeamSubtask(
            orchestration_id=orchestration.id,
            assigned_agent_id=subtask_data['assigned_agent_id'],
            workspace_id=workspace_id,
            title=subtask_data.get('title', f'Subtask {i+1}'),
            description=subtask_data.get('description', ''),
            stage_index=subtask_data.get('stage_index', 0),
            order_index=subtask_data.get('order_index', i),
            depends_on_subtask_ids=subtask_data.get('depends_on', []),
            input_payload=subtask_data.get('input', {}),
            status=SubtaskStatus.PENDING,
        )
        db.session.add(subtask)

    orchestration.total_stages = max([s.get('stage_index', 0) for s in subtasks_data], default=0) + 1

    db.session.commit()

    write_agent_audit(
        event_type='orchestration.created',
        actor_type='user',
        actor_id=user.id,
        target_type='task',
        target_id=task_id,
        workspace_id=workspace_id,
        payload={
            'orchestration_id': orchestration.id,
            'team_id': team_id,
            'strategy': strategy.value,
            'agent_count': len(valid_agent_ids),
        }
    )

    return ApiResponse.created(
        orchestration.to_dict(include_subtasks=True),
        'Orchestration created successfully'
    ).to_response()


def _auto_create_subtasks(strategy, agent_ids, task):
    """基于策略自动创建子任务"""
    subtasks = []

    if strategy == OrchestrationStrategy.SEQUENTIAL:
        # 顺序执行：每个 Agent 依次处理
        for i, agent_id in enumerate(agent_ids):
            subtasks.append({
                'title': f'Stage {i+1}',
                'description': f'Sequential processing by agent {agent_id}',
                'assigned_agent_id': agent_id,
                'stage_index': i,
                'order_index': 0,
                'depends_on': [i-1] if i > 0 else [],
                'input': {'task_content': task.content},
            })

    elif strategy == OrchestrationStrategy.PARALLEL:
        # 并行执行：所有 Agent 同时处理
        for i, agent_id in enumerate(agent_ids):
            subtasks.append({
                'title': f'Parallel work {i+1}',
                'description': f'Parallel processing by agent {agent_id}',
                'assigned_agent_id': agent_id,
                'stage_index': 0,
                'order_index': i,
                'depends_on': [],
                'input': {'task_content': task.content},
            })

    elif strategy == OrchestrationStrategy.MAP_REDUCE:
        # MapReduce：多个 Map 任务 + 一个 Reduce 任务
        for i, agent_id in enumerate(agent_ids[:-1]):
            subtasks.append({
                'title': f'Map {i+1}',
                'description': f'Map processing',
                'assigned_agent_id': agent_id,
                'stage_index': 0,
                'order_index': i,
                'depends_on': [],
                'input': {'task_content': task.content},
            })

        # Reduce 阶段
        if len(agent_ids) > 0:
            subtasks.append({
                'title': 'Reduce',
                'description': 'Aggregate results',
                'assigned_agent_id': agent_ids[-1],
                'stage_index': 1,
                'order_index': 0,
                'depends_on': list(range(len(agent_ids) - 1)),
                'input': {},
            })

    elif strategy == OrchestrationStrategy.DEBATE:
        # 辩论模式：多轮辩论
        rounds = 3
        for round_num in range(rounds):
            for i, agent_id in enumerate(agent_ids):
                subtasks.append({
                    'title': f'Debate Round {round_num + 1} - Agent {i+1}',
                    'description': f'Debate contribution',
                    'assigned_agent_id': agent_id,
                    'stage_index': round_num,
                    'order_index': i,
                    'depends_on': [],
                    'input': {'task_content': task.content, 'round': round_num + 1},
                })

    else:
        # 默认：每个 Agent 一个子任务
        for i, agent_id in enumerate(agent_ids):
            subtasks.append({
                'title': f'Subtask {i+1}',
                'description': 'Task processing',
                'assigned_agent_id': agent_id,
                'stage_index': 0,
                'order_index': i,
                'depends_on': [],
                'input': {'task_content': task.content},
            })

    return subtasks


@agent_team_orchestration_bp.route('/workspaces/<int:workspace_id>/orchestrations/<int:orchestration_id>', methods=['GET'])
@unified_auth_required
def get_orchestration(workspace_id, orchestration_id):
    """获取编排详情"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    orchestration = TeamTaskOrchestration.query.filter_by(
        id=orchestration_id,
        workspace_id=workspace_id
    ).first()

    if not orchestration:
        return ApiResponse.not_found('Orchestration not found').to_response()

    include_subtasks = request.args.get('include_subtasks', 'true').lower() == 'true'

    return ApiResponse.success(
        orchestration.to_dict(include_subtasks=include_subtasks)
    ).to_response()


@agent_team_orchestration_bp.route('/workspaces/<int:workspace_id>/orchestrations/<int:orchestration_id>/start', methods=['POST'])
@unified_auth_required
def start_orchestration_execution(workspace_id, orchestration_id):
    """启动编排执行"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    orchestration = TeamTaskOrchestration.query.filter_by(
        id=orchestration_id,
        workspace_id=workspace_id
    ).first()

    if not orchestration:
        return ApiResponse.not_found('Orchestration not found').to_response()

    if orchestration.status != OrchestrationStatus.PENDING:
        return ApiResponse.error(
            f'Orchestration is already {orchestration.status.value}',
            409
        ).to_response()

    orchestration.status = OrchestrationStatus.RUNNING
    orchestration.started_at = datetime.utcnow()
    orchestration.current_stage = 0

    # 激活第一阶段的所有子任务
    TeamSubtask.query.filter_by(
        orchestration_id=orchestration_id,
        stage_index=0
    ).update({'status': SubtaskStatus.ASSIGNED})

    db.session.commit()

    return ApiResponse.success(orchestration.to_dict()).to_response()


@agent_team_orchestration_bp.route('/workspaces/<int:workspace_id>/orchestrations/<int:orchestration_id>/cancel', methods=['POST'])
@unified_auth_required
def cancel_orchestration(workspace_id, orchestration_id):
    """取消编排"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    orchestration = TeamTaskOrchestration.query.filter_by(
        id=orchestration_id,
        workspace_id=workspace_id
    ).first()

    if not orchestration:
        return ApiResponse.not_found('Orchestration not found').to_response()

    if orchestration.status in [OrchestrationStatus.COMPLETED, OrchestrationStatus.CANCELLED]:
        return ApiResponse.error('Orchestration is already finished', 409).to_response()

    orchestration.status = OrchestrationStatus.CANCELLED
    orchestration.completed_at = datetime.utcnow()

    # 取消所有未完成的子任务
    TeamSubtask.query.filter(
        TeamSubtask.orchestration_id == orchestration_id,
        TeamSubtask.status.in_([SubtaskStatus.PENDING, SubtaskStatus.ASSIGNED, SubtaskStatus.RUNNING])
    ).update({'status': SubtaskStatus.SKIPPED})

    db.session.commit()

    return ApiResponse.success(orchestration.to_dict()).to_response()


# ==================== Agent Runtime API ====================

@agent_team_orchestration_bp.route('/agent/team/subtasks', methods=['GET'])
@agent_session_required
def list_agent_subtasks():
    """Agent 拉取分配给自己的子任务"""
    agent = g.current_agent
    if not agent:
        return ApiResponse.error('Agent session required', 401).to_response()

    subtasks = TeamSubtask.query.filter_by(
        assigned_agent_id=agent.id,
        status=SubtaskStatus.ASSIGNED
    ).join(TeamTaskOrchestration).filter(
        TeamTaskOrchestration.status == OrchestrationStatus.RUNNING
    ).order_by(TeamSubtask.created_at.asc()).all()

    return ApiResponse.success({
        'items': [s.to_dict() for s in subtasks],
        'total': len(subtasks)
    }).to_response()


@agent_team_orchestration_bp.route('/agent/team/subtasks/<int:subtask_id>/accept', methods=['POST'])
@agent_session_required
def accept_subtask(subtask_id):
    """Agent 接受子任务"""
    agent = g.current_agent
    if not agent:
        return ApiResponse.error('Agent session required', 401).to_response()

    subtask = TeamSubtask.query.filter_by(
        id=subtask_id,
        assigned_agent_id=agent.id
    ).first()

    if not subtask:
        return ApiResponse.not_found('Subtask not found').to_response()

    if subtask.status != SubtaskStatus.ASSIGNED:
        return ApiResponse.error(f'Subtask is {subtask.status.value}', 409).to_response()

    subtask.status = SubtaskStatus.RUNNING
    subtask.started_at = datetime.utcnow()

    db.session.commit()

    return ApiResponse.success(subtask.to_dict()).to_response()


@agent_team_orchestration_bp.route('/agent/team/subtasks/<int:subtask_id>/complete', methods=['POST'])
@agent_session_required
def complete_subtask(subtask_id):
    """Agent 提交子任务结果"""
    agent = g.current_agent
    if not agent:
        return ApiResponse.error('Agent session required', 401).to_response()

    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    subtask = TeamSubtask.query.filter_by(
        id=subtask_id,
        assigned_agent_id=agent.id
    ).first()

    if not subtask:
        return ApiResponse.not_found('Subtask not found').to_response()

    if subtask.status != SubtaskStatus.RUNNING:
        return ApiResponse.error(f'Subtask is {subtask.status.value}', 409).to_response()

    subtask.status = SubtaskStatus.COMPLETED
    subtask.completed_at = datetime.utcnow()
    subtask.output_payload = data.get('output', {})

    # 尝试推进到下一阶段
    _try_advance_stage(subtask.orchestration_id)

    db.session.commit()

    return ApiResponse.success(subtask.to_dict()).to_response()


@agent_team_orchestration_bp.route('/agent/team/subtasks/<int:subtask_id>/fail', methods=['POST'])
@agent_session_required
def fail_subtask(subtask_id):
    """Agent 报告子任务失败"""
    agent = g.current_agent
    if not agent:
        return ApiResponse.error('Agent session required', 401).to_response()

    data = validate_json_request()
    if isinstance(data, tuple):
        return data

    subtask = TeamSubtask.query.filter_by(
        id=subtask_id,
        assigned_agent_id=agent.id
    ).first()

    if not subtask:
        return ApiResponse.not_found('Subtask not found').to_response()

    subtask.status = SubtaskStatus.FAILED
    subtask.last_error = data.get('error', 'Unknown error')
    subtask.attempt_count = (subtask.attempt_count or 0) + 1

    # 检查重试策略
    max_retries = subtask.orchestration.config.get('max_retries', 2)
    if subtask.attempt_count < max_retries:
        # 重置状态等待重试
        subtask.status = SubtaskStatus.ASSIGNED
    else:
        # 检查失败处理策略
        failure_strategy = subtask.orchestration.config.get('on_failure', 'fail_all')
        if failure_strategy == 'fail_all':
            subtask.orchestration.status = OrchestrationStatus.FAILED
            subtask.orchestration.completed_at = datetime.utcnow()

    db.session.commit()

    return ApiResponse.success(subtask.to_dict()).to_response()


def _try_advance_stage(orchestration_id):
    """尝试推进到下一阶段"""
    orchestration = TeamTaskOrchestration.query.get(orchestration_id)
    if not orchestration or orchestration.status != OrchestrationStatus.RUNNING:
        return

    current_stage = orchestration.current_stage

    # 检查当前阶段是否完成
    pending_in_stage = TeamSubtask.query.filter(
        TeamSubtask.orchestration_id == orchestration_id,
        TeamSubtask.stage_index == current_stage,
        TeamSubtask.status.in_([SubtaskStatus.PENDING, SubtaskStatus.ASSIGNED, SubtaskStatus.RUNNING])
    ).count()

    if pending_in_stage > 0:
        return  # 当前阶段还有未完成的子任务

    # 当前阶段完成，推进到下一阶段
    next_stage = current_stage + 1

    if next_stage >= orchestration.total_stages:
        # 所有阶段完成
        orchestration.status = OrchestrationStatus.COMPLETED
        orchestration.completed_at = datetime.utcnow()
        orchestration.current_stage = next_stage

        # 触发结果聚合
        _aggregate_results(orchestration)
    else:
        # 激活下一阶段的子任务
        orchestration.current_stage = next_stage

        # 激活该阶段的所有子任务
        TeamSubtask.query.filter_by(
            orchestration_id=orchestration_id,
            stage_index=next_stage
        ).update({'status': SubtaskStatus.ASSIGNED})


def _aggregate_results(orchestration):
    """聚合子任务结果"""
    completed_subtasks = TeamSubtask.query.filter_by(
        orchestration_id=orchestration.id,
        status=SubtaskStatus.COMPLETED
    ).all()

    results = {
        'subtask_count': len(completed_subtasks),
        'outputs': [s.output_payload for s in completed_subtasks],
        'aggregated_at': datetime.utcnow().isoformat(),
    }

    # 如果有聚合 Agent，分配给聚合 Agent
    if orchestration.output_aggregator_agent_id:
        # 创建聚合子任务
        aggregator_subtask = TeamSubtask(
            orchestration_id=orchestration.id,
            assigned_agent_id=orchestration.output_aggregator_agent_id,
            workspace_id=orchestration.workspace_id,
            title='Aggregate Results',
            description='Aggregate all subtask outputs',
            stage_index=orchestration.total_stages,
            order_index=0,
            status=SubtaskStatus.ASSIGNED,
            input_payload={'subtask_outputs': results['outputs']},
        )
        db.session.add(aggregator_subtask)
        orchestration.total_stages += 1

    orchestration.result_payload = results


@agent_team_orchestration_bp.route('/workspaces/<int:workspace_id>/orchestrations/<int:orchestration_id>/aggregate', methods=['POST'])
@unified_auth_required
def manual_aggregate(workspace_id, orchestration_id):
    """手动触发结果聚合"""
    user = get_current_user()
    workspace, err = get_workspace_or_404(workspace_id)
    if err:
        return err

    access_err = ensure_workspace_access(user, workspace)
    if access_err:
        return access_err

    orchestration = TeamTaskOrchestration.query.filter_by(
        id=orchestration_id,
        workspace_id=workspace_id
    ).first()

    if not orchestration:
        return ApiResponse.not_found('Orchestration not found').to_response()

    _aggregate_results(orchestration)
    db.session.commit()

    return ApiResponse.success(orchestration.to_dict(include_subtasks=True)).to_response()
