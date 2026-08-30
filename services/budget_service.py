"""
预算校验服务（P2.6）

任务派发（agent_runtime_pull）与 AgentRun 创建（agent_trigger_engine）路径
强制校验 Budget 上限；超限写入 interaction_request 审批事件（budget_exceeded）
并记录审计，同一预算在同一周期内幂等告警一次。

用量数据源：
- duration_minutes：agent_runs 的 started_at/ended_at 累计（周期窗口内）
- concurrent：active 租约数（即时值，不受周期影响）
- tokens：AIRequestLog 的 total_tokens 累计（仅 workspace 级统计：
  AIRequestLog 目前只有 user 维度，agent/project 级 token 预算视为未跟踪，
  校验时跳过并在结果中标注 not_tracked）
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func

import structlog

from models import (
    AgentRun,
    AgentRunState,
    AgentTaskEvent,
    AgentTaskLease,
    AIRequestLog,
    Budget,
    db,
)

INTERACTION_REQUEST_EVENT_TYPE = 'interaction_request'

# Budget.resource -> AgentRunState 汇总策略
CONCURRENT_STATES = (AgentRunState.QUEUED.value, AgentRunState.LEASED.value, AgentRunState.RUNNING.value)

logger = structlog.get_logger()


def period_start(period: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """计算周期窗口起点；total 返回 None（不限）。"""
    now = now or datetime.utcnow()
    if period == 'total':
        return None
    if period == 'daily':
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'weekly':
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'monthly':
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def _token_usage(workspace_id: int, start: Optional[datetime]) -> int:
    """workspace 级 Token 用量：workspace 用户集合内的 AIRequestLog 累计。"""
    from models import Organization, OrganizationMember

    org = db.session.get(Organization, workspace_id)
    member_ids = [
        row[0] for row in db.session.query(OrganizationMember.user_id).filter_by(
            organization_id=workspace_id
        ).all()
    ]
    if org and org.owner_id and org.owner_id not in member_ids:
        member_ids.append(org.owner_id)
    query = db.session.query(func.coalesce(func.sum(AIRequestLog.total_tokens), 0))
    if member_ids:
        query = query.filter(AIRequestLog.user_id.in_(member_ids))
    else:
        return 0
    if start:
        query = query.filter(AIRequestLog.created_at >= start)
    return int(query.scalar() or 0)


def get_usage(budget: Budget, now: Optional[datetime] = None) -> Dict[str, Any]:
    """计算单个预算的当前用量。返回 {used, not_tracked}。"""
    now = now or datetime.utcnow()
    start = period_start(budget.period, now)

    if budget.resource == 'tokens':
        if budget.scope_type != 'workspace':
            return {'used': 0, 'not_tracked': True}
        return {'used': _token_usage(budget.workspace_id, start), 'not_tracked': False}

    if budget.resource == 'duration_minutes':
        rows = (
            db.session.query(AgentRun.started_at, AgentRun.ended_at)
            .filter(
                AgentRun.workspace_id == budget.workspace_id,
                AgentRun.started_at.isnot(None),
                AgentRun.ended_at.isnot(None),
            )
        )
        if budget.scope_type == 'agent' and budget.agent_id:
            rows = rows.filter(AgentRun.agent_id == budget.agent_id)
        if start:
            rows = rows.filter(AgentRun.started_at >= start)
        # 行级秒累计：跨方言可移植（SQLite/MySQL 一致），上限 500 行防全表扫描
        total_seconds = sum(
            max((e - s_).total_seconds(), 0)
            for s_, e in rows.limit(500).all()
        )
        return {'used': int(total_seconds // 60), 'not_tracked': False}

    if budget.resource == 'concurrent':
        query = db.session.query(func.count(AgentTaskLease.id)).filter(
            AgentTaskLease.workspace_id == budget.workspace_id,
            AgentTaskLease.active.is_(True),
        )
        if budget.scope_type == 'agent' and budget.agent_id:
            query = query.filter(AgentTaskLease.agent_id == budget.agent_id)
        return {'used': int(query.scalar() or 0), 'not_tracked': False}

    return {'used': 0, 'not_tracked': True}


def _applicable_budgets(workspace_id: int, agent_id: Optional[int], project_id: Optional[int]):
    query = Budget.query.filter_by(workspace_id=workspace_id, is_active=True)
    conditions = [Budget.scope_type == 'workspace']
    if agent_id:
        conditions.append(
            (Budget.scope_type == 'agent') & (Budget.agent_id == agent_id)
        )
    if project_id:
        conditions.append(
            (Budget.scope_type == 'project') & (Budget.project_id == project_id)
        )
    from sqlalchemy import or_
    return query.filter(or_(*conditions)).all()


def check_budgets(workspace_id: int, agent_id: Optional[int] = None,
                  project_id: Optional[int] = None, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """校验 workspace/agent/project 三个维度适用的全部预算。

    返回超限列表：[{budget_id, scope_type, resource, period, limit, used}]。
    """
    violations = []
    for budget in _applicable_budgets(workspace_id, agent_id, project_id):
        usage = get_usage(budget, now=now)
        if usage.get('not_tracked'):
            continue
        if usage['used'] >= budget.limit_value:
            violations.append({
                'budget_id': budget.id,
                'scope_type': budget.scope_type,
                'resource': budget.resource,
                'period': budget.period,
                'limit': budget.limit_value,
                'used': usage['used'],
            })
    return violations


def _has_pending_budget_event(workspace_id: int, budget_id: int) -> bool:
    """同一预算是否已有未决（pending_approval）超限事件（幂等告警）。"""
    rows = (
        AgentTaskEvent.query
        .filter(
            AgentTaskEvent.workspace_id == workspace_id,
            AgentTaskEvent.event_type == INTERACTION_REQUEST_EVENT_TYPE,
        )
        .order_by(AgentTaskEvent.id.desc())
        .limit(100)
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        if (
            payload.get('interaction_type') == 'budget_exceeded'
            and payload.get('budget_id') == budget_id
            and payload.get('status') == 'pending_approval'
        ):
            return True
    return False


def raise_budget_exceeded(workspace_id: int, violations: List[Dict[str, Any]],
                          context: Optional[Dict[str, Any]] = None) -> List[str]:
    """为每个超限预算写入 interaction_request 事件（幂等）+ 审计。

    返回本次新建的 interaction_id 列表。
    """
    import uuid
    from datetime import datetime as dt

    from api.agent_common import write_agent_audit

    created = []
    context = context or {}
    now = dt.utcnow()
    task_id = context.get('task_id')
    if not task_id:
        raise ValueError('budget exceeded events require a task_id (AgentTaskEvent FK)')
    for violation in violations:
        budget_id = violation.get('budget_id')
        if _has_pending_budget_event(workspace_id, budget_id):
            continue

        interaction_id = f"budg-{uuid.uuid4().hex[:12]}"
        payload = {
            'interaction_id': interaction_id,
            'interaction_type': 'budget_exceeded',
            'status': 'pending_approval',
            'budget_id': budget_id,
            'resource': violation.get('resource'),
            'period': violation.get('period'),
            'limit': violation.get('limit'),
            'used': violation.get('used'),
            'scope_type': violation.get('scope_type'),
            'governance': {'requires_approval': True, 'risk_tier': 'high'},
            'context': context,
            'requested_at': now.isoformat(),
        }
        db.session.add(AgentTaskEvent(
            task_id=int(task_id),
            attempt_id='',
            agent_id=context.get('agent_id'),
            workspace_id=int(workspace_id),
            event_type=INTERACTION_REQUEST_EVENT_TYPE,
            seq=1,
            event_timestamp=now,
            payload=payload,
            message=f"budget exceeded {interaction_id} {violation.get('resource')} "
                    f"{violation.get('used')}/{violation.get('limit')}",
            created_by='system:budget',
        ))
        try:
            # write_agent_audit 读取请求头；触发引擎等非请求上下文下降级为日志
            write_agent_audit(
                event_type='budget.exceeded',
                actor_type='system',
                actor_id=0,
                target_type='budget',
                target_id=budget_id,
                workspace_id=workspace_id,
                payload=violation,
                risk_score=30,
            )
        except RuntimeError:
            logger.warning("budget.exceeded_audit_no_request_context", violation=violation)
        created.append(interaction_id)
    if created:
        db.session.commit()
    return created
