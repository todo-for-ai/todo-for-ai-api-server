"""Agent 综合健康度分析服务（评分 / 告警 / 趋势 / 状态迁移）。

从 api/agents/health.py 下沉的计算逻辑：路由层只做参数解析与鉴权，
本模块以 owner_id（而非 flask user 对象）为入参，便于单测与复用。

四个入口：
- compute_agent_health      综合健康分（声誉/完成率/冲突/沙盒合规四维加权）
- compute_health_alerts     低分告警（含原因与改进建议）
- compute_health_trend      按日健康趋势（声誉审计流聚合，可按 Agent 过滤）
- compute_state_transitions 健康状态迁移流（healthy/degraded/critical Sankey）
"""

import datetime as dt
from datetime import datetime, timedelta

from sqlalchemy import func

from models import (
    Agent,
    AgentConflict,
    AgentReputation,
    AuditLog,
    SandboxViolation,
    TaskAssignment,
    TaskAssignmentState,
)

# 健康状态分档阈值
HEALTHY_THRESHOLD = 80.0
DEGRADED_THRESHOLD = 50.0

_SUB_SCORE_LABELS = {
    "reputation": "声誉",
    "completion": "完成率",
    "conflict": "冲突控制",
    "violation": "沙盒合规",
}

_DEFAULT_WEIGHTS = {"reputation": 0.4, "completion": 0.3,
                    "conflict": 0.15, "violation": 0.15}


def normalize_health_weights(weights):
    """归一化四维权重：非法值回退默认、负值截 0、全零回退默认、总和归一。"""
    w = dict(_DEFAULT_WEIGHTS)
    if weights:
        for k in _DEFAULT_WEIGHTS:
            try:
                v = float(weights.get(k, _DEFAULT_WEIGHTS[k]))
            except (TypeError, ValueError):
                v = _DEFAULT_WEIGHTS[k]
            w[k] = max(0.0, v)
    total_w = sum(w.values())
    if total_w <= 0:
        w = dict(_DEFAULT_WEIGHTS)
        total_w = sum(w.values())
    return {k: v / total_w for k, v in w.items()}


def compute_agent_health(owner_id, days, weights=None, with_recommendations=False):
    """综合健康分计算。返回 (days, items)，items 按 health_score 降序。

    四个子分：声誉（AgentReputation.score，缺省 50）、完成率（近 days 天
    TaskAssignment done 占比，无记录记 50）、冲突惩罚（少者得分高）、
    沙盒违规惩罚。with_recommendations=True 时附最少一条改进建议。
    """
    w = normalize_health_weights(weights)

    since = datetime.utcnow() - timedelta(days=days)
    agents = Agent.query.filter_by(owner_id=owner_id).with_entities(
        Agent.id, Agent.name, Agent.status).all()
    if not agents:
        return days, []

    agent_ids = [a.id for a in agents]

    reps = {r.agent_id: r for r in AgentReputation.query.filter(
        AgentReputation.agent_id.in_(agent_ids)).all()}
    assign_rows = (
        TaskAssignment.query
        .filter(TaskAssignment.agent_id.in_(agent_ids),
                TaskAssignment.created_at >= since)
        .with_entities(TaskAssignment.agent_id, TaskAssignment.state)
        .all()
    )
    prod: dict = {}
    for aid, state in assign_rows:
        b = prod.setdefault(aid, {"total": 0, "done": 0})
        b["total"] += 1
        if state and state.value == "done":
            b["done"] += 1

    conflict_rows = (
        AgentConflict.query
        .filter(AgentConflict.owner_id == owner_id,
                AgentConflict.created_at >= since)
        .with_entities(AgentConflict.agent_ids)
        .all()
    )
    conflict_counts: dict = {}
    for (agent_ids_json,) in conflict_rows:
        for aid in (agent_ids_json or []):
            conflict_counts[aid] = conflict_counts.get(aid, 0) + 1

    violation_rows = (
        SandboxViolation.query
        .filter(SandboxViolation.agent_id.in_(agent_ids),
                SandboxViolation.blocked_at >= since)
        .with_entities(SandboxViolation.agent_id, func.count(SandboxViolation.id))
        .group_by(SandboxViolation.agent_id)
        .all()
    )
    violation_counts = {aid: c for aid, c in violation_rows}

    max_conflicts = max(conflict_counts.values(), default=1)
    max_violations = max(violation_counts.values(), default=1)

    items = []
    for a in agents:
        rep = reps.get(a.id)
        rep_score = rep.score if rep and rep.score is not None else 50.0
        p = prod.get(a.id, {"total": 0, "done": 0})
        completion_rate = (p["done"] / p["total"] * 100) if p["total"] > 0 else None
        completion_score = completion_rate if completion_rate is not None else 50.0
        cc = conflict_counts.get(a.id, 0)
        vc = violation_counts.get(a.id, 0)
        conflict_score = 100 * (1 - cc / max_conflicts) if max_conflicts > 0 else 100.0
        violation_score = 100 * (1 - vc / max_violations) if max_violations > 0 else 100.0

        health = round(
            rep_score * w["reputation"] + completion_score * w["completion"]
            + conflict_score * w["conflict"] + violation_score * w["violation"],
            1,
        )
        sub_scores = {
            "reputation": round(rep_score, 1),
            "completion": round(completion_score, 1),
            "conflict": round(conflict_score, 1),
            "violation": round(violation_score, 1),
        }
        item = {
            "agent_id": a.id,
            "name": a.name,
            "status": a.status.value if a.status else None,
            "health_score": health,
            "reputation_score": round(rep_score, 1),
            "completion_rate": round(completion_rate, 1) if completion_rate is not None else None,
            "total_assignments": p["total"],
            "done_assignments": p["done"],
            "conflicts": cc,
            "sandbox_violations": vc,
            "sub_scores": sub_scores,
        }
        if with_recommendations:
            item["recommendations"] = _build_recommendations(
                rep_score, completion_rate, p["total"], cc, vc, sub_scores)
        items.append(item)
    items.sort(key=lambda x: x["health_score"], reverse=True)
    return days, items


def _build_recommendations(rep_score, completion_rate, total_assignments,
                           conflicts, violations, sub_scores):
    """从最弱维度推导改进建议；至少返回一条（最弱维度提示）。"""
    recs = []
    if rep_score < 50:
        recs.append("声誉分偏低，建议复盘近期失败任务并补充正向反馈以恢复信任")
    if completion_rate is not None and completion_rate < 50:
        recs.append("完成率偏低，建议核减负载或拆解复杂任务后再分配")
    elif total_assignments == 0:
        recs.append("近期无任务分配，建议主动领取任务以建立产出记录")
    if conflicts > 0:
        recs.append(f"近期发生 {conflicts} 次协作冲突，建议复核协作边界与消息协议")
    if violations > 0:
        recs.append(f"近期发生 {violations} 次沙盒违规，建议收紧工具权限并复查沙盒策略")
    if not recs:
        weakest = sorted(sub_scores.items(), key=lambda x: x[1])[:1]
        for dim, score in weakest:
            recs.append(
                f"当前最弱维度为「{_SUB_SCORE_LABELS.get(dim, dim)}」({score})，建议针对性改进")
    return recs


def compute_health_alerts(owner_id, days, min_health_score, weights=None):
    """低于阈值 Agent 的告警列表（附触发原因与改进建议）。"""
    _, items = compute_agent_health(owner_id, days, weights=weights,
                                    with_recommendations=True)
    alerts = []
    for a in items:
        if a["health_score"] >= min_health_score:
            continue
        reasons = []
        if a["sub_scores"]["reputation"] < 50:
            reasons.append(f"声誉 {a['sub_scores']['reputation']} 偏低")
        if a["completion_rate"] is not None and a["completion_rate"] < 50:
            reasons.append(f"完成率 {a['completion_rate']}% 偏低")
        if a["conflicts"] > 0:
            reasons.append(f"冲突 {a['conflicts']} 次")
        if a["sandbox_violations"] > 0:
            reasons.append(f"违规 {a['sandbox_violations']} 次")
        a_copy = dict(a)
        a_copy["reasons"] = reasons
        alerts.append(a_copy)
    return alerts


def compute_health_trend(owner_id, days, agent_id=None):
    """按日健康趋势：聚合声誉审计（reputation.update）的日末分值与增减次数。

    附带同期的协作冲突与沙盒违规计数；agent_id 可把范围收窄到单个 Agent
    （冲突计数随之按参与方过滤）。返回 dict（days/trend/总计字段...）。
    """
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(
        owner_id=owner_id).with_entities(Agent.id).all()]
    if agent_id is not None and agent_id not in agent_ids:
        return {"days": days, "trend": [], "total_positive": 0,
                "total_negative": 0, "agent_id": agent_id, "agent_name": None,
                "by_kind_overall": {}}
    if agent_id is not None:
        agent_ids = [agent_id]
    if not agent_ids:
        return {"days": days, "trend": [], "total_positive": 0,
                "total_negative": 0, "by_kind_overall": {}}

    selected_name = None
    if agent_id is not None:
        selected_name = Agent.query.filter_by(id=agent_id).with_entities(
            Agent.name).first()
        selected_name = selected_name[0] if selected_name else None

    kind_map = {
        aid: (k.value if k else "unknown")
        for aid, k in Agent.query.filter(Agent.id.in_(agent_ids)).with_entities(
            Agent.id, Agent.kind).all()
    }

    rows = (
        AuditLog.query
        .filter(
            AuditLog.action == "reputation.update",
            AuditLog.resource_type == "agent",
            AuditLog.resource_id.in_(agent_ids),
            AuditLog.created_at >= since,
        )
        .with_entities(
            func.date(AuditLog.created_at).label("d"),
            AuditLog.resource_id,
            AuditLog.detail,
        )
        .all()
    )

    # per (day, agent) 保留当日最后一次分值；统计正/负增量次数
    last_score_by_day_agent: dict = {}
    pos_by_day: dict = {}
    neg_by_day: dict = {}
    for d, aid, detail in rows:
        key = (str(d), aid)
        det = detail or {}
        new_score = det.get("new_score")
        delta = det.get("score_delta")
        if new_score is not None:
            last_score_by_day_agent[key] = new_score
        if delta is not None:
            try:
                dval = float(delta)
            except (TypeError, ValueError):
                continue
            if dval > 0:
                pos_by_day[str(d)] = pos_by_day.get(str(d), 0) + 1
            elif dval < 0:
                neg_by_day[str(d)] = neg_by_day.get(str(d), 0) + 1

    day_scores: dict = {}
    day_kind_scores: dict = {}
    for (day, aid), score in last_score_by_day_agent.items():
        day_scores.setdefault(day, []).append(score)
        k = kind_map.get(aid, "unknown")
        day_kind_scores.setdefault(day, {}).setdefault(k, []).append(score)

    conflict_by_day: dict = {}
    if agent_id is not None:
        conflict_rows_raw = (
            AgentConflict.query
            .filter(AgentConflict.owner_id == owner_id,
                    AgentConflict.created_at >= since)
            .with_entities(func.date(AgentConflict.created_at).label("d"),
                           AgentConflict.agent_ids)
            .all()
        )
        for d, agent_ids_json in conflict_rows_raw:
            if agent_ids_json and agent_id in (agent_ids_json or []):
                conflict_by_day[str(d)] = conflict_by_day.get(str(d), 0) + 1
    else:
        conflict_rows = (
            AgentConflict.query
            .filter(AgentConflict.owner_id == owner_id,
                    AgentConflict.created_at >= since)
            .with_entities(func.date(AgentConflict.created_at).label("d"),
                           func.count(AgentConflict.id))
            .group_by("d")
            .all()
        )
        conflict_by_day = {str(d): c for d, c in conflict_rows if d}

    violation_rows = (
        SandboxViolation.query
        .filter(SandboxViolation.agent_id.in_(agent_ids),
                SandboxViolation.blocked_at >= since)
        .with_entities(func.date(SandboxViolation.blocked_at).label("d"),
                       func.count(SandboxViolation.id))
        .group_by("d")
        .all()
    )
    violation_by_day = {str(d): c for d, c in violation_rows if d}

    trend = []
    kind_overall: dict = {}
    for day in sorted(day_scores.keys()):
        scores = day_scores[day]
        avg = round(sum(scores) / len(scores), 2) if scores else None
        dk = day_kind_scores.get(day, {})
        by_kind_avg: dict = {}
        for k, ks in dk.items():
            if ks:
                by_kind_avg[k] = round(sum(ks) / len(ks), 2)
                kind_overall.setdefault(k, []).extend(ks)
        trend.append({
            "date": day,
            "avg_reputation": avg,
            "positive": pos_by_day.get(day, 0),
            "negative": neg_by_day.get(day, 0),
            "conflicts": conflict_by_day.get(day, 0),
            "sandbox_violations": violation_by_day.get(day, 0),
            "by_kind_avg": by_kind_avg,
        })

    by_kind_overall = {k: round(sum(ks) / len(ks), 2)
                       for k, ks in kind_overall.items() if ks}
    by_kind_overall_sorted = dict(sorted(by_kind_overall.items(),
                                         key=lambda kv: kv[1], reverse=True))

    return {
        "days": days,
        "trend": trend,
        "total_positive": sum(pos_by_day.values()),
        "total_negative": sum(neg_by_day.values()),
        "total_conflicts": sum(conflict_by_day.values()),
        "total_violations": sum(violation_by_day.values()),
        "agent_id": agent_id,
        "agent_name": selected_name,
        "by_kind_overall": by_kind_overall_sorted,
    }


def compute_state_transitions(owner_id, days):
    """健康状态迁移流：按日分档（healthy/degraded/critical）并统计迁移。

    返回 {'days', 'states', 'flows', 'total_transitions'}，flows 按次数降序，
    适合 Sankey 图。
    """
    since = datetime.utcnow() - timedelta(days=days)
    agent_ids = [a.id for a in Agent.query.filter_by(
        owner_id=owner_id).with_entities(Agent.id).all()]
    if not agent_ids:
        return {"days": days, "transitions": [], "states": []}

    rows = (
        AuditLog.query
        .filter(
            AuditLog.action == "reputation.update",
            AuditLog.resource_type == "agent",
            AuditLog.resource_id.in_(agent_ids),
            AuditLog.created_at >= since,
        )
        .with_entities(
            func.date(AuditLog.created_at).label("d"),
            AuditLog.resource_id,
            AuditLog.detail,
        )
        .all()
    )

    def classify(score: float) -> str:
        if score >= HEALTHY_THRESHOLD:
            return "healthy"
        if score >= DEGRADED_THRESHOLD:
            return "degraded"
        return "critical"

    agent_states: dict = {}
    for d, aid, detail in rows:
        # detail 为历史脏数据时可能不是 dict（回退 0 分 = critical）
        new_score = detail.get("new_score", 0) if isinstance(detail, dict) else 0
        state = classify(new_score)
        # MySQL 的 func.date 返回 date、SQLite 返回 str——统一转字符串键
        day_key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        agent_states.setdefault(aid, {})[day_key] = state

    transitions: dict = {}
    state_totals: dict = {}
    for aid, date_states in agent_states.items():
        sorted_dates = sorted(date_states.items())
        for i, (_, s) in enumerate(sorted_dates):
            state_totals[s] = state_totals.get(s, 0) + 1
            if i > 0:
                prev_s = sorted_dates[i - 1][1]
                if prev_s != s:
                    key = (prev_s, s)
                    transitions[key] = transitions.get(key, 0) + 1

    states = ["healthy", "degraded", "critical"]
    flows = []
    for (src, dst), cnt in sorted(transitions.items(), key=lambda kv: kv[1], reverse=True):
        flows.append({"source": src, "target": dst, "value": cnt})

    return {
        "days": days,
        "states": [{"name": s, "count": state_totals.get(s, 0)} for s in states],
        "flows": flows,
        "total_transitions": sum(transitions.values()),
    }
