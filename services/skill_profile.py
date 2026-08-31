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

import json

import structlog

from models import (
    Agent,
    AgentExperience,
    AgentSoulVersion,
    TaskAssignment,
    TaskAssignmentState,
    db,
)
from models.agent_soul_version import MEMORY_KIND_SKILL_PROFILE

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


def _next_memory_version(agent_id: int, memory_kind: str) -> int:
    """某记忆种类当前最大版本号 + 1。"""
    from sqlalchemy import func

    max_version = db.session.query(
        func.coalesce(func.max(AgentSoulVersion.version), 0)
    ).filter_by(agent_id=agent_id, memory_kind=memory_kind).scalar()
    return int(max_version or 0) + 1


def record_memory_version(agent, snapshot: dict, edited_by_user_id: int,
                          change_summary: str = '') -> AgentSoulVersion:
    """技能画像版本快照入库（P3.4 记忆治理，复用 AgentSoulVersion 表）。"""
    version_row = AgentSoulVersion(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        version=_next_memory_version(agent.id, MEMORY_KIND_SKILL_PROFILE),
        memory_kind=MEMORY_KIND_SKILL_PROFILE,
        soul_markdown='',
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        change_summary=(change_summary or 'rebuild')[:255],
        edited_by_user_id=int(edited_by_user_id),
        created_by=f'user:{edited_by_user_id}',
    )
    db.session.add(version_row)
    return version_row


def rebuild_skill_profile(agent_id: int, edited_by_user_id: int,
                          change_summary: str = '') -> dict:
    """重建并持久化 Agent 技能画像，返回画像 dict。

    每次重建写入一条版本快照（P3.4）。
    """
    agent = db.session.get(Agent, agent_id)
    if not agent:
        raise ValueError(f'agent {agent_id} not found')

    profile = build_skill_profile(agent)
    agent.skill_profile = profile
    agent.skill_profile_updated_at = datetime.utcnow()
    record_memory_version(agent, profile, edited_by_user_id=edited_by_user_id,
                          change_summary=change_summary or 'rebuild')
    db.session.commit()

    logger.info(
        "skill_profile.rebuilt",
        agent_id=agent_id,
        skill_count=len(profile['skills']),
    )
    return profile


def forget_skill_profile(agent_id: int, edited_by_user_id: int) -> dict:
    """遗忘技能画像（P3.4 可遗忘）：清空画像并写入墓碑版本快照。"""
    agent = db.session.get(Agent, agent_id)
    if not agent:
        raise ValueError(f'agent {agent_id} not found')

    forgotten = {
        'skills': [],
        'assignments': {'completed': 0, 'failed': 0},
        'experience_count': 0,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'forgotten': True,
    }
    agent.skill_profile = None
    agent.skill_profile_updated_at = None
    record_memory_version(
        agent, forgotten,
        edited_by_user_id=edited_by_user_id,
        change_summary='forgotten (right to erasure)',
    )
    db.session.commit()

    logger.info("skill_profile.forgotten", agent_id=agent_id)
    return forgotten


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
