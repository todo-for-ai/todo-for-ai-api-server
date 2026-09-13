"""
失败自愈循环（P2.3）

基于 commit 协议的 failed 提交（failure_code/failure_reason/证据）：
1. 自动归因：failure_code 与 reason 文本 → 归因类别（test_failure/
   build_failure/lint_failure/timeout/auth_error/transient/unknown）
2. 重试封顶：按 AgentTaskAttempt 历史失败次数计数，未超封顶时自动生成
   修复子任务回流（继承父任务 DoD 与执行属性）；超过封顶升级人工审批
   （interaction_request 事件，审批队列可见）
3. 幂等：同一 attempt 只生成一次修复/升级动作
"""

import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from models import (
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskEvent,
    Task,
    db,
)

# 归因类别：failure_code 精确匹配优先，其次 reason 关键词
CODE_CATEGORY_MAP = {
    "TESTS_FAILED": "test_failure",
    "TEST_FAILURE": "test_failure",
    "DOD_CHECK_FAILED": "test_failure",
    "BUILD_FAILED": "build_failure",
    "COMPILATION_ERROR": "build_failure",
    "LINT_FAILED": "lint_failure",
    "TIMEOUT": "timeout",
    "EXECUTION_TIMEOUT": "timeout",
    "AUTH_ERROR": "auth_error",
    "PERMISSION_DENIED": "auth_error",
    "LEASE_EXPIRED": "transient",
    "RATE_LIMITED": "transient",
    "NETWORK_ERROR": "transient",
    # 资源级故障：额度/计费耗尽——重试无意义，走熔断+上报而非自愈重试
    "QUOTA_EXCEEDED": "quota_exhausted",
    "INSUFFICIENT_QUOTA": "quota_exhausted",
    "INSUFFICIENT_CREDITS": "quota_exhausted",
    "BILLING_ERROR": "quota_exhausted",
    "PAYMENT_REQUIRED": "quota_exhausted",
}

REASON_KEYWORDS = (
    ("assert", "test_failure"),
    ("test failed", "test_failure"),
    ("tests failed", "test_failure"),
    ("compilation", "build_failure"),
    ("build", "build_failure"),
    ("lint", "lint_failure"),
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("unauthorized", "auth_error"),
    ("permission", "auth_error"),
    ("rate limit", "transient"),
    ("connection", "transient"),
    # 额度/计费关键词（各 CLI 引擎对 401/402/429 表述不一，按文本兜底）
    ("insufficient_quota", "quota_exhausted"),
    ("quota exceeded", "quota_exhausted"),
    ("exceeded your current quota", "quota_exhausted"),
    ("credit balance", "quota_exhausted"),
    ("insufficient credits", "quota_exhausted"),
    ("billing", "quota_exhausted"),
    ("payment required", "quota_exhausted"),
    ("usage limit", "quota_exhausted"),
)

CATEGORY_LABELS = {
    "test_failure": "测试失败",
    "build_failure": "构建失败",
    "lint_failure": "静态检查失败",
    "timeout": "执行超时",
    "auth_error": "认证/权限错误",
    "transient": "瞬时错误",
    "quota_exhausted": "API 额度/计费耗尽",
    "unknown": "未分类失败",
}

# 不可重试的资源级故障：不生成修复子任务，直接熔断派发 + 升级人工
NON_RETRYABLE_CATEGORIES = {"quota_exhausted"}

# 默认重试封顶（可被 agent.max_retry / 预算覆盖，此处为服务级缺省；
# 部署可用 FAILURE_REPAIR_MAX_ATTEMPTS 调节，长跑场景建议放宽）
DEFAULT_MAX_REPAIR_ATTEMPTS = 2


def _max_repair_attempts() -> int:
    """服务级重试封顶：环境变量 FAILURE_REPAIR_MAX_ATTEMPTS 优先。"""
    raw = os.environ.get('FAILURE_REPAIR_MAX_ATTEMPTS')
    if not raw:
        return DEFAULT_MAX_REPAIR_ATTEMPTS
    try:
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPAIR_ATTEMPTS


def _record_failure_experience(task, agent, category: str,
                               failure_code: Optional[str], failure_reason: Optional[str]) -> None:
    """失败经验自动入库（P2.3 → P3.1 学习闭环）。

    归因结果作为 failure_pattern 经验沉淀到 AgentExperience，
    供技能画像（skill_profile）与派单打分（experience_bonus）消费。
    经验写入失败只记日志，不阻断自愈主流程。
    """
    import structlog

    from models import AgentExperience

    if not agent:
        return
    logger = structlog.get_logger()
    try:
        tags = getattr(task, 'tags', None) or []
        db.session.add(AgentExperience(
            agent_id=int(agent.id),
            experience_type='failure_pattern',
            domain=str(tags[0]).strip().lower() if tags else None,
            task_type=category,
            capabilities_used=(getattr(agent, 'capabilities', None) or [])[:3],
            outcome_pattern=f"{failure_code or 'N/A'}: {(failure_reason or '').strip()[:300]}",
            key_learnings=f"自动归因类别: {category}（任务 #{task.id}）",
            confidence=0.6,
            source_task_id=int(task.id),
        ))
        db.session.flush()
    except Exception as e:  # noqa: BLE001 - 经验沉淀绝不能阻断恢复主流程
        logger.warning("recovery.experience_record_failed", error=str(e))
        db.session.rollback()


def classify_failure(failure_code: Optional[str], failure_reason: Optional[str]) -> str:
    """归因：failure_code 精确匹配优先，reason 关键词次之，其余 unknown。"""
    code = (failure_code or "").strip().upper()
    if code in CODE_CATEGORY_MAP:
        return CODE_CATEGORY_MAP[code]

    reason = (failure_reason or "").lower()
    for keyword, category in REASON_KEYWORDS:
        if keyword in reason:
            return category
    return "unknown"


def count_failed_attempts(task_id: int) -> int:
    """统计任务历史上失败的 attempt 数（用于重试封顶）。"""
    return int(
        AgentTaskAttempt.query.filter_by(
            task_id=task_id, state=AgentTaskAttemptState.ABORTED
        ).count()
    )


def _has_recovery_event(task_id: int, attempt_id: str) -> bool:
    return (
        AgentTaskEvent.query.filter(
            AgentTaskEvent.task_id == task_id,
            AgentTaskEvent.payload["recovery"]["attempt_id"].as_string() == attempt_id,
        ).first()
        is not None
    )


def handle_failed_commit(task, agent, attempt_id: str,
                         failure_code: Optional[str] = None,
                         failure_reason: Optional[str] = None,
                         max_attempts: Optional[int] = None,
                         auto_repair: bool = True) -> Dict[str, Any]:
    """commit 协议 failed 提交的自愈入口（P2.3）。

    - 幂等：同一 attempt_id 只处理一次
    - 未达重试封顶：自动生成修复子任务（继承 DoD，回流派发池）
    - 达到封顶：写 repair_escalation interaction_request（审批队列可见），
      人工批准后在任务上重置状态重新派发
    - auto_repair=False：只做归因/经验沉淀，不生成修复子任务也不升级人工
      （目标循环任务的失败由循环规划器重规划，双通道会重复派发）
    """
    if max_attempts is None:
        max_attempts = _max_repair_attempts()
    category = classify_failure(failure_code, failure_reason)
    failed_attempts = count_failed_attempts(task.id)

    now = datetime.utcnow()
    interaction_id = f"recp-{uuid.uuid4().hex[:12]}"
    workspace_id = task.project.organization_id if task.project else None

    if _has_recovery_event(task.id, attempt_id):
        return {"action": "skipped", "reason": "already processed", "category": category}

    # 失败经验沉淀（P3.1 学习闭环）：每个被处理的 attempt 记一条 failure_pattern
    _record_failure_experience(task, agent, category, failure_code, failure_reason)

    # 项目知识自动策展（P3.2）：失败归因 → 知识提案（幂等，不阻断主流程）
    try:
        from services.knowledge_curation import propose_from_failure

        propose_from_failure(task, agent, category, failure_reason)
        db.session.flush()
    except Exception as e:  # noqa: BLE001 - 策展失败不阻断恢复主流程
        import structlog

        structlog.get_logger().warning("recovery.curation_proposal_failed", error=str(e))
        db.session.rollback()

    # 资源级故障（额度/计费耗尽）：重试无意义——熔断该 Agent 派发并上报
    # 用户；循环/普通任务都不生成修复子任务、不计重试封顶。
    if category in NON_RETRYABLE_CATEGORIES and workspace_id:
        from services.quota_guard import raise_quota_exhausted

        quota_report = raise_quota_exhausted(
            workspace_id, task, agent,
            category=category, failure_reason=failure_reason or '',
        )
        db.session.commit()
        return {
            "action": "escalated_quota_exhausted",
            "category": category,
            "failed_attempts": failed_attempts,
            "quota_report": quota_report,
        }

    if not auto_repair:
        db.session.commit()
        return {
            "action": "loop_replan",
            "category": category,
            "failed_attempts": failed_attempts,
        }

    # 封顶：升级人工（interaction_request 审批事件，budget/pr 审批同一队列可见）
    if failed_attempts >= max_attempts:
        if workspace_id:
            payload = {
                "interaction_id": interaction_id,
                "interaction_type": "repair_escalation",
                "status": "pending_approval",
                "task_id": int(task.id),
                "governance": {"requires_approval": True, "risk_tier": "high"},
                "recovery": {
                    "attempt_id": attempt_id,
                    "category": category,
                    "failed_attempts": failed_attempts,
                    "max_attempts": max_attempts,
                    "failure_summary": (failure_reason or "")[:500],
                },
                "requested_at": now.isoformat(),
            }
            db.session.add(AgentTaskEvent(
                task_id=int(task.id),
                attempt_id="",
                agent_id=int(agent.id) if agent else None,
                workspace_id=int(workspace_id),
                event_type="interaction_request",
                seq=1,
                event_timestamp=now,
                payload=payload,
                message=f"repair escalation {interaction_id} category={category} attempts={failed_attempts}",
                created_by="system:recovery",
            ))
            db.session.commit()
        return {
            "action": "escalated_human",
            "interaction_id": interaction_id,
            "category": category,
            "failed_attempts": failed_attempts,
            "max_attempts": max_attempts,
        }

    # 未封顶：生成修复子任务回流
    reason_line = (failure_reason or failure_code or category)[:300]
    repair = Task.create(
        project_id=task.project_id,
        owner_id=task.owner_id,
        title=f"[修复] {task.title}（{CATEGORY_LABELS.get(category, category)}，第 {failed_attempts + 1} 次）"[:500],
        content=(
            f"父任务 #{task.id} 执行失败，自动归因：**{CATEGORY_LABELS.get(category, category)}**\n\n"
            f"- failure_code: `{failure_code or 'N/A'}`\n"
            f"- failure_reason: {reason_line}\n"
            f"- 失败尝试: 第 {failed_attempts + 1}/{max_attempts + 1} 次\n"
            + (f"\n附加信息: {attempt_id}" if attempt_id else "")
        ),
        priority=task.priority,
        revision=1,
        is_ai_task=True,
        dod=task.dod,  # 继承父任务 DoD
        parent_task_id=task.id,
        creator_id=task.creator_id,
        creator_type="ai",
        creator_identifier=f"recovery:{category}",
        created_by="system:recovery",
    )
    db.session.flush()  # 取 repair.id 供事件 payload 引用

    payload = {
        "interaction_id": interaction_id,
        "interaction_type": "repair_created",
        "status": "auto_repairing",
        "task_id": int(task.id),
        "repair_task_id": int(repair.id),
        "recovery": {
            "attempt_id": attempt_id,
            "category": category,
            "failed_attempts": failed_attempts,
            "max_attempts": max_attempts,
        },
        "created_at": now.isoformat(),
    }
    if workspace_id:
        db.session.add(AgentTaskEvent(
            task_id=int(task.id),
            attempt_id="",
            agent_id=int(agent.id) if agent else None,
            workspace_id=int(workspace_id),
            event_type="recovery_created",
            seq=1,
            event_timestamp=now,
            payload=payload,
            message=f"repair subtask {repair.id} created ({category})",
            created_by="system:recovery",
        ))
    db.session.commit()

    return {
        "action": "repair_created",
        "repair_task_id": int(repair.id),
        "category": category,
        "failed_attempts": failed_attempts,
        "max_attempts": max_attempts,
    }
