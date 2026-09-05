"""
GoalLoop 目标循环驱动器（v2：计划式拆解）

循环机制：创建循环 → 规划器把目标**拆解成有序计划**（steps）→ 逐轮把计划
步骤物化为任务并自动派发 → 任务成功则直接执行下一步（省一次评审调用），
任务失败或计划耗尽则触发**评审**（继续扩展计划/重排计划/宣告完成/受阻）。

护栏：轮数上限、连续受阻容忍、人工暂停/停止/kick。并发防护：advancing
CAS 标记，同一循环不并发双发；LLM 调用期间不持锁，落库前复核状态。

规划器可插拔：默认走平台 LLM（feature='goal_loop'），并注入执行 Agent 的
岗位角色上下文（agent.role_template）；GOAL_LOOP_PLANNER=scripted 时使用
确定性脚本规划器（仅供测试/E2E）。
"""

import json
import os
from datetime import datetime

from models import db, GoalLoop, GoalLoopStatus, Task, TaskStatus, Project, Agent

TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.CANCELLED}
ACTIVE_TASK_STATUSES = {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED}

DEFAULT_ROUNDS_LIMIT = 10
DEFAULT_STALL_LIMIT = 2


def _naive_utc_now():
    return datetime.utcnow()


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
    """执行 Agent 的岗位角色上下文（来自绑定的角色模板）。"""
    template = agent.role_template if agent else None
    if not template:
        return {'role': None, 'role_description': None}
    return {
        'role': template.display_name or template.name,
        'role_category': template.category,
        'role_description': (template.description or '')[:500],
    }


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
    """把目标拆解成有序计划步骤。"""
    role = _role_context(loop.agent)
    role_line = ''
    if role.get('role'):
        role_line = f"执行者角色：{role['role']}（{role.get('role_category') or ''}）{role.get('role_description') or ''}\n"

    system_prompt = (
        '你是目标循环规划器。把目标拆解为有序的执行步骤（计划）。'
        '只输出 JSON：{"steps": [{"title": "步骤标题", "content": "给 Agent 的可执行指令"}]}，'
        '步骤数量不超过轮数上限，最后一步应包含验收/收尾。'
    )
    user_prompt = (
        f'{role_line}'
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
    role = _role_context(loop.agent)
    role_line = f"执行者角色：{role.get('role')}\n" if role.get('role') else ''

    system_prompt = (
        '你是目标循环评审器。根据目标、完成标准和执行历史决定下一步。'
        '只输出 JSON：{"action": "complete|extend|blocked", '
        '"steps": [{"title": "...", "content": "..."}]（action=extend 时必填，为剩余计划）, '
        '"reason": "决策原因或完成总结"}'
    )
    user_prompt = (
        f'{role_line}'
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
        _auto_assign(task)
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
        _auto_assign(task)
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


def _create_round_task(loop, step: dict) -> Task:
    role = _role_context(loop.agent)
    content = (step.get('content') or step.get('title') or '').strip()
    role_line = f"【执行角色：{role['role']}】\n" if role.get('role') else ''
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


def _auto_assign(task):
    try:
        from services.agent_runtime_controller import AgentRuntimeController
        AgentRuntimeController.auto_assign_task(task)
    except Exception:
        db.session.rollback()


# ── 对外操作 ──

def create_loop(*, project: Project, agent: Agent, title: str, goal_text: str,
                done_definition: str = None, rounds_limit: int = DEFAULT_ROUNDS_LIMIT,
                created_by: int = None) -> GoalLoop:
    loop = GoalLoop(
        workspace_id=project.organization_id,
        project_id=project.id,
        agent_id=agent.id,
        title=title.strip()[:500],
        goal_text=goal_text,
        done_definition=(done_definition or '').strip() or None,
        status=GoalLoopStatus.RUNNING,
        rounds_limit=max(1, int(rounds_limit or DEFAULT_ROUNDS_LIMIT)),
        stall_limit=DEFAULT_STALL_LIMIT,
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
