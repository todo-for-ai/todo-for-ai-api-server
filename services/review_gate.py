"""
多 Agent 角色编排 - 评审者关卡（P2.4）

- 内置角色模板 seed：developer / reviewer / tester（幂等，AgentRoleTemplate）
- TeamTaskOrchestration.role_assignments：角色 → Agent 映射
- 评审者关卡：require_agent_review 启用时，PR 合并前必须存在
  Agent 评审者提交的通过评审（evidence_type='review', status=passed），
  且禁止自评（评审者不得是产出该 PR 的执行 Agent）
"""

from typing import Any, Dict, Optional

from models import (
    AgentRoleTemplate,
    AgentTaskLease,
    AgentTaskEvent,
    TaskEvidenceRecord,
    db,
)

BUILTIN_ROLE_TEMPLATE_NAMES = ("developer", "reviewer", "tester")

_ROLE_TEMPLATE_SEEDS = (
    {
        "name": "developer",
        "display_name": "开发者",
        "category": "developer",
        "description": "负责功能实现：编码、构建、单元测试",
        "capability_tags": ["coding", "build", "unit_test"],
        "system_prompt": "你是资深开发者。根据任务与 DoD 实现代码，保证测试通过后再提交。",
    },
    {
        "name": "reviewer",
        "display_name": "评审者",
        "category": "qa",
        "description": "负责代码评审：正确性、边界、安全与可维护性，给出 approve/reject 结论",
        "capability_tags": ["code_review", "security"],
        "system_prompt": "你是严格的代码评审者。逐项核对 DoD 与改动差异，发现问题必须拒绝并给出修复建议。",
    },
    {
        "name": "tester",
        "display_name": "测试者",
        "category": "qa",
        "description": "负责验收测试：执行集成/回归测试并提交通过证据",
        "capability_tags": ["testing", "regression"],
        "system_prompt": "你是验收测试者。执行完整回归，只有全部通过时才提交 passed 证据。",
    },
)


def ensure_builtin_role_templates(workspace_id: Optional[int], created_by_user_id: int):
    """幂等创建 workspace 级 developer/reviewer/tester 角色模板。返回创建的数量。"""
    existing = {
        row.name for row in AgentRoleTemplate.query.filter(
            AgentRoleTemplate.name.in_(BUILTIN_ROLE_TEMPLATE_NAMES),
            AgentRoleTemplate.workspace_id == workspace_id,
        ).all()
    }
    created = 0
    for seed in _ROLE_TEMPLATE_SEEDS:
        if seed["name"] in existing:
            continue
        db.session.add(AgentRoleTemplate(
            workspace_id=workspace_id,
            created_by_user_id=created_by_user_id,
            name=seed["name"],
            display_name=seed["display_name"],
            description=seed["description"],
            category=seed["category"],
            capability_tags=seed["capability_tags"],
            system_prompt=seed["system_prompt"],
            is_builtin=False,
            created_by=f'user:{created_by_user_id}',
        ))
        created += 1
    if created:
        db.session.commit()
    return created


def resolve_reviewer_agent_id(task, binding) -> Optional[int]:
    """解析任务的评审者 Agent：绑定级显式指定优先，回退团队编排 role_assignments。"""
    if binding is not None and binding.reviewer_agent_id:
        return int(binding.reviewer_agent_id)

    from models import TeamTaskOrchestration

    orchestration = (
        TeamTaskOrchestration.query
        .filter_by(task_id=task.id)
        .order_by(TeamTaskOrchestration.id.desc())
        .first()
    )
    if orchestration and isinstance(orchestration.role_assignments, dict):
        reviewer_id = orchestration.role_assignments.get("reviewer")
        if reviewer_id:
            return int(reviewer_id)
    return None


def check_agent_review_gate(task, binding, pr_number: Optional[int],
                            author_agent_id: Optional[int] = None) -> Dict[str, Any]:
    """合并前评审者关卡。

    返回 {'passed': bool, 'reason': str, ...}。未启用关卡直接通过。
    规则：
    - 必须存在与 PR 关联的 review 证据（evidence_type='review'）且 status=passed
    - 评审者不得是产出 PR 的执行 Agent（自评禁止）
    """
    if binding is None or not bool(getattr(binding, "require_agent_review", False)):
        return {"passed": True, "reason": "gate_disabled"}

    rows = (
        TaskEvidenceRecord.query
        .filter_by(task_id=task.id, evidence_type='review')
        .order_by(TaskEvidenceRecord.id.desc())
        .limit(50)
        .all()
    )
    matched = []
    for row in rows:
        detail = row.detail or {}
        if pr_number is not None and detail.get('pr_number') not in (None, pr_number):
            continue
        matched.append(row)

    reviewer_agent_id = resolve_reviewer_agent_id(task, binding)

    passed_reviews = [r for r in matched if r.status == 'passed']
    if not passed_reviews:
        return {
            "passed": False,
            "reason": "agent_review_required",
            "reviewer_agent_id": reviewer_agent_id,
            "pr_number": pr_number,
        }

    # 自评禁止：评审证据的 agent 不得等于 PR 作者 agent
    if author_agent_id:
        for review in passed_reviews:
            if review.agent_id == author_agent_id:
                return {
                    "passed": False,
                    "reason": "self_review_forbidden",
                    "reviewer_agent_id": review.agent_id,
                    "pr_number": pr_number,
                }

    return {
        "passed": True,
        "reason": "review_passed",
        "reviewer_agent_id": passed_reviews[0].agent_id,
        "pr_number": pr_number,
    }
