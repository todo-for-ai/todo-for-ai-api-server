"""
GoalLoop 目标循环驱动器

循环机制：任务终态 → notify_task_finished → maybe_advance → 规划器从目标
推导下一步（continue=建下一轮任务并自动派发 / complete=宣告达成 / blocked=
受阻计数），护栏：轮数上限、连续受阻容忍、人工暂停/停止。规划器可插拔：
默认走平台 LLM（feature='goal_loop'），GOAL_LOOP_PLANNER=scripted 时用
确定性脚本规划器（仅供测试/E2E）。

并发防护：推进前 CAS 抢占 advancing 标记（同一循环不并发双发任务）；
LLM 调用期间不持行锁，用户暂停/停止后推进在落库前复核状态即中止。
"""

import json
import os
from datetime import datetime

from models import db, GoalLoop, GoalLoopStatus, Task, TaskStatus, Project, Agent
from api.agent_common import now_utc

TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.CANCELLED}
ACTIVE_TASK_STATUSES = {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.BLOCKED}

DEFAULT_ROUNDS_LIMIT = 10
DEFAULT_STALL_LIMIT = 2


def _naive_utc_now():
    return datetime.utcnow()


# ── 查询助手 ──

def _loop_task_query(loop_id):
    from sqlalchemy import cast, String
    tag = f'{_tag_prefix()}{loop_id}'
    # tags 是 JSON 数组列，按标签字符串包含匹配（量级=单循环轮数，可接受）
    return Task.query.filter(
        Task.project_id.isnot(None),
        cast(Task.tags, String).like(f'%{tag}%'),
    )


def _tag_prefix():
    from models.goal_loop import GOAL_LOOP_TAG_PREFIX
    return GOAL_LOOP_TAG_PREFIX


def loop_tasks(loop_id):
    """循环的全部轮次任务（按创建顺序）。"""
    return _loop_task_query(loop_id).order_by(Task.id).all()


def rounds_done(loop_id) -> int:
    return _loop_task_query(loop_id).count()


def _recent_history(loop, limit=5):
    rows = loop_tasks(loop.id)[-limit:]
    history = []
    for t in rows:
        history.append({
            'title': t.title,
            'status': t.status.value if t.status else None,
        })
    return history


# ── 规划器 ──

def _planner_mode() -> str:
    return (os.getenv('GOAL_LOOP_PLANNER') or 'llm').strip().lower()


def _llm_planner(loop, history) -> dict:
    from services.ai_service import call_llm_production

    system_prompt = (
        '你是目标循环规划器。根据目标、完成标准和最近轮次历史，决定下一步。'
        '只输出 JSON：{"action":"continue|complete|blocked",'
        '"title":"下一轮任务标题(continue时必填)",'
        '"content":"下一轮任务内容(continue时必填,给 agent 的可执行指令)",'
        '"reason":"决策原因或完成总结"}'
    )
    user_prompt = (
        f'目标：{loop.goal_text}\n'
        f'完成标准：{loop.done_definition or "（未明确，由你判断）"}\n'
        f'已进行轮数：{rounds_done(loop.id)}/{loop.rounds_limit}\n'
        f'最近轮次：{json.dumps(history, ensure_ascii=False)}\n'
        f'连续受阻次数：{loop.stall_count}'
    )
    result = call_llm_production(
        feature='goal_loop',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        user_id=loop.created_by or 0,
        use_cache=False,
        temperature=0.4,
        max_tokens=1200,
    )
    if not result.get('success'):
        raise RuntimeError(f"llm_failed: {result.get('error')}")
    parsed = _extract_json(result['data'])
    action = (parsed.get('action') or '').strip().lower()
    if action not in ('continue', 'complete', 'blocked'):
        raise RuntimeError(f'llm_bad_action: {action}')
    if action == 'continue' and not (parsed.get('title') or '').strip():
        raise RuntimeError('llm_continue_without_title')
    return parsed


def _scripted_planner(loop, history) -> dict:
    """确定性脚本规划器：仅供测试/E2E，验证循环机制本身。"""
    target_rounds = int(os.getenv('GOAL_LOOP_SCRIPTED_ROUNDS', '3'))
    done = rounds_done(loop.id)
    if done >= target_rounds:
        return {'action': 'complete', 'reason': f'脚本规划器：已完成 {done} 轮，目标达成'}
    n = done + 1
    return {
        'action': 'continue',
        'title': f'{loop.title} · 第 {n} 轮',
        'content': f'朝目标推进第 {n} 步。目标：{loop.goal_text}',
        'reason': f'脚本规划器第 {n} 轮',
    }


def _call_planner(loop, history) -> dict:
    if _planner_mode() == 'scripted':
        return _scripted_planner(loop, history)
    return _llm_planner(loop, history)


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


# ── 核心推进逻辑 ──

def maybe_advance(loop_id) -> dict:
    """推进一次循环（幂等、并发安全）。返回 {advanced: bool, reason: str}。"""
    loop = db.session.get(GoalLoop, loop_id)
    if not loop:
        return {'advanced': False, 'reason': 'loop_not_found'}

    if loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': f'not_running:{loop.status.value}'}

    # CAS 抢占推进标记，防并发双发
    claimed = GoalLoop.query.filter_by(id=loop_id, advancing=0).update(
        {'advancing': 1}
    )
    db.session.commit()
    if not claimed:
        return {'advanced': False, 'reason': 'already_advancing'}

    try:
        return _advance_locked(loop_id)
    finally:
        try:
            GoalLoop.query.filter_by(id=loop_id).update({'advancing': 0})
            db.session.commit()
        except Exception:
            db.session.rollback()


def _advance_locked(loop_id) -> dict:
    loop = db.session.get(GoalLoop, loop_id)
    if not loop or loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': 'not_running'}

    done = rounds_done(loop.id)
    if done >= loop.rounds_limit:
        _finish(loop, GoalLoopStatus.LIMIT_REACHED,
                last_error=f'轮数上限 {loop.rounds_limit} 已耗尽，目标未宣告完成')
        return {'advanced': False, 'reason': 'rounds_limit'}

    active = [
        t for t in loop_tasks(loop.id)
        if t.status in ACTIVE_TASK_STATUSES
    ]
    if active:
        return {'advanced': False, 'reason': 'active_task_exists'}

    history = _recent_history(loop)
    try:
        decision = _call_planner(loop, history)
    except Exception as exc:  # noqa: BLE001 - 规划器失败归入受阻计数
        return _register_stall(loop, f'planner_failed: {exc}')

    action = decision.get('action')

    # 规划期间用户可能已暂停/停止，落库前复核
    db.session.expire(loop)
    if loop.status != GoalLoopStatus.RUNNING:
        return {'advanced': False, 'reason': 'not_running_after_planner'}

    if action == 'complete':
        loop.status = GoalLoopStatus.DONE
        loop.completion_summary = decision.get('reason') or ''
        loop.stall_count = 0
        loop.finished_at = _naive_utc_now()
        db.session.commit()
        return {'advanced': False, 'reason': 'completed'}

    if action == 'blocked':
        return _register_stall(loop, f"blocked: {decision.get('reason') or '未给出原因'}")

    # continue → 建下一轮任务并自动派发
    task = _create_round_task(loop, decision)
    loop.last_task_id = task.id
    loop.stall_count = 0
    loop.last_error = None
    if loop.started_at is None:
        loop.started_at = _naive_utc_now()
    db.session.commit()

    _auto_assign(task)
    return {'advanced': True, 'reason': 'task_created', 'task_id': task.id}


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


def _create_round_task(loop, decision: dict) -> Task:
    task = Task(
        title=(decision.get('title') or f'{loop.title} · 下一轮').strip()[:500],
        content=(decision.get('content') or decision.get('title') or '').strip(),
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
    except Exception as exc:  # noqa: BLE001 - 派发失败不回滚任务本身
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
            maybe_advance(loop_id)
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
