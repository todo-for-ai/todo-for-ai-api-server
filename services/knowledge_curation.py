"""项目知识自动策展服务（P3.2）

把平台运行中产生的信号（失败归因 / PR 评审 / 人类纠偏）自动整理为
「项目知识提案」，人类确认后沉淀为项目共享知识条目（KnowledgeEntry），
驳回则归档留痕。上下文规则/项目约定从手填变为「自动建议 + 人工确认」。

设计约束：
- propose_* 幂等（project_id + dedupe_key 唯一），失败只记日志不阻断主流程
- confirm 才生成 KnowledgeEntry（entry_type='rule'，项目内共享）
"""

from datetime import datetime

import structlog

from models import (
    KnowledgeEntry,
    ProjectKnowledgeProposal,
    db,
)

logger = structlog.get_logger()


def _get_or_create_proposal(project_id: int, dedupe_key: str, **fields) -> tuple:
    """幂等取/建提案。返回 (proposal, created)。"""
    existing = ProjectKnowledgeProposal.query.filter_by(
        project_id=project_id, dedupe_key=dedupe_key,
    ).first()
    if existing:
        return existing, False

    proposal = ProjectKnowledgeProposal(
        project_id=project_id,
        workspace_id=fields.pop('workspace_id', None),
        dedupe_key=dedupe_key,
        **fields,
    )
    db.session.add(proposal)
    db.session.flush()
    return proposal, True


def propose_from_failure(task, agent, category: str, failure_reason) -> object:
    """失败归因 → 项目教训提案（P3.2 自动策展第一来源）。

    同一任务同一归因类别只提一次案（dedupe_key=failure:<task>:<category>）。
    """
    workspace_id = task.project.organization_id if task.project else None
    label = {
        'test_failure': '测试失败的教训',
        'build_failure': '构建失败的教训',
        'lint_failure': '静态检查的教训',
        'timeout': '执行超时的教训',
        'auth_error': '认证/权限的教训',
        'transient': '瞬时错误的教训',
        'unknown': '未分类失败的教训',
    }.get(category, f'{category} 的教训')

    proposal, created = _get_or_create_proposal(
        task.project_id,
        f"failure:{task.id}:{category}",
        workspace_id=workspace_id,
        proposal_type='failure_lesson',
        title=f"[自动策展] {label}：{task.title}"[:500],
        content=(
            f"任务 #{task.id}「{task.title}」执行失败，自动归因为 **{label}**。\n\n"
            f"- failure_reason: {(failure_reason or '').strip()[:500] or 'N/A'}\n"
            f"- 执行 Agent: {agent.name if agent else 'N/A'}\n\n"
            f"确认后将沉淀为项目约定/教训，供后续任务与 Agent 参考避免重蹈覆辙。"
        ),
        source_type=ProjectKnowledgeProposal.SOURCE_FAILURE_ATTRIBUTION,
        source_ref={
            'task_id': int(task.id),
            'category': category,
            'failure_reason': (failure_reason or '').strip()[:300],
        },
        proposed_by_agent_id=int(agent.id) if agent else None,
        created_by='system:curator',
    )
    if created:
        logger.info("knowledge_curator.proposed_from_failure",
                    proposal_id=proposal.id, task_id=task.id, category=category)
    return proposal


def propose_from_review(task, agent, reviewer_agent_id: int, pr_number,
                        review_summary: str) -> object:
    """评审意见 → 项目评审洞见提案（复用 P2.4 评审者关卡信号）。"""
    workspace_id = task.project.organization_id if task.project else None
    proposal, created = _get_or_create_proposal(
        task.project_id,
        f"review:{task.id}:{pr_number}",
        workspace_id=workspace_id,
        proposal_type='review_insight',
        title=f"[自动策展] 评审洞见：{task.title}（PR #{pr_number}）"[:500],
        content=(
            f"任务 #{task.id} 的 PR #{pr_number} 通过 Agent 评审。\n\n"
            f"- 评审意见: {(review_summary or '').strip()[:500] or 'N/A'}\n\n"
            f"确认后沉淀为项目评审约定。"
        ),
        source_type=ProjectKnowledgeProposal.SOURCE_PR_REVIEW,
        source_ref={
            'task_id': int(task.id),
            'pr_number': pr_number,
            'reviewer_agent_id': int(reviewer_agent_id) if reviewer_agent_id else None,
        },
        proposed_by_agent_id=int(agent.id) if agent else None,
        created_by='system:curator',
    )
    if created:
        logger.info("knowledge_curator.proposed_from_review",
                    proposal_id=proposal.id, task_id=task.id, pr_number=pr_number)
    return proposal


def confirm_proposal(proposal, user, title: str = None, content: str = None) -> KnowledgeEntry:
    """人工确认提案 → 项目共享知识条目（幂等：已确认直接返回关联条目）。"""
    if proposal.status == ProjectKnowledgeProposal.STATUS_CONFIRMED:
        return KnowledgeEntry.query.get(proposal.knowledge_entry_id)

    if proposal.proposed_by_agent_id is None:
        raise ValueError('proposal has no originating agent to own the knowledge entry')

    entry = KnowledgeEntry(
        agent_id=proposal.proposed_by_agent_id,
        project_id=proposal.project_id,
        title=(title or proposal.title)[:500],
        content=content or proposal.content,
        domain=proposal.proposal_type,
        tags=['curated', proposal.source_type],
        entry_type='rule',
        source_type='auto_extracted',
        confidence=0.8,
        shared_with_project=True,
        is_valid=True,
        created_by=f'user:{user.id}',
    )
    db.session.add(entry)
    db.session.flush()

    proposal.status = ProjectKnowledgeProposal.STATUS_CONFIRMED
    proposal.decided_by_user_id = user.id
    proposal.decided_at = datetime.utcnow()
    proposal.knowledge_entry_id = entry.id
    db.session.commit()

    logger.info("knowledge_curator.confirmed",
                proposal_id=proposal.id, entry_id=entry.id)
    return entry


def dismiss_proposal(proposal, user, reason: str = None) -> None:
    """人工驳回提案（归档留痕，不入知识库）。"""
    if proposal.status == ProjectKnowledgeProposal.STATUS_CONFIRMED:
        raise ValueError('confirmed proposal cannot be dismissed')

    proposal.status = ProjectKnowledgeProposal.STATUS_DISMISSED
    proposal.decided_by_user_id = user.id
    proposal.decided_at = datetime.utcnow()
    proposal.dismissal_reason = (reason or '')[:500]
    db.session.commit()

    logger.info("knowledge_curator.dismissed", proposal_id=proposal.id)
