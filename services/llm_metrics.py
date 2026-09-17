"""LLM 调用指标：摄取清洗 + 用户/组织/Agent 三视角聚合。

数据来源：agent-runtime 每次引擎真实调用上报（幂等键 call_id 防重复摄取）。
聚合口径：created_at 落在窗口内；归属用户在摄取时由服务端解析
（agent.owner_id，缺省 creator_user_id），用户视图即"我名下 Agent 的调用"。
"""

from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from models import db, LlmCallMetric, Agent

_ALLOWED_STATUSES = {'success', 'failed', 'timeout'}

_MAX_CALLS_PER_BATCH = 100
_FIELD_CAPS = {
    'engine': 32, 'model': 128, 'base_url': 255,
    'status': 16, 'error_code': 64, 'error_message': 512,
}
_STRING_FIELDS = ('engine', 'model', 'base_url', 'status',
                  'error_code', 'error_message')
_INT_FIELDS = ('task_id', 'duration_ms', 'input_tokens', 'output_tokens',
               'total_tokens', 'cache_read_tokens')
_FLOAT_FIELDS = ('cost_usd',)

# 分位数计算的时长采样上限（窗口内调用远超此数时以采样近似，足够可观测）
_DURATION_SAMPLE_LIMIT = 5000


def _clean_call(data):
    """校验并清洗单条上报；不合法抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError('call must be an object')
    call_id = str(data.get('call_id') or '').strip()
    if not call_id or len(call_id) > 36:
        raise ValueError('call_id is required (<=36 chars)')
    cleaned = {'call_id': call_id}
    status = str(data.get('status') or 'success').strip().lower()
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f'status must be one of {sorted(_ALLOWED_STATUSES)}')
    for field in _STRING_FIELDS:
        value = data.get(field)
        cleaned[field] = str(value).strip()[:_FIELD_CAPS[field]] if value is not None else ''
    cleaned['status'] = status
    for field in _INT_FIELDS:
        value = data.get(field)
        cleaned[field] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    for field in _FLOAT_FIELDS:
        value = data.get(field)
        cleaned[field] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if cleaned['duration_ms'] is None or cleaned['duration_ms'] < 0:
        cleaned['duration_ms'] = 0
    return cleaned


def record_calls(agent, calls):
    """批量摄取；返回 (inserted, skipped)。逐条落库，重复 call_id 跳过不阻断。"""
    if not isinstance(calls, list):
        raise ValueError('calls must be a list')
    if len(calls) > _MAX_CALLS_PER_BATCH:
        raise ValueError(f'at most {_MAX_CALLS_PER_BATCH} calls per batch')
    inserted = skipped = 0
    for raw in calls:
        try:
            data = _clean_call(raw)
            db.session.add(LlmCallMetric.record_call(agent, data))
            db.session.commit()
            inserted += 1
        except IntegrityError:
            db.session.rollback()
            skipped += 1
        except ValueError:
            db.session.rollback()
            raise
    return inserted, skipped


def _num(value, digits=2):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if digits is not None else value


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    idx = min(int(len(sorted_values) * pct / 100), len(sorted_values) - 1)
    return sorted_values[idx]


def summarize(base_query, hours):
    """对给定过滤口径做窗口聚合，返回 totals/by_day/by_agent/by_model/recent_failures。"""
    since = datetime.utcnow() - timedelta(hours=hours)
    q = base_query.filter(LlmCallMetric.created_at >= since)

    calls = q.count()
    success = q.filter(LlmCallMetric.status == 'success').count()
    failed = q.filter(LlmCallMetric.status == 'failed').count()
    timeout = q.filter(LlmCallMetric.status == 'timeout').count()

    token_row = q.with_entities(
        func.sum(LlmCallMetric.input_tokens),
        func.sum(LlmCallMetric.output_tokens),
        func.sum(LlmCallMetric.total_tokens),
        func.sum(LlmCallMetric.cache_read_tokens),
        func.sum(LlmCallMetric.cost_usd),
        func.avg(LlmCallMetric.duration_ms),
    ).first()

    durations = [
        int(row[0]) for row in
        q.filter(LlmCallMetric.status != 'timeout')
        .with_entities(LlmCallMetric.duration_ms)
        .order_by(LlmCallMetric.duration_ms.asc())
        .limit(_DURATION_SAMPLE_LIMIT)
        .all()
    ]

    by_day = [
        {
            'date': str(day),
            'calls': int(cnt or 0),
            'total_tokens': int(tokens or 0),
            'avg_duration_ms': _num(avg, 1),
        }
        for day, cnt, tokens, avg in q.with_entities(
            func.date(LlmCallMetric.created_at),
            func.count(LlmCallMetric.id),
            func.sum(LlmCallMetric.total_tokens),
            func.avg(LlmCallMetric.duration_ms),
        ).group_by(func.date(LlmCallMetric.created_at)).order_by(
            func.date(LlmCallMetric.created_at)).all()
    ]

    by_agent = _by_agent(q)

    by_model = [
        {'model': model or '(unknown)', 'calls': int(cnt or 0),
         'total_tokens': int(tokens or 0)}
        for model, cnt, tokens in q.with_entities(
            LlmCallMetric.model,
            func.count(LlmCallMetric.id),
            func.sum(LlmCallMetric.total_tokens),
        ).group_by(LlmCallMetric.model).order_by(
            func.count(LlmCallMetric.id).desc()).limit(10).all()
    ]

    recent_failures = [
        {
            'created_at': row.created_at.isoformat() if row.created_at else None,
            'agent_id': row.agent_id,
            'task_id': row.task_id,
            'engine': row.engine,
            'model': row.model,
            'base_url': row.base_url,
            'status': row.status,
            'error_code': row.error_code,
            'error_message': row.error_message,
        }
        for row in q.filter(LlmCallMetric.status != 'success')
        .order_by(LlmCallMetric.created_at.desc()).limit(10).all()
    ]

    return {
        'window_hours': hours,
        'totals': {
            'calls': calls,
            'success': success,
            'failed': failed,
            'timeout': timeout,
            'success_rate': _num(success * 1.0 / calls, 4) if calls else None,
            'avg_duration_ms': _num(token_row[5], 1),
            'p50_duration_ms': _percentile(durations, 50),
            'p95_duration_ms': _percentile(durations, 95),
            'input_tokens': int(token_row[0] or 0),
            'output_tokens': int(token_row[1] or 0),
            'total_tokens': int(token_row[2] or 0),
            'cache_read_tokens': int(token_row[3] or 0),
            'cost_usd': _num(token_row[4], 6),
        },
        'by_day': by_day,
        'by_agent': by_agent,
        'by_model': by_model,
        'recent_failures': recent_failures,
    }


def _by_agent(q):
    """按 Agent 分组聚合（名称缺失时回退显示 ID）。"""
    rows = (
        q.join(Agent, Agent.id == LlmCallMetric.agent_id, isouter=True)
        .with_entities(
            LlmCallMetric.agent_id,
            Agent.name,
            func.count(LlmCallMetric.id),
            func.sum(LlmCallMetric.total_tokens),
            func.sum(case((LlmCallMetric.status == 'success', 1), else_=0)),
            func.avg(LlmCallMetric.duration_ms),
        )
        .group_by(LlmCallMetric.agent_id, Agent.name)
        .all()
    )
    return [
        {
            'agent_id': agent_id,
            'agent_name': name or f'#{agent_id}',
            'calls': int(cnt or 0),
            'total_tokens': int(tokens or 0),
            'success_rate': _num(float(succ or 0) / float(cnt), 4) if cnt else None,
            'avg_duration_ms': _num(avg, 1),
        }
        for agent_id, name, cnt, tokens, succ, avg in rows
    ]


def user_scope_query(user_id):
    return LlmCallMetric.query.filter(LlmCallMetric.owner_user_id == user_id)


def workspace_scope_query(workspace_id):
    return LlmCallMetric.query.filter(LlmCallMetric.workspace_id == workspace_id)


def agent_scope_query(agent_id):
    return LlmCallMetric.query.filter(LlmCallMetric.agent_id == agent_id)


def parse_window_hours(args, default=168):
    """窗口参数：hours（默认 7 天，上限 90 天）。"""
    try:
        hours = int(args.get('hours', default))
    except (TypeError, ValueError):
        return default
    return max(1, min(hours, 2160))
