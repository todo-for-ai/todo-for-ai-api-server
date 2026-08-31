"""洞察落地为动作（P3.3）

把既有分析维度从看板接到行动面：
1. 负载预测 → 派单节流：
   从 TaskAssignment 历史预测 Agent 未来负载（吞吐/积压天数），
   超载 Agent 在派单打分中被降权（score_task_for_agent 接入），
   并可在派单预览/Agent 详情中查看预测明细。
2. 返工分析 → DoD 模板推荐：
   统计项目内自愈修复子任务（creator_identifier=recovery:<类别>）的
   返工类别分布，映射为推荐的 DoD 验收模板；确认后可一键合并进任务 DoD。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import func

import structlog

from models import (
    Task,
    TaskAssignment,
    TaskAssignmentState,
    db,
)

logger = structlog.get_logger()

# 预测积压超过该天数视为超载（节流阈值）
OVERLOAD_BACKLOG_DAYS = 3.0
# 超载时的打分扣减
OVERLOAD_SCORE_PENALTY = 60
# 统计吞吐的回看窗口
DEFAULT_WINDOW_DAYS = 7


def predict_agent_load(agent, window_days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    """基于执行历史预测 Agent 当前负载。

    - active: 进行中的任务分配数
    - completed_window: 窗口内完成任务数 → 吞吐（件/天）
    - projected_backlog_days: active / 吞吐（吞吐为 0 且有积压 → ∞ 用大数表示）
    - overloaded: 积压天数 ≥ OVERLOAD_BACKLOG_DAYS
    """
    now = datetime.utcnow()
    since = now - timedelta(days=window_days)

    active = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent.id,
        TaskAssignment.state.in_((
            TaskAssignmentState.ASSIGNED,
            TaskAssignmentState.CLAIMED,
            TaskAssignmentState.RUNNING,
            TaskAssignmentState.WAITING_HUMAN,
            TaskAssignmentState.REVIEW,
        )),
    ).count()

    completed_window = TaskAssignment.query.filter(
        TaskAssignment.agent_id == agent.id,
        TaskAssignment.state == TaskAssignmentState.DONE,
        TaskAssignment.completed_at >= since,
    ).count()

    throughput_per_day = completed_window / float(window_days)
    if active <= 0:
        projected_backlog_days = 0.0
    elif throughput_per_day <= 0:
        projected_backlog_days = 999.0  # 有积压但窗口内零完成：无排空迹象
    else:
        projected_backlog_days = round(active / throughput_per_day, 2)

    overloaded = projected_backlog_days >= OVERLOAD_BACKLOG_DAYS

    return {
        'active_assignments': int(active),
        'completed_window': int(completed_window),
        'window_days': int(window_days),
        'throughput_per_day': round(throughput_per_day, 3),
        'projected_backlog_days': projected_backlog_days,
        'overloaded': overloaded,
    }


def compute_load_throttle(agent, window_days: int = DEFAULT_WINDOW_DAYS) -> tuple:
    """派单节流：返回 (扣减分, 负载预测明细)。未超载时扣减为 0。"""
    load = predict_agent_load(agent, window_days=window_days)
    penalty = OVERLOAD_SCORE_PENALTY if load['overloaded'] else 0
    return penalty, load


# ── 返工分析 → DoD 模板推荐 ─────────────────────────────────────────────

# 返工类别 → 推荐 DoD 验收模板（type 为 commit 协议可执行类型 + manual）
CATEGORY_DOD_TEMPLATES: Dict[str, List[Dict[str, str]]] = {
    'test_failure': [
        {'type': 'test', 'value': 'run project test suite'},
    ],
    'build_failure': [
        {'type': 'build', 'value': 'build project'},
    ],
    'lint_failure': [
        {'type': 'lint', 'value': 'run linter'},
    ],
    'timeout': [
        {'type': 'manual', 'value': '长任务需拆分并上报进度'},
    ],
}


def analyze_project_rework(project_id: int, days: int = 30) -> Dict[str, Any]:
    """按返工类别统计项目内自愈修复子任务（P2.3 自愈回流的返工信号）。"""
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        db.session.query(Task.creator_identifier, func.count(Task.id))
        .filter(
            Task.project_id == project_id,
            Task.parent_task_id.isnot(None),
            Task.creator_identifier.like('recovery:%'),
            Task.created_at >= since,
        )
        .group_by(Task.creator_identifier)
        .all()
    )
    category_counts: Dict[str, int] = {}
    for identifier, count in rows:
        category = str(identifier or '').split(':', 1)[1] or 'unknown'
        category_counts[category] = category_counts.get(category, 0) + int(count)

    total_repairs = sum(category_counts.values())
    return {
        'days': int(days),
        'total_repair_tasks': total_repairs,
        'by_category': dict(sorted(category_counts.items(), key=lambda kv: -kv[1])),
    }


def recommend_dod_templates(project_id: int, days: int = 30) -> Dict[str, Any]:
    """返工类别 → 推荐的 DoD 模板（含模板是否已覆盖任务/项目不可知，仅给建议）。"""
    rework = analyze_project_rework(project_id, days=days)

    recommendations = []
    for category, count in rework['by_category'].items():
        template = CATEGORY_DOD_TEMPLATES.get(category)
        recommendations.append({
            'category': category,
            'rework_count': count,
            'recommended': bool(template),
            'dod_template': template or [],
        })
    return {
        'project_id': project_id,
        'rework': rework,
        'recommendations': recommendations,
        'has_recommendations': any(item['recommended'] for item in recommendations),
    }


def apply_dod_template(task, categories: List[str]) -> Dict[str, Any]:
    """把推荐模板合并进任务 DoD（去重：type 相同且 value 相同的不重复加）。

    返回更新后的 dod 列表。
    """
    existing = list(task.dod or [])
    existing_keys = {
        (str(item.get('type') or '').strip().lower(),
         str(item.get('value') or '').strip().lower())
        for item in existing
    }
    added = []
    for category in categories:
        for item in CATEGORY_DOD_TEMPLATES.get(category, []):
            key = (item['type'], item['value'].strip().lower())
            if key in existing_keys:
                continue
            existing.append({'type': item['type'], 'value': item['value']})
            existing_keys.add(key)
            added.append(item)
    if added:
        task.dod = existing
        db.session.commit()
    return {'dod': task.dod or [], 'added': added}


# ── 知识传播网络 → 导师制编排 ────────────────────────────────────────────

def _agent_knowledge_coverage(workspace_id: int, since: datetime) -> Dict[int, Dict[str, Any]]:
    """统计工作区内每个 Agent 的知识产出/覆盖：经验数、复用次数、领域集合。"""
    from models import Agent, AgentExperience, AgentStatus

    agents = Agent.query.filter_by(workspace_id=workspace_id).filter(
        Agent.status != AgentStatus.DISABLED,
    ).all()

    rows = (
        db.session.query(
            AgentExperience.agent_id,
            AgentExperience.domain,
            AgentExperience.times_reused,
        )
        .join(Agent, AgentExperience.agent_id == Agent.id)
        .filter(
            Agent.workspace_id == workspace_id,
            AgentExperience.is_valid.is_(True),
            AgentExperience.created_at >= since,
        )
        .all()
    )
    coverage: Dict[int, Dict[str, Any]] = {
        agent.id: {'agent': agent, 'exps': 0, 'reuses': 0, 'domains': set()}
        for agent in agents
    }
    for agent_id, domain, reused in rows:
        c = coverage.get(agent_id)
        if not c:
            continue
        c['exps'] += 1
        c['reuses'] += reused or 0
        if domain:
            c['domains'].add(str(domain).strip().lower())
    return coverage


def recommend_mentorship_pairs(workspace_id: int, limit: int = 5,
                               window_days: int = 90) -> Dict[str, Any]:
    """知识传播网络 → 导师制编排建议。

    高产出知识 Agent（经验多/被复用多）与低覆盖 Agent（无/少经验）
    按领域缺口配对：学徒缺什么、导师就擅长什么。
    """
    since = datetime.utcnow() - timedelta(days=window_days)
    coverage = _agent_knowledge_coverage(workspace_id, since)

    mentors_pool = sorted(
        (c for c in coverage.values() if c['exps'] > 0),
        key=lambda c: (-c['reuses'], -c['exps']),
    )
    mentees_pool = sorted(
        (c for c in coverage.values() if c['exps'] == 0),
        key=lambda c: c['agent'].name or '',
    )

    suggestions = []
    used_mentor_domains: Dict[int, set] = {}
    for mentee in mentees_pool:
        for mentor in mentors_pool:
            if mentor['agent'].id == mentee['agent'].id:
                continue
            taken = used_mentor_domains.setdefault(mentor['agent'].id, set())
            # 导师尚未被占用的优势领域（学徒该领域零覆盖）
            free_domains = [
                d for d in sorted(mentor['domains'])
                if d not in taken and d not in mentee['domains']
            ]
            if not free_domains:
                continue
            domain = free_domains[0]
            taken.add(domain)
            suggestions.append({
                'mentor_id': mentor['agent'].id,
                'mentor_name': mentor['agent'].name,
                'mentee_id': mentee['agent'].id,
                'mentee_name': mentee['agent'].name,
                'domain': domain,
                'mentor_exps': mentor['exps'],
                'mentor_reuses': mentor['reuses'],
                'reason': (
                    f"导师在「{domain}」有 {mentor['exps']} 条有效经验"
                    f"（被复用 {mentor['reuses']} 次），学徒该领域零覆盖"
                ),
            })
            if len(suggestions) >= limit:
                return {
                    'workspace_id': workspace_id,
                    'suggestions': suggestions,
                    'has_suggestions': True,
                }
    return {
        'workspace_id': workspace_id,
        'suggestions': suggestions,
        'has_suggestions': bool(suggestions),
    }


def apply_mentorship_pair(mentor, mentee, domain: str, created_by_user_id: int) -> Dict[str, Any]:
    """确认导师制建议 → 落地为协作动作：

    1. 创建（或复用）双成员团队「导师制:<mentor>→<mentee>:<domain>」，
       导师 LEADER / 学徒 MEMBER；
    2. 把导师该领域的历史经验置为 is_shared，学徒侧复用引擎立即可见。

    幂等：同名团队已存在且配置匹配时直接返回既有团队。
    """
    from models import AgentExperience, AgentTeam, AgentTeamMember, AgentTeamMemberRole, AgentTeamStatus

    domain = str(domain or '').strip().lower()
    team_name = f"导师制:{mentor.name}→{mentee.name}:{domain}"[:128]

    existing = AgentTeam.query.filter_by(workspace_id=mentor.workspace_id, name=team_name).first()
    if existing:
        return {
            'team_id': existing.id, 'team_name': existing.name,
            'created': False, 'shared_experiences': 0,
        }

    team = AgentTeam(
        workspace_id=mentor.workspace_id,
        created_by_user_id=created_by_user_id,
        name=team_name,
        description=f"知识传播网络驱动的导师制编排：{mentor.name} 在「{domain}」带教 {mentee.name}",
        config={'mentorship': {
            'mentor_id': mentor.id, 'mentee_id': mentee.id, 'domain': domain,
        }},
        status=AgentTeamStatus.ACTIVE,
    )
    db.session.add(team)
    db.session.flush()

    db.session.add(AgentTeamMember(
        team_id=team.id, agent_id=mentor.id,
        added_by_user_id=created_by_user_id,
        role=AgentTeamMemberRole.LEADER,
    ))
    db.session.add(AgentTeamMember(
        team_id=team.id, agent_id=mentee.id,
        added_by_user_id=created_by_user_id,
        role=AgentTeamMemberRole.MEMBER,
    ))

    # 协作动作：导师该领域经验共享（学徒侧复用/检索立即可见）
    shared_count = AgentExperience.query.filter(
        AgentExperience.agent_id == mentor.id,
        AgentExperience.is_valid.is_(True),
        AgentExperience.is_shared.is_(False),
        func.lower(AgentExperience.domain) == domain,
    ).update({'is_shared': True}, synchronize_session=False)

    db.session.commit()
    logger.info("mentorship.applied", team_id=team.id,
                mentor_id=mentor.id, mentee_id=mentee.id, domain=domain,
                shared_experiences=int(shared_count))
    return {
        'team_id': team.id, 'team_name': team.name,
        'created': True, 'shared_experiences': int(shared_count),
    }
