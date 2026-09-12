"""执行者路由与任务派发（含云端联动）。"""

import logging
from datetime import timedelta

from models import Agent, AgentTaskAttempt, AgentTaskLease, Task, TaskStatus, db

from services.lease_policy import effective_lease_ttl

log = logging.getLogger(__name__)


def role_context(agent: Agent) -> dict:
    """Agent 的岗位角色上下文（来自绑定的角色模板）。"""
    template = agent.role_template if agent else None
    if not template:
        return {'role': None, 'role_description': None}
    return {
        'role': template.display_name or template.name,
        'role_category': template.category,
        'role_description': (template.description or '')[:500],
    }


def director(loop) -> Agent:
    """循环的指挥者：显式指定优先，否则退回绑定 Agent（单 Agent 模式）。"""
    return loop.director if loop.director_agent_id else loop.agent


def executor_pool(loop) -> list:
    """工作区内可接单的活跃 Agent（含岗位绑定），按创建序。

    只包含当前处于工作时间区间内的 Agent——编排拆分时不应把步骤路由给
    窗口外的执行者（会被派发门禁挡下，白费一轮）。
    """
    from services.agent_working_schedule import is_in_working_window

    return [
        a for a in
        Agent.query.filter(
            Agent.workspace_id == loop.workspace_id,
            Agent.runner_enabled.is_(True),
            Agent.status == 'ACTIVE',
        )
        .order_by(Agent.id)
        .all()
        if is_in_working_window(a.working_schedule or {})
    ]


def available_executor_roles(loop, limit=20) -> list:
    """可接单 Agent 的岗位角色清单（供指挥者拆解时指派步骤参考）。"""
    roles = []
    for a in executor_pool(loop):
        template = a.role_template
        if not template:
            continue
        name = (template.display_name or template.name or '').strip()
        if name and name not in roles:
            roles.append(name)
        if len(roles) >= limit:
            break
    return roles


def pick_executor(loop, step: dict) -> Agent:
    """按步骤声明的岗位要求路由执行者；无匹配退回绑定 Agent。

    岗位匹配到多个执行者时选当前活跃租约最少的一个（负载均衡），
    让编排拆分出的步骤真正摊到多个 Agent 上并行干活。
    """
    from services.workspace_runtime_policy import agent_active_lease_count

    wanted = (step.get('role') or '').strip()
    if wanted:
        best_key, best = None, None
        for cand in executor_pool(loop):
            template = cand.role_template
            if not template:
                continue
            names = {(template.display_name or '').strip(), (template.name or '').strip()}
            names.discard('')
            if wanted not in names and not any(wanted in n for n in names):
                continue
            key = (agent_active_lease_count(cand.id), cand.id)
            if best_key is None or key < best_key:
                best_key, best = key, cand
        if best is not None:
            return best
    return loop.agent


def create_round_task(loop, step: dict, executor: Agent = None) -> Task:
    """把计划步骤物化为循环任务（内容带执行角色前缀）。"""
    executor = executor or loop.agent
    role_name = (step.get('role') or '').strip()
    if not role_name:
        role_name = (role_context(executor).get('role') or '').strip()
    content = (step.get('content') or step.get('title') or '').strip()
    role_line = f"【执行角色：{role_name}】\n" if role_name else ''
    task = Task(
        title=(step.get('title') or f'{loop.title} · 下一轮').strip()[:500],
        content=f"{role_line}{content}",
        project_id=loop.project_id,
        owner_id=loop.created_by,
        is_ai_task=True,
        status=TaskStatus.TODO,
    )
    db.session.add(task)
    db.session.flush()
    task.add_tag(loop.tag)
    db.session.flush()
    return task


def assign_task_to_agent(task, agent: Agent):
    """把任务直接派给指定执行者（建 attempt+lease 并推送）。

    派发前过两道门（与 pull 路径同语义）：
    - 工作区编排并发上限（「同时干活」Agent 数）——到上限且该 Agent 不在岗
      则不派（任务留 TODO，等容量释放后由 pull 兜底）；
    - token/时长预算——超限走审批队列事件并跳过派发。
    """
    from api.agent_common import generate_id, now_utc
    from api.agent_runtime_websocket import push_task_to_agent
    from services.workspace_runtime_policy import check_dispatch_capacity

    capacity = check_dispatch_capacity(agent.workspace_id, agent.id)
    if not capacity['allowed']:
        log.warning("goal_loop.dispatch_capacity_blocked",
                    extra={"agent_id": agent.id, "task_id": task.id,
                           "active_agents": capacity['active_agents'],
                           "limit": capacity['limit']})
        return

    from services.budget_service import check_budgets, raise_budget_exceeded
    violations = check_budgets(
        workspace_id=int(agent.workspace_id),
        agent_id=int(agent.id),
        project_id=int(task.project_id) if task.project_id else None,
    )
    if violations:
        raise_budget_exceeded(
            workspace_id=int(agent.workspace_id),
            violations=violations,
            context={'agent_id': int(agent.id), 'task_id': int(task.id)},
        )
        log.warning("goal_loop.dispatch_budget_blocked",
                    extra={"agent_id": agent.id, "task_id": task.id,
                           "violations": len(violations)})
        return

    now = now_utc()
    attempt_id = generate_id('att')
    lease_id = generate_id('lea')
    db.session.add(AgentTaskAttempt(
        attempt_id=attempt_id,
        task_id=task.id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        state='ACTIVE',
        lease_id=lease_id,
        started_at=now,
        created_by='system',
    ))
    db.session.add(AgentTaskLease(
        lease_id=lease_id,
        task_id=task.id,
        attempt_id=attempt_id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        expires_at=now + timedelta(seconds=effective_lease_ttl(agent_id=agent.id)),
        active=True,
        created_by='system',
    ))
    if task.status == TaskStatus.TODO:
        task.status = TaskStatus.IN_PROGRESS
    db.session.commit()

    try:
        push_task_to_agent(agent.id, {
            'task_id': task.id,
            'attempt_id': attempt_id,
            'lease_id': lease_id,
            'payload': {
                'title': task.title,
                'content': task.content,
                'prompt': task.title or task.content or '',
            },
            'project_id': task.project_id,
            'priority': str(task.priority) if task.priority else None,
            'created_at': task.created_at.isoformat() if task.created_at else None,
            'workspace_id': agent.workspace_id,
        })
    except Exception:  # noqa: BLE001  WebSocket 未连接时静默（agent 轮询可拉到）
        pass


def ensure_cloud_executor(loop, executor: Agent):
    """编排↔执行环境联动：执行者不在岗时按其执行模式解析后端拉起。

    managed_runner → 部署级后端（k8s/docker/...）按需创建运行时；
    external_pull 反连 → remote 后端（在线即已岗，离线仅登记待反连）。
    任意失败（无集群配置/无密钥/工作区超限）都只降级为 external_pull
    兜底派发，绝不阻塞循环推进；确保结果记录在日志。
    """
    try:
        if not executor:
            return
        if executor.workspace_id != loop.workspace_id:
            return
        from models import AgentKey
        key_row = AgentKey.query.filter_by(agent_id=executor.id, is_active=True).first()
        agent_key = key_row.reveal() if key_row else None
        if not agent_key:
            log.warning("goal_loop.cloud_executor_no_key", extra={"agent_id": executor.id})
            return
        from services.runtime_env import get_runtime_provider_for_agent
        result = get_runtime_provider_for_agent(executor).ensure_runtime(executor, agent_key)
        log.info("goal_loop.cloud_executor_ensured",
                 extra={"agent_id": executor.id, "result": result.get('status')})
    except Exception:  # noqa: BLE001
        log.warning("goal_loop.cloud_executor_ensure_failed", exc_info=True)
        db.session.rollback()


def auto_assign(task, agent: Agent = None):
    try:
        if agent is not None:
            assign_task_to_agent(task, agent)
        else:
            from services.agent_runtime_controller import AgentRuntimeController
            AgentRuntimeController.auto_assign_task(task)
    except Exception:  # noqa: BLE001
        db.session.rollback()
