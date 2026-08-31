"""Agent 技能画像服务（P3.1 SOUL v2）

把 Agent 的运行历史自动沉淀为结构化技能画像：
- 数据源 1：AgentExperience（domain / task_type / capabilities_used × 成功/失败经验类型）
- 数据源 2：TaskAssignment（完成 / 失败 / 总量）

画像持久化在 agents.skill_profile（JSON），供：
- 派单打分（score_task_for_agent 的 skill_profile_bonus）
- 前端 / API 展示 Agent「擅长什么、成功率如何」
- SOUL 工作档案化的地基（后续与 soul_markdown 合流）

重建是纯聚合、幂等的；不改写人工维护的 capabilities 列表。
"""

from datetime import datetime

import structlog

from models import (
    Agent,
    AgentExperience,
    TaskAssignment,
    TaskAssignmentState,
    db,
)

logger = structlog.get_logger()

# 经验类型 → 成败归类
SUCCESS_EXPERIENCE_TYPES = ('success_pattern', 'strategy', 'optimization')
FAILURE_EXPERIENCE_TYPES = ('failure_pattern', 'anti_pattern')

# 画像最多保留的技能条数（按出现次数排序后截断）
MAX_SKILLS = 20


def _empty_stats():
    return {'success': 0, 'failure': 0}


def _record(stats: dict, is_success: bool):
    stats['success' if is_success else 'failure'] += 1


def _is_success_experience(experience_type) -> bool:
    exp_type = str(experience_type or '').strip().lower()
    if exp_type in SUCCESS_EXPERIENCE_TYPES:
        return True
    if exp_type in FAILURE_EXPERIENCE_TYPES:
        return False
    return True  # 未知类型按中性/正向计


def _finalize_skills(buckets: dict) -> list:
    skills = []
    for (name, kind), stats in buckets.items():
        total = stats['success'] + stats['failure']
        skills.append({
            'name': name,
            'kind': kind,
            'count': total,
            'success_rate': round(stats['success'] / total, 3) if total else None,
        })
    skills.sort(key=lambda item: (-item['count'], item['name']))
    return skills[:MAX_SKILLS]


def build_skill_profile(agent) -> dict:
    """聚合 Agent 的技能画像（不落库）。"""
    buckets: dict = {}

    experiences = AgentExperience.query.filter_by(
        agent_id=agent.id, is_valid=True,
    ).all()
    for exp in experiences:
        success = _is_success_experience(exp.experience_type)
        if exp.domain:
            _record(buckets.setdefault((str(exp.domain).strip().lower(), 'domain'), _empty_stats()), success)
        if exp.task_type:
            _record(buckets.setdefault((str(exp.task_type).strip().lower(), 'task_type'), _empty_stats()), success)
        for cap in (exp.capabilities_used or []):
            name = str(cap).strip().lower()
            if name:
                _record(buckets.setdefault((name, 'capability'), _empty_stats()), success)

    completed = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent.id,
        TaskAssignment.state == TaskAssignmentState.DONE,
    ).count()
    failed = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent.id,
        TaskAssignment.state == TaskAssignmentState.FAILED,
    ).count()

    return {
        'skills': _finalize_skills(buckets),
        'assignments': {'completed': int(completed), 'failed': int(failed)},
        'experience_count': len(experiences),
        'generated_at': datetime.utcnow().isoformat() + 'Z',
    }


def rebuild_skill_profile(agent_id: int) -> dict:
    """重建并持久化 Agent 技能画像，返回画像 dict。"""
    agent = db.session.get(Agent, agent_id)
    if not agent:
        raise ValueError(f'agent {agent_id} not found')

    profile = build_skill_profile(agent)
    agent.skill_profile = profile
    agent.skill_profile_updated_at = datetime.utcnow()
    db.session.commit()

    logger.info(
        "skill_profile.rebuilt",
        agent_id=agent_id,
        skill_count=len(profile['skills']),
    )
    return profile


def skill_profile_bonus(agent, matched_terms) -> int:
    """派单打分加分：画像技能命中任务匹配词时给小幅加权（cap 20）。

    matched_terms 应传入已归一化的任务侧命中集合（tags + 文本命中）。
    """
    profile = getattr(agent, 'skill_profile', None)
    if not profile or not isinstance(profile, dict):
        return 0
    matched = {str(term).strip().lower() for term in (matched_terms or []) if term}
    if not matched:
        return 0
    bonus = 0
    for skill in profile.get('skills', []):
        if str(skill.get('name') or '').strip().lower() in matched:
            bonus += 4
    return min(bonus, 20)
