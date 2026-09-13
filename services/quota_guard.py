"""额度熔断（quota guard）：LLM API token 额度/计费耗尽的优雅停车。

这是自托管平台最常见的资源级故障：用户配置的 API token 没额度了。
语义与普通任务失败完全不同——重试无意义、继续派发只会烧出更多失败，
必须：①识别（归因 quota_exhausted，见 failure_recovery）；②熔断该
Agent 的派发（pull / auto_assign / goal_loop dispatch 三道门）；
③上报用户（interaction_request 事件，审批队列/open 协议可见）。

熔断窗口：自最近一次上报起 QUOTA_BLOCK_WINDOW_HOURS（默认 24h）内
不再给该 Agent 派发；窗口过后自动恢复（用户换了 key/充了值即自愈）。
"""

import os
import uuid
from datetime import datetime, timedelta

QUOTA_BLOCK_WINDOW_HOURS_DEFAULT = 24


def _window_hours() -> int:
    raw = os.environ.get('QUOTA_BLOCK_WINDOW_HOURS')
    if not raw:
        return QUOTA_BLOCK_WINDOW_HOURS_DEFAULT
    try:
        return max(1, min(24 * 30, int(raw)))
    except (TypeError, ValueError):
        return QUOTA_BLOCK_WINDOW_HOURS_DEFAULT


def has_pending_quota_block(agent_id) -> bool:
    """该 Agent 最近一个熔断窗口内是否有未解决的额度耗尽上报。"""
    if agent_id is None:
        return False
    try:
        from models import AgentTaskEvent

        since = datetime.utcnow() - timedelta(hours=_window_hours())
        row = (
            AgentTaskEvent.query
            .filter(
                AgentTaskEvent.agent_id == int(agent_id),
                AgentTaskEvent.event_type == 'interaction_request',
                AgentTaskEvent.created_at >= since,
                AgentTaskEvent.payload['interaction_type'].as_string() == 'token_quota_exhausted',
            )
            .first()
        )
        return row is not None
    except Exception:  # noqa: BLE001 - 熔断查询失败绝不阻断派发主链路
        return False


def raise_quota_exhausted(workspace_id, task, agent=None,
                          category: str = 'quota_exhausted',
                          failure_reason: str = '') -> dict:
    """写额度耗尽上报事件（幂等：一个窗口内同一 Agent 只报一次）。"""
    from models import AgentTaskEvent, db

    agent_id = int(agent.id) if agent else None
    if has_pending_quota_block(agent_id):
        return {'reported': False, 'reason': 'already_reported_in_window'}

    now = datetime.utcnow()
    interaction_id = f"quota-{uuid.uuid4().hex[:12]}"
    payload = {
        'interaction_id': interaction_id,
        'interaction_type': 'token_quota_exhausted',
        'status': 'pending_approval',
        'task_id': int(task.id),
        'governance': {'requires_approval': True, 'risk_tier': 'high'},
        'quota': {
            'category': category,
            'agent_id': agent_id,
            'agent_name': getattr(agent, 'name', None),
            'failure_summary': (failure_reason or '')[:500],
            'block_window_hours': _window_hours(),
            'hint': '该 Agent 的 LLM API token 额度/计费已耗尽：派发已熔断，'
                    '请充值或更换 key；解决后等窗口结束自动恢复，或直接复跑。',
        },
        'requested_at': now.isoformat(),
    }
    db.session.add(AgentTaskEvent(
        task_id=int(task.id),
        attempt_id='',
        agent_id=agent_id,
        workspace_id=int(workspace_id),
        event_type='interaction_request',
        seq=1,
        event_timestamp=now,
        payload=payload,
        message=f"token quota exhausted ({category}) agent={agent_id}",
        created_by='system:quota_guard',
    ))
    return {'reported': True, 'interaction_id': interaction_id}
