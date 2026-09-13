"""循环上下文走廊与自动压缩（长跑的记忆层）。

问题：GoalLoop 逐轮物化任务，但执行者每轮都是「失忆」的——不知道
前几轮干了什么、失败了什么；而把全部历史塞进 prompt 又会随轮数无限
膨胀，烧 token 且稀释注意力。

方案（三层走廊，注入每个轮次任务的执行内容顶部）：
1. 目标层：goal_text + done_definition（每轮必带，防跑偏）；
2. 压缩层：context_digest——对「上次摘要覆盖点之前」的历史轮次做
   滚动压缩摘要（LLM 可用时语义压缩，否则抽取式降级），按节奏刷新
   （每 GOAL_LOOP_COMPRESS_EVERY 个新终态轮刷一次，控制 LLM 开销）；
3. 明细层：最近 GOAL_LOOP_CONTEXT_RECENT_ROUNDS 轮保留细节
   （标题/状态/失败归因），更早的进压缩层。

所有层级都有硬性长度钳制：走廊整体超限时先裁明细、再裁摘要头，
保证注入 prompt 的上下文规模有确定上界（自动清理的确定性保证）。
"""

import logging

from .constants import clamp_int, naive_utc_now

log = logging.getLogger(__name__)

# 走廊参数（env 可调）
CONTEXT_RECENT_ROUNDS = 3       # 明细层保留的最近轮数
CONTEXT_COMPRESS_EVERY = 3      # 每累积 N 个新终态轮刷新一次压缩摘要
CONTEXT_MAX_DIGEST_CHARS = 2000  # 压缩层长度上界
CONTEXT_MAX_RECENT_CHARS = 1200  # 单轮明细长度上界
CONTEXT_MAX_CORRIDOR_CHARS = 6000  # 走廊整体长度上界


def _env_int(name, default, lo, hi):
    import os
    raw = os.environ.get(name)
    if not raw:
        return default
    value = clamp_int(raw, lo, hi)
    return default if value is None else value


def recent_rounds_limit() -> int:
    return _env_int('GOAL_LOOP_CONTEXT_RECENT_ROUNDS', CONTEXT_RECENT_ROUNDS, 0, 20)


def compress_every() -> int:
    return _env_int('GOAL_LOOP_COMPRESS_EVERY', CONTEXT_COMPRESS_EVERY, 1, 100)


def max_digest_chars() -> int:
    return _env_int('GOAL_LOOP_MAX_DIGEST_CHARS', CONTEXT_MAX_DIGEST_CHARS, 200, 20000)


def _clip(text, limit):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit] + '…'


def _round_line(task, failure_of=None, limit=CONTEXT_MAX_RECENT_CHARS):
    """单轮明细行：标题 + 状态（+ 失败归因）。"""
    from models import TaskStatus

    status = task.status.value if task.status else 'unknown'
    line = f"- #{task.id} {task.title} → {status}"
    if task.status == TaskStatus.CANCELLED and failure_of is not None:
        failure = failure_of(task.id)
        if failure:
            line += f"（失败：{failure}）"
    return _clip(line, limit)


def build_corridor(loop) -> str:
    """构建注入轮次任务的上下文走廊（目标层 + 压缩层 + 明细层）。

    无任何历史轮次时返回空串（首轮不注入，零开销）。
    """
    from models import TaskStatus
    from .query import loop_tasks, _last_failure_reason

    tasks = loop_tasks(loop.id)
    terminal = [t for t in tasks if t.status in (TaskStatus.DONE, TaskStatus.CANCELLED)]
    if not terminal:
        return ''

    sections = []
    goal_line = f"【总体目标】{loop.goal_text}"
    if loop.done_definition:
        goal_line += f"\n【完成标准】{loop.done_definition}"
    sections.append(_clip(goal_line, 800))

    if loop.context_digest:
        sections.append(f"【历史进展摘要（早期轮次已压缩）】\n{loop.context_digest}")

    recent = terminal[-recent_rounds_limit():]
    recent_lines = [
        _round_line(t, failure_of=_last_failure_reason) for t in recent
    ]
    sections.append("【最近轮次明细】\n" + "\n".join(recent_lines))

    corridor = "\n\n".join(sections)
    # 确定性上界：整体超限先裁明细层（保留头部），再硬截整段
    if len(corridor) > CONTEXT_MAX_CORRIDOR_CHARS:
        corridor = corridor[:CONTEXT_MAX_CORRIDOR_CHARS] + "\n…（上下文走廊已按上限截断）"
    return corridor


def extractive_digest(loop, tasks) -> str:
    """抽取式压缩（无 LLM 降级）：轮次标题+状态+失败归因的紧凑清单。"""
    from .query import _last_failure_reason

    lines = []
    for t in tasks:
        line = _round_line(t, failure_of=_last_failure_reason, limit=200)
        lines.append(line)
    digest = "\n".join(lines)
    return _clip(digest, max_digest_chars())


def compress_digest(loop) -> dict:
    """把「摘要覆盖点之后、明细层之前」的终态轮次滚动压缩进 context_digest。

    - 增量：只压缩 context_digest_upto 之后的部分，摘要滚动向前；
    - LLM 可用走语义压缩，失败/不可用自动降级抽取式（长跑不因无 key 断记忆）；
    - 幂等：压缩后推进 context_digest_upto，重复调用零副作用。
    """
    from models import Task, TaskStatus
    from .query import loop_tasks

    tasks = loop_tasks(loop.id)
    terminal = [t for t in tasks if t.status in (TaskStatus.DONE, TaskStatus.CANCELLED)]
    upto = loop.context_digest_upto or 0
    pending = [t for t in terminal if t.id > upto]

    # 只压缩「将滑出明细层」的轮次；最近的留在明细层
    recent_n = recent_rounds_limit()
    to_compress = pending[:-recent_n] if len(pending) > recent_n else []
    if not to_compress:
        return {'compressed': 0, 'reason': 'nothing_to_compress'}

    old_digest = (loop.context_digest or '').strip()
    new_part = _llm_digest(loop, old_digest, to_compress)
    if new_part is None:
        new_part = extractive_digest(loop, to_compress)

    if old_digest:
        merged = f"{old_digest}\n{new_part}"
    else:
        merged = new_part
    merged = _clip(merged, max_digest_chars())

    loop.context_digest = merged
    loop.context_digest_upto = to_compress[-1].id
    from models import db
    db.session.commit()
    return {'compressed': len(to_compress), 'upto': int(to_compress[-1].id)}


def _llm_digest(loop, old_digest, tasks):
    """LLM 语义压缩；任何失败返回 None（调用方降级抽取式）。"""
    try:
        from .planning import llm_call
        from .query import _last_failure_reason

        lines = [_round_line(t, failure_of=_last_failure_reason, limit=200) for t in tasks]
        system_prompt = (
            '你是目标循环的记忆压缩器。把轮次执行记录滚动合并成一份紧凑的进展摘要，'
            '保留：完成了什么、关键产出/结论、失败过什么及原因、对后续轮次有用的教训。'
            '只输出 JSON：{"digest": "摘要正文"}。'
        )
        user_prompt = (
            f"目标：{loop.goal_text}\n"
            f"完成标准：{loop.done_definition or '（未明确）'}\n"
            f"已有摘要（更早轮次）：\n{old_digest or '（无）'}\n"
            f"新增轮次记录：\n" + "\n".join(lines)
        )
        parsed = llm_call(loop, system_prompt, user_prompt)
        # llm_call 走 extract_json，文本摘要可能解析失败——只接受 dict 里的字段
        if isinstance(parsed, dict):
            for key in ('digest', 'summary', 'text'):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return _clip(value, max_digest_chars())
        return None
    except Exception:  # noqa: BLE001 - 压缩失败绝不阻断循环推进
        log.warning("goal_loop.context_digest_llm_failed", exc_info=True)
        return None


def maybe_compress(loop) -> dict:
    """按节奏刷新压缩摘要：自上次压缩以来累积 ≥ compress_every 个新终态轮才触发。

    在状态机物化下一轮之前调用——保证新轮次的执行者拿到最新记忆。
    任何异常只记日志，绝不阻断推进。
    """
    try:
        from models import TaskStatus
        from .query import loop_tasks

        upto = loop.context_digest_upto or 0
        terminal_ids = [
            t.id for t in loop_tasks(loop.id)
            if t.status in (TaskStatus.DONE, TaskStatus.CANCELLED) and t.id > upto
        ]
        if len(terminal_ids) < compress_every():
            return {'compressed': 0, 'reason': 'below_cadence'}
        return compress_digest(loop)
    except Exception:  # noqa: BLE001
        log.warning("goal_loop.context_compress_failed", exc_info=True)
        return {'compressed': 0, 'reason': 'error'}
