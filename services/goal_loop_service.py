"""
GoalLoop 目标循环驱动器（v3：多 Agent 自动编排）

循环机制：创建循环 → 指挥者把目标**拆解成有序计划**（steps，每步可指名
执行岗位）→ 逐轮把计划步骤物化为任务并**按岗位路由执行者 Agent** → 任务
成功则直接执行下一步（省一次评审调用），任务失败或计划耗尽则触发**评审**
（继续扩展计划/重排计划/宣告完成/受阻）。

多 Agent 分工：loop.director（指挥者）负责拆解与评审，规划提示词注入
指挥者岗位上下文与工作区可用执行者角色清单；执行者按步骤声明的 role
（岗位名）从工作区活跃 Agent 池匹配路由，无匹配退回绑定 Agent（单 Agent
模式完全向后兼容）。

护栏：轮数上限、连续受阻容忍、人工暂停/停止/kick。并发防护：advancing
CAS 标记，同一循环不并发双发；LLM 调用期间不持锁，落库前复核状态。

规划器可插拔：默认走平台 LLM（feature='goal_loop'）；
GOAL_LOOP_PLANNER=scripted 时使用确定性脚本规划器（仅供测试/E2E）。
"""

import json
import os
from datetime import datetime, timedelta

from models import db, GoalLoop, GoalLoopStatus, Task, TaskStatus, Project, Agent

TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.CANCELLED}
ACTIVE_TASK_STATUSES = {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED}

DEFAULT_ROUNDS_LIMIT = 10
DEFAULT_STALL_LIMIT = 2
MAX_ROUNDS_LIMIT = 2000
DEFAULT_STUCK_TASK_HOURS = 6


def _naive_utc_now():
    return datetime.utcnow()


def _stuck_task_hours() -> float:
    """看门狗判定轮次卡死的小时数（GOAL_LOOP_STUCK_TASK_HOURS）。"""
    try:
        return float(os.getenv('GOAL_LOOP_STUCK_TASK_HOURS', '') or DEFAULT_STUCK_TASK_HOURS)
    except ValueError:
        return DEFAULT_STUCK_TASK_HOURS


def _time_budget_exceeded(loop) -> bool:
    if not loop.time_budget_hours or not loop.started_at:
        return False
    elapsed = _naive_utc_now() - loop.started_at
    return elapsed.total_seconds() >= loop.time_budget_hours * 3600


def _budget_line(loop) -> str:
    """给规划器/评审器的剩余时间预算上下文。"""
    if not loop.time_budget_hours:
        return ''
    if not loop.started_at:
        return f'时长预算：{loop.time_budget_hours} 小时（尚未开跑）\n'
    elapsed = (_naive_utc_now() - loop.started_at).total_seconds() / 3600.0
    remaining = max(0.0, loop.time_budget_hours - elapsed)
    return f'剩余时间预算：{remaining:.1f}/{loop.time_budget_hours} 小时\n'


# ── 查询助手 ──

def _tag_prefix():
    from models.goal_loop import GOAL_LOOP_TAG_PREFIX
    return GOAL_LOOP_TAG_PREFIX


def _loop_task_query(loop_id):
    from sqlalchemy import cast, String
    tag = f'{_tag_prefix()}{loop_id}'
    # tags 是 JSON 数组列，按标签字符串包含匹配（量级=单循环轮数，可接受）
    return Task.query.filter(
        Task.project_id.isnot(None),
        cast(Task.tags, String).like(f'%{tag}%'),
    )


def loop_tasks(loop_id):
    """循环的全部轮次任务（按创建顺序）。"""
    return _loop_task_query(loop_id).order_by(Task.id).all()


def rounds_done(loop_id) -> int:
    return _loop_task_query(loop_id).count()


def _recent_history(loop, limit=5):
    rows = loop_tasks(loop.id)[-limit:]
    return [
        {
            'title': t.title,
            'status': t.status.value if t.status else None,
        }
        for t in rows
    ]


def _role_context(agent: Agent) -> dict:
    """Agent 的岗位角色上下文（来自绑定的角色模板）。"""
    template = agent.role_template if agent else None
    if not template:
        return {'role': None, 'role_description': None}
    return {
        'role': template.display_name or template.name,
        'role_category': template.category,
        'role_description': (template.description or '')[:500],
    }


def _director(loop) -> Agent:
    """循环的指挥者：显式指定优先，否则退回绑定 Agent（单 Agent 模式）。"""
    return loop.director if loop.director_agent_id else loop.agent


def _executor_pool(loop) -> list:
    """工作区内可接单的活跃 Agent（含岗位绑定），按创建序。"""
    return (
        Agent.query.filter(
            Agent.workspace_id == loop.workspace_id,
            Agent.runner_enabled.is_(True),
            Agent.status == 'ACTIVE',
        )
        .order_by(Agent.id)
        .all()
    )


def _available_executor_roles(loop, limit=20) -> list:
    """可接单 Agent 的岗位角色清单（供指挥者拆解时指派步骤参考）。"""
    roles = []
    for a in _executor_pool(loop):
        template = a.role_template
        if not template:
            continue
        name = (template.display_name or template.name or '').strip()
        if name and name not in roles:
            roles.append(name)
        if len(roles) >= limit:
            break
    return roles


def _pick_executor(loop, step: dict) -> Agent:
    """按步骤声明的岗位要求路由执行者；无匹配退回绑定 Agent。"""
    wanted = (step.get('role') or '').strip()
    if wanted:
        for cand in _executor_pool(loop):
            template = cand.role_template
            if not template:
                continue
            names = {(template.display_name or '').strip(), (template.name or '').strip()}
            names.discard('')
            if wanted in names or any(wanted in n for n in names):
                return cand
    return loop.agent


# ── 规划器（v2：拆解 + 评审 两段） ──

def _planner_mode() -> str:
    return (os.getenv('GOAL_LOOP_PLANNER') or 'llm').strip().lower()


def _extract_json(raw):
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块包裹）。"""
    text = (raw or '').strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text.lower().startswith('json'):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _valid_steps(steps, max_steps) -> bool:
    return (
        isinstance(steps, list)
        and 1 <= len(steps) <= max_steps
        and all(isinstance(s, dict) and (s.get('title') or '').strip() for s in steps)
    )


def _llm_call(loop, system_prompt: str, user_prompt: str) -> dict:
    from services.ai_service import call_llm_production
    result = call_llm_production(
        feature='goal_loop',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        user_id=loop.created_by or 0,
        use_cache=False,
        temperature=0.4,
        max_tokens=2000,
    )
    if not result.get('success'):
        raise RuntimeError(f"llm_failed: {result.get('error')}")
    return _extract_json(result['data'])


def _decompose(loop) -> list:
    """指挥者把目标拆解成有序计划步骤（每步可指名执行岗位）。"""
    role = _role_context(_director(loop))
    role_line = ''
    if role.get('role'):
        role_line = f"指挥者角色：{role['role']}（{role.get('role_category') or ''}）{role.get('role_description') or ''}\n"
    roles = _available_executor_roles(loop)
    roles_line = f"可用执行者角色：{'、'.join(roles)}\n" if roles else ''

    system_prompt = (
        '你是目标循环的指挥者。把目标拆解为有序的执行步骤（计划），'
        '不同步骤可安排给不同岗位的执行者完成（如规划/开发/测试/验收分工）。'
        '只输出 JSON：{"steps": [{"title": "步骤标题", "content": "给执行者的可执行指令", '
        '"role": "执行岗位（可选，从可用执行者角色中选择）"}]}，'
        '步骤数量不超过轮数上限，最后一步应包含验收/收尾。'
    )
    user_prompt = (
        f'{role_line}'
        f'{roles_line}'
        f'{_budget_line(loop)}'
        f'目标：{loop.goal_text}\n'
        f'完成标准：{loop.done_definition or "（未明确，由你判断）"}\n'
        f'轮数上限：{loop.rounds_limit}'
    )
    parsed = _llm_call(loop, system_prompt, user_prompt)
    steps = parsed.get('steps') if isinstance(parsed, dict) else None
    if not _valid_steps(steps, loop.rounds_limit):
        raise RuntimeError('llm_bad_plan')
    return steps


def _review(loop, last_status: str) -> dict:
    """计划耗尽或上轮失败后的评审：扩展/重排计划、宣告完成或受阻。"""
    role = _role_context(_director(loop))
    role_line = f"指挥者角色：{role.get('role')}\n" if role.get('role') else ''
    roles = _available_executor_roles(loop)
    roles_line = f"可用执行者角色：{'、'.join(roles)}\n" if roles else ''

    system_prompt = (
        '你是目标循环的评审器（指挥者）。根据目标、完成标准和执行历史决定下一步。'
        '只输出 JSON：{"action": "complete|extend|blocked", '
        '"steps": [{"title": "...", "content": "...", "role": "执行岗位（可选）"}]'
        '（action=extend 时必填，为剩余计划）, '
        '"reason": "决策原因或完成总结"}'
    )
    user_prompt = (
        f'{role_line}'
        f'{roles_line}'
        f'{_budget_line(loop)}'
        f'目标：{loop.goal_text}\n'
        f'完成标准：{loop.done_definition or "（未明确，由你判断）"}\n'
        f'已执行轮数：{rounds_done(loop.id)}/{loop.rounds_limit}\n'
        f'最近轮次：{json.dumps(_recent_history(loop), ensure_ascii=False)}\n'
        f'上一轮状态：{last_status or "未知"}'
    )
    parsed = _llm_call(loop, system_prompt, user_prompt)
    action = (parsed.get('action') or '').strip().lower()
    if action not in ('complete', 'extend', 'blocked'):
        raise RuntimeError(f'llm_bad_action: {action}')
    if action == 'extend' and not _valid_steps(parsed.get('steps'), loop.rounds_limit):
        raise RuntimeError('llm_extend_without_steps')
    return parsed


def _scripted_decompose(loop) -> list:
    target = int(os.getenv('GOAL_LOOP_SCRIPTED_ROUNDS', '3'))
    return [
        {
            'title': f'{loop.title} · 计划步骤 {i}',
            'content': f'朝目标推进第 {i}/{target} 步。目标：{loop.goal_text}',
        }
        for i in range(1, target + 1)
    ]


def _scripted_review(loop, last_status: str) -> dict:
    if last_status == 'done':
        return {
            'action': 'complete',
            'reason': f'脚本规划器：已完成 {rounds_done(loop.id)} 轮，目标达成',
        }
    return {'action': 'blocked', 'reason': f'脚本规划器：上轮状态 {last_status}，无法推进'}


def _call_decompose(loop) -> list:
    if _planner_mode() == 'scripted':
        return _scripted_decompose(loop)
    return _decompose(loop)


def _call_review(loop, last_status: str) -> dict:
    if _planner_mode() == 'scripted':
        return _scripted_review(loop, last_status)
    return _review(loop, last_status)


# ── 核心推进逻辑 ──

def maybe_advance(loop_id, trigger_task_id=None) -> dict:
    """推进一次循环（幂等、并发安全）。返回 {advanced: bool, reason: str}。"""
    loop = db.session.get(GoalLoop, loop_id)
    if not loop:
        return {'advanced': False, 'reason': 'loop_not_found'}

    if loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': f'not_running:{loop.status.value}'}

    claimed = GoalLoop.query.filter_by(id=loop_id, advancing=0).update({'advancing': 1})
    db.session.commit()
    if not claimed:
        return {'advanced': False, 'reason': 'already_advancing'}

    try:
        return _advance_locked(loop_id, trigger_task_id)
    finally:
        try:
            GoalLoop.query.filter_by(id=loop_id).update({'advancing': 0})
            db.session.commit()
        except Exception:
            db.session.rollback()


def _advance_locked(loop_id, trigger_task_id=None) -> dict:
    loop = db.session.get(GoalLoop, loop_id)
    if not loop or loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': 'not_running'}

    done = rounds_done(loop.id)
    if done >= loop.rounds_limit:
        _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                last_error=f'轮数上限 {loop.rounds_limit} 已耗尽，目标未宣告完成')
        return {'advanced': False, 'reason': 'rounds_limit'}

    if _time_budget_exceeded(loop):
        _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                last_error=f'时长预算 {loop.time_budget_hours} 小时已耗尽，目标未宣告完成')
        return {'advanced': False, 'reason': 'time_budget_exhausted'}

    loop_tasks_all = loop_tasks(loop.id)
    active = [t for t in loop_tasks_all if t.status in ACTIVE_TASK_STATUSES]
    if active:
        return {'advanced': False, 'reason': 'active_task_exists'}

    last_status = None
    if trigger_task_id:
        trigger = db.session.get(Task, trigger_task_id)
        if trigger is not None:
            last_status = trigger.status.value if trigger.status else None

    # ── ① 无计划：先拆解 ──
    if not loop.plan:
        try:
            steps = _call_decompose(loop)
        except Exception as exc:  # noqa: BLE001
            return _register_stall(loop, f'decompose_failed: {exc}')
        db.session.expire(loop)
        if loop.status != GoalLoopStatus.RUNNING:
            return {'advanced': False, 'reason': 'not_running_after_planner'}
        loop.plan = steps
        loop.plan_index = 0
        loop.plan_revision = (loop.plan_revision or 0) + 1

    plan = loop.plan or []
    plan_index = loop.plan_index or 0

    # ── ② 计划有剩余步骤 且 上轮成功（或首轮）：直接物化下一步 ──
    if plan_index < len(plan) and (last_status in (None, 'done')):
        step = plan[plan_index]
        task = _create_round_task(loop, step)
        loop.plan_index = plan_index + 1
        loop.last_task_id = task.id
        loop.stall_count = 0
        loop.last_error = None
        if loop.started_at is None:
            loop.started_at = _naive_utc_now()
        db.session.commit()
        _auto_assign(task, _pick_executor(loop, step))
        return {'advanced': True, 'reason': 'task_created', 'task_id': task.id}

    # ── ③ 计划耗尽 或 上轮失败：评审 ──
    try:
        decision = _call_review(loop, last_status or 'unknown')
    except Exception as exc:  # noqa: BLE001
        return _register_stall(loop, f'review_failed: {exc}')

    db.session.expire(loop)
    if loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': 'not_running_after_planner'}

    action = (decision.get('action') or '').strip().lower()

    if action == 'complete':
        loop.status = GoalLoopStatus.DONE
        loop.completion_summary = decision.get('reason') or ''
        loop.stall_count = 0
        loop.finished_at = _naive_utc_now()
        db.session.commit()
        return {'advanced': False, 'reason': 'completed'}

    if action == 'extend':
        steps = decision.get('steps')
        if not _valid_steps(steps, loop.rounds_limit):
            return _register_stall(loop, 'extend_without_valid_steps')
        remaining = list(plan[max(plan_index, 0):])
        loop.plan = remaining + steps
        loop.plan_index = max(plan_index, 0)
        loop.plan_revision = (loop.plan_revision or 0) + 1
        step = loop.plan[loop.plan_index]
        task = _create_round_task(loop, step)
        loop.plan_index += 1
        loop.stall_count = 0
        loop.last_error = None
        db.session.commit()
        _auto_assign(task, _pick_executor(loop, step))
        return {'advanced': True, 'reason': 'plan_extended', 'task_id': task.id}

    return _register_stall(loop, f"blocked: {decision.get('reason') or '未给出原因'}")


def _register_stall(loop, reason: str) -> dict:
    db.session.expire(loop)
    loop.stall_count = (loop.stall_count or 0) + 1
    loop.last_error = reason[:2000]
    status_changed = False
    if loop.status == GoalLoopStatus.RUNNING and loop.stall_count >= (loop.stall_limit or DEFAULT_STALL_LIMIT):
        loop.status = GoalLoopStatus.STALLED
        loop.finished_at = _naive_utc_now()
        status_changed = True
    db.session.commit()
    return {
        'advanced': False,
        'reason': 'stalled' if status_changed else 'stall_counted',
        'stall_count': loop.stall_count,
    }


def _finish(loop, status: GoalLoopStatus, last_error: str = None):
    loop.status = status
    if last_error:
        loop.last_error = last_error
    loop.finished_at = _naive_utc_now()
    db.session.commit()


def _create_round_task(loop, step: dict, executor: Agent = None) -> Task:
    executor = executor or loop.agent
    role_name = (step.get('role') or '').strip()
    if not role_name:
        role_name = (_role_context(executor).get('role') or '').strip()
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


def _assign_task_to_agent(task, agent: Agent):
    """把任务直接派给指定执行者（建 attempt+lease 并推送）。

    AgentRuntimeController.auto_assign_task 固定派给工作区第一个活跃 Agent，
    无法按步骤岗位路由，且该文件有并行会话在改，故此处自包含实现。
    """
    from api.agent_common import generate_id, now_utc
    from models import AgentTaskAttempt, AgentTaskLease

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
        expires_at=now + timedelta(seconds=60),
        active=True,
        created_by='system',
    ))
    if task.status == TaskStatus.TODO:
        task.status = TaskStatus.IN_PROGRESS
    db.session.commit()

    try:
        from api.agent_runtime_websocket import push_task_to_agent
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


def _auto_assign(task, agent: Agent = None):
    try:
        if agent is not None:
            _assign_task_to_agent(task, agent)
        else:
            from services.agent_runtime_controller import AgentRuntimeController
            AgentRuntimeController.auto_assign_task(task)
    except Exception:  # noqa: BLE001
        db.session.rollback()


# ── 对外操作 ──

def create_loop(*, project: Project, agent: Agent, title: str, goal_text: str,
                done_definition: str = None, rounds_limit: int = DEFAULT_ROUNDS_LIMIT,
                created_by: int = None, director: Agent = None,
                time_budget_hours: int = None, stall_limit: int = DEFAULT_STALL_LIMIT) -> GoalLoop:
    def _clamp(value, lo, hi, default):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, value))

    loop = GoalLoop(
        workspace_id=project.organization_id,
        project_id=project.id,
        agent_id=agent.id,
        director_agent_id=director.id if director else None,
        title=title.strip()[:500],
        goal_text=goal_text,
        done_definition=(done_definition or '').strip() or None,
        status=GoalLoopStatus.RUNNING,
        rounds_limit=_clamp(rounds_limit, 1, MAX_ROUNDS_LIMIT, DEFAULT_ROUNDS_LIMIT),
        time_budget_hours=_clamp(time_budget_hours, 1, 24 * 30, None) if time_budget_hours else None,
        stall_limit=_clamp(stall_limit, 1, 50, DEFAULT_STALL_LIMIT),
        created_by=created_by,
    )
    db.session.add(loop)
    db.session.commit()
    maybe_advance(loop.id)
    return loop


def notify_task_finished(task_id: int):
    """任务终态钩子：任务属于某个循环时推进它。任何路由都可安全调用。"""
    try:
        task = db.session.get(Task, task_id)
        if not task or not task.tags:
            return
        prefix = _tag_prefix()
        loop_id = None
        for tag in task.tags:
            if isinstance(tag, str) and tag.startswith(prefix):
                try:
                    loop_id = int(tag[len(prefix):])
                except ValueError:
                    continue
                break
        if loop_id:
            maybe_advance(loop_id, trigger_task_id=task_id)
    except Exception:
        db.session.rollback()


def set_status(loop_id: int, status: GoalLoopStatus) -> GoalLoop:
    loop = db.session.get(GoalLoop, loop_id)
    if not loop:
        raise LookupError('goal_loop_not_found')
    loop.status = status
    if status in (GoalLoopStatus.PAUSED, GoalLoopStatus.RUNNING):
        # 人工干预重置受阻计数
        loop.stall_count = 0
    if status in (GoalLoopStatus.STOPPED, GoalLoopStatus.DONE):
        loop.finished_at = _naive_utc_now()
    db.session.commit()
    return loop


# ── 多日续航看门狗 ──

def _abandon_task_runtime(task_id: int, reason: str):
    """作废任务在途的运行时凭证（租约/attempt），避免 runtime 继续持有。"""
    from models import AgentTaskAttempt, AgentTaskAttemptState, AgentTaskLease
    AgentTaskLease.query.filter_by(task_id=task_id, active=True).update({'active': False})
    AgentTaskAttempt.query.filter(
        AgentTaskAttempt.task_id == task_id,
        AgentTaskAttempt.state == AgentTaskAttemptState.ACTIVE,
    ).update({'state': AgentTaskAttemptState.ABORTED, 'failure_code': reason[:64]})


def watchdog_sweep(limit=200) -> dict:
    """多日续航巡检（幂等，供定时任务/手动 kick 调用）。

    ① 时长预算耗尽的 RUNNING 循环 → 终态 LIMIT_REACHED；
    ② 卡死轮次：活跃任务超过 N 小时无活动（agent 掉线/租约失效）→ 任务置
       cancelled 并作废在途租约 → 走既有评审兜底（blocked → 受阻计数）；
    ③ 漏触发自愈：RUNNING 且无活跃任务（如重启丢失触发）→ 幂等推进
       （maybe_advance 自带 CAS 与护栏，重复调用安全）。
    """
    now = _naive_utc_now()
    stuck_hours = _stuck_task_hours()
    result = {'checked': 0, 'time_exhausted': 0, 'stuck_cancelled': 0, 'kicked': 0}

    loops = (
        GoalLoop.query.filter(GoalLoop.status == GoalLoopStatus.RUNNING)
        .order_by(GoalLoop.id)
        .limit(limit)
        .all()
    )
    for loop in loops:
        result['checked'] += 1
        db.session.expire(loop)
        if loop.status != GoalLoopStatus.RUNNING:
            continue

        # ① 时长预算
        if _time_budget_exceeded(loop):
            _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                    last_error=f'时长预算 {loop.time_budget_hours} 小时已耗尽，目标未宣告完成')
            result['time_exhausted'] += 1
            continue

        tasks = loop_tasks(loop.id)
        active = [t for t in tasks if t.status in ACTIVE_TASK_STATUSES]

        if active:
            # ② 卡死轮次：最老活跃任务超过阈值小时无活动
            stamps = [t.updated_at or t.created_at for t in active]
            stamps = [s for s in stamps if s is not None]
            if not stamps:
                continue
            idle_hours = (now - min(stamps)).total_seconds() / 3600.0
            if idle_hours < stuck_hours:
                continue
            for t in active:
                _abandon_task_runtime(t.id, 'WATCHDOG_STUCK')
                t.status = TaskStatus.CANCELLED
            db.session.commit()
            result['stuck_cancelled'] += len(active)
            for t in active:
                notify_task_finished(t.id)
            continue

        # ③ 漏触发自愈（无活跃任务时幂等推进）
        kick = maybe_advance(loop.id)
        if kick.get('advanced'):
            result['kicked'] += 1
    return result
