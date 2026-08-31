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
