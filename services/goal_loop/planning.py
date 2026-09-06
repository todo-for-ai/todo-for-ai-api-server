"""规划器：指挥者视角的目标拆解与评审（LLM / scripted 双实现）。"""

import json
import os

from models import Agent

from .constants import naive_utc_now
from .dispatch import available_executor_roles, director, role_context


def planner_mode() -> str:
    return (os.getenv('GOAL_LOOP_PLANNER') or 'llm').strip().lower()


def extract_json(raw):
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


def valid_steps(steps, max_steps) -> bool:
    return (
        isinstance(steps, list)
        and 1 <= len(steps) <= max_steps
        and all(isinstance(s, dict) and (s.get('title') or '').strip() for s in steps)
    )


def llm_call(loop, system_prompt: str, user_prompt: str) -> dict:
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
    return extract_json(result['data'])


def budget_line(loop) -> str:
    """给规划器/评审器的剩余时间预算上下文。"""
    if not loop.time_budget_hours:
        return ''
    if not loop.started_at:
        return f'时长预算：{loop.time_budget_hours} 小时（尚未开跑）\n'
    from .constants import naive_utc_now
    elapsed = (naive_utc_now() - loop.started_at).total_seconds() / 3600.0
    remaining = max(0.0, loop.time_budget_hours - elapsed)
    return f'剩余时间预算：{remaining:.1f}/{loop.time_budget_hours} 小时\n'


def decompose(loop) -> list:
    """指挥者把目标拆解成有序计划步骤（每步可指名执行岗位）。"""
    role = role_context(director(loop))
    role_line = ''
    if role.get('role'):
        role_line = f"指挥者角色：{role['role']}（{role.get('role_category') or ''}）{role.get('role_description') or ''}\n"
    roles = available_executor_roles(loop)
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
        f'{budget_line(loop)}'
        f'目标：{loop.goal_text}\n'
        f'完成标准：{loop.done_definition or "（未明确，由你判断）"}\n'
        f'轮数上限：{loop.rounds_limit}'
    )
    parsed = llm_call(loop, system_prompt, user_prompt)
    steps = parsed.get('steps') if isinstance(parsed, dict) else None
    if not valid_steps(steps, loop.rounds_limit):
        raise RuntimeError('llm_bad_plan')
    return steps


def review(loop, last_status: str) -> dict:
    """计划耗尽或上轮失败后的评审：扩展/重排计划、宣告完成或受阻。"""
    role = role_context(director(loop))
    role_line = f"指挥者角色：{role.get('role')}\n" if role.get('role') else ''
    roles = available_executor_roles(loop)
    roles_line = f"可用执行者角色：{'、'.join(roles)}\n" if roles else ''

    system_prompt = (
        '你是目标循环的评审器（指挥者）。根据目标、完成标准和执行历史决定下一步。'
        '只输出 JSON：{"action": "complete|extend|blocked", '
        '"steps": [{"title": "...", "content": "...", "role": "执行岗位（可选）"}]'
        '（action=extend 时必填，为剩余计划）, '
        '"reason": "决策原因或完成总结"}'
    )
    from .query import rounds_done, recent_history
    user_prompt = (
        f'{role_line}'
        f'{roles_line}'
        f'{budget_line(loop)}'
        f'目标：{loop.goal_text}\n'
        f'完成标准：{loop.done_definition or "（未明确，由你判断）"}\n'
        f'已执行轮数：{rounds_done(loop.id)}/{loop.rounds_limit}\n'
        f'最近轮次：{json.dumps(recent_history(loop), ensure_ascii=False)}\n'
        f'上一轮状态：{last_status or "未知"}'
    )
    parsed = llm_call(loop, system_prompt, user_prompt)
    action = (parsed.get('action') or '').strip().lower()
    if action not in ('complete', 'extend', 'blocked'):
        raise RuntimeError(f'llm_bad_action: {action}')
    if action == 'extend' and not valid_steps(parsed.get('steps'), loop.rounds_limit):
        raise RuntimeError('llm_extend_without_steps')
    return parsed


def scripted_decompose(loop) -> list:
    target = int(os.getenv('GOAL_LOOP_SCRIPTED_ROUNDS', '3'))
    return [
        {
            'title': f'{loop.title} · 计划步骤 {i}',
            'content': f'朝目标推进第 {i}/{target} 步。目标：{loop.goal_text}',
        }
        for i in range(1, target + 1)
    ]


def scripted_review(loop, last_status: str) -> dict:
    from .query import rounds_done
    if last_status == 'done':
        return {
            'action': 'complete',
            'reason': f'脚本规划器：已完成 {rounds_done(loop.id)} 轮，目标达成',
        }
    return {'action': 'blocked', 'reason': f'脚本规划器：上轮状态 {last_status}，无法推进'}


def call_decompose(loop) -> list:
    if planner_mode() == 'scripted':
        return scripted_decompose(loop)
    return decompose(loop)


def call_review(loop, last_status: str) -> dict:
    if planner_mode() == 'scripted':
        return scripted_review(loop, last_status)
    return review(loop, last_status)
