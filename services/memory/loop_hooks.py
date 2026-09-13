"""循环生命周期 → 记忆写入钩子（长跑的自动沉淀）。

写什么、写到哪个维度：
- 循环达成：会话级（本次运行的完成总结，随循环过期价值递减）+
  项目级（持久结论：这个项目怎么把这类目标做成的）；
- 循环受阻（STALLED，含额度停车）：项目级（「此目标曾在此处受阻 +
  原因」，未来同类目标开跑时被召回，避免重蹈覆辙）。

所有写入 try/except 包裹——记忆沉淀绝不影响循环本身的状态流转。
"""

import logging

from . import scopes as memory_scopes
from . import store as memory_store

log = logging.getLogger(__name__)


def on_loop_completed(loop, summary: str, rounds: int) -> None:
    """循环 DONE：会话级总结 + 项目级持久结论。"""
    try:
        if not loop.agent_id:
            return
        chain = memory_scopes.chain_from_loop(loop)
        short_goal = (loop.goal_text or loop.title or '')[:120]

        session_ref = next(
            (r for r in chain if r.scope_type.value == 'session'), None)
        if session_ref:
            memory_store.remember(
                session_ref, 'summary',
                f'循环 #{loop.id} 达成：{short_goal}',
                f'共 {rounds} 轮。{summary or ""}'[:2000],
                source_type='loop_done', source_task_id=loop.last_task_id,
                agent_id=loop.agent_id, confidence=75,
            )
        # 项目级持久结论（不含会话细节）
        project_ref = next(
            (r for r in chain if r.scope_type.value == 'project'), None)
        if project_ref:
            memory_store.remember(
                project_ref, 'pattern',
                f'目标「{short_goal}」已达成（{rounds} 轮）',
                f'达成结论：{(summary or "（规划器未给出总结）")[:800]}',
                source_type='loop_done', source_task_id=loop.last_task_id,
                agent_id=loop.agent_id, confidence=80,
            )
    except Exception:  # noqa: BLE001 - 记忆沉淀绝不影响循环收尾
        log.warning("memory.loop_completed_hook_failed", exc_info=True)


def on_loop_blocked(loop, reason: str) -> None:
    """循环 STALLED（无进展/额度停车/规划器连续受阻）：项目级受阻教训。"""
    try:
        if not loop.agent_id:
            return
        chain = memory_scopes.chain_from_loop(loop)
        project_ref = next(
            (r for r in chain if r.scope_type.value == 'project'), None)
        if not project_ref:
            return
        memory_store.remember(
            project_ref, 'insight',
            f'目标「{(loop.goal_text or loop.title or "")[:120]}」曾受阻停车',
            f'受阻原因：{(reason or loop.last_error or "未知")[:500]}。'
            f'同类目标重跑前先解决该前置问题。',
            source_type='loop_blocked', source_task_id=loop.last_task_id,
            agent_id=loop.agent_id, confidence=75,
        )
    except Exception:  # noqa: BLE001
        log.warning("memory.loop_blocked_hook_failed", exc_info=True)


def recall_for_loop_step(loop, step_title: str, step_content: str, top_k=3) -> list:
    """按步骤文本沿继承链召回记忆，返回带维度标签的注入行。"""
    try:
        from . import recall_top_k

        chain = memory_scopes.chain_from_loop(loop)
        hits = memory_store.recall(
            chain, f'{step_title or ""} {step_content or ""}',
            top_k=top_k or recall_top_k(),
        )
        return memory_store.format_memory_lines(hits)
    except Exception:  # noqa: BLE001 - 召回失败不阻断派发
        log.warning("memory.loop_step_recall_failed", exc_info=True)
        return []
